import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import duckdb
import pandas as pd
from herbie import Herbie

from get_nei_tree import resolve_points_balltree


def parse_points_file(filepath):
    """
    Parses the points file. Handles lat/lons that are separated by commas,
    newlines, or both.
    """
    points = []
    try:
        with open(filepath, 'r') as f:
            content = f.read()

        # Replace newlines with commas, split by comma, and remove empty strings
        raw_values = [val.strip() for val in content.replace('\n', ',').split(',') if val.strip()]

        # Group into pairs (lat, lon)
        for i in range(0, len(raw_values), 2):
            if i + 1 < len(raw_values):
                lat = float(raw_values[i])
                lon = float(raw_values[i + 1])
                points.append((lat, lon))
        return points
    except Exception as e:
        print(f"Error reading points file: {e}")
        sys.exit(1)


def get_s3_path(H):
    """Convert a Herbie instance's GRIB2 source URL to an S3 path."""
    grib_url = H.grib
    if grib_url.startswith("https://"):
        return grib_url.replace("https://", "s3://", 1)
    return grib_url


def transform_rows(H_ref, var_point_ds, resolved_df, var_info, valid_time_utc, run_time_utc):
    """Transform extracted point data into rows matching the hrrr_forecasts schema.

    Parameters
    ----------
    H_ref : Herbie
        Herbie instance (used only for the S3 source path).
    var_point_ds : xr.Dataset
        Dataset returned by pick_points for this variable.
    resolved_df : pd.DataFrame
        Resolved nearest grid points.
    var_info : dict
        Variable metadata from map.json.
    valid_time_utc : pd.Timestamp
        The forecast valid time (run_time_utc + fxx delta).
    run_time_utc : pd.Timestamp
        The base UTC+6 model run time (static, no fxx offset).

    Returns a list of dicts with keys:
    valid_time_utc, run_time_utc, latitude, longitude, variable, value, source_s3
    """
    rows = []
    source_s3 = get_s3_path(H_ref)
    variable_name = var_info["variable_name"]
    short_hand = var_info["short_hand"]

    for idx in range(len(resolved_df)):
        rows.append({
            "valid_time_utc": valid_time_utc,
            "run_time_utc": run_time_utc,
            "latitude": float(resolved_df["latitude"].values[idx]),
            "longitude": float(resolved_df["longitude"].values[idx]),
            "variable": variable_name,
            "value": float(var_point_ds[short_hand].values[idx]),
            "source_s3": source_s3,
        })

    return rows


def ensure_table_exists(con):
    """Create the hrrr_forecasts table if it does not already exist."""
    con.execute("""
        CREATE TABLE IF NOT EXISTS hrrr_forecasts (
            valid_time_utc TIMESTAMP,
            run_time_utc TIMESTAMP,
            latitude FLOAT,
            longitude FLOAT,
            variable VARCHAR,
            value FLOAT,
            source_s3 VARCHAR
        )
    """)


def load_rows(con, rows):
    """Insert transformed rows into hrrr_forecasts, skipping duplicates.

    Duplicates are identified by (valid_time_utc, run_time_utc, latitude,
    longitude, variable).  Incoming latitude/longitude/value are cast to FLOAT
    before comparison so that 32-bit stored values match the 64-bit inputs.
    Returns the number of newly inserted rows.
    """
    if not rows:
        return 0

    rows_df = pd.DataFrame(rows)

    before_count = con.execute("SELECT COUNT(*) FROM hrrr_forecasts").fetchone()[0]

    con.execute("""
        INSERT INTO hrrr_forecasts
        SELECT s.valid_time_utc, s.run_time_utc,
               CAST(s.latitude AS FLOAT), CAST(s.longitude AS FLOAT),
               s.variable, CAST(s.value AS FLOAT), s.source_s3
        FROM rows_df s
        WHERE NOT EXISTS (
            SELECT 1 FROM hrrr_forecasts h
            WHERE h.valid_time_utc = s.valid_time_utc
              AND h.run_time_utc = s.run_time_utc
              AND h.latitude = CAST(s.latitude AS FLOAT)
              AND h.longitude = CAST(s.longitude AS FLOAT)
              AND h.variable = s.variable
        )
    """)

    after_count = con.execute("SELECT COUNT(*) FROM hrrr_forecasts").fetchone()[0]
    return after_count - before_count


def is_data_ingested(con, run_time_utc, valid_time_utc, variable_name, resolved_df):
    """Check if data for a specific variable/time/points combination is already fully ingested.

    Queries the database for exact matches on the resolved nearest-grid
    lat/lon coordinates (cast to FLOAT for consistency with stored values).
    Returns True only when every point in resolved_df already has a row.
    """
    if len(resolved_df) == 0:
        return True

    # Build a temporary DataFrame of the resolved points for the query
    pts = resolved_df[["latitude", "longitude"]].copy()

    matched = con.execute("""
        SELECT COUNT(*) FROM (
            SELECT DISTINCT h.latitude, h.longitude
            FROM hrrr_forecasts h
            INNER JOIN pts p
              ON h.latitude = CAST(p.latitude AS FLOAT)
             AND h.longitude = CAST(p.longitude AS FLOAT)
            WHERE h.valid_time_utc = ?
              AND h.run_time_utc = ?
              AND h.variable = ?
        )
    """, [valid_time_utc, run_time_utc, variable_name]).fetchone()[0]

    return matched >= len(resolved_df)


def main():
    parser = argparse.ArgumentParser(
        description="Ingest HRRR forecast data for specific lat/long points."
    )

    # Positional required argument
    parser.add_argument(
        "points_file",
        metavar="points.txt",
        type=str,
        help="Text file with a list of lat-long pairs."
    )

    # Options
    parser.add_argument(
        "--run-date",
        type=str,
        default="latest",
        help="Forecast run date (e.g., YYYY-MM-DD). Defaults to the last available date with complete data."
    )

    parser.add_argument(
        "--variables",
        type=str,
        default="ALL_SUPPORTED",
        help="Comma separated list of variables to ingest. Defaults to all supported variables."
    )

    parser.add_argument(
        "--num-hours",
        type=int,
        default=48,
        help="Forecast lead time range (fxx 0 through num-hours). Defaults to 48."
    )

    args = parser.parse_args()

    # 1. Parse the points
    points = parse_points_file(args.points_file)

    # 2. Load map.json variable definitions
    map_json_path = Path(__file__).parent / "map.json"
    with open(map_json_path, "r") as f:
        all_variables = json.load(f)

    if args.variables == "ALL_SUPPORTED":
        variables = all_variables
    else:
        requested = {v.strip() for v in args.variables.split(',')}
        variables = [v for v in all_variables if v["variable_name"] in requested]

    # 3. Determine the reference date (UTC+6),if latest,
    # subtract 2 hours to ensure the HRRR run has finished uploading to AWS
    if args.run_date == "latest":
        ref_dt = datetime.now(timezone.utc) - timedelta(hours=2)
    else:
        ref_dt = datetime.strptime(args.run_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(hours=6)
    ref_date = ref_dt.strftime("%Y-%m-%d %H:%M")

    # 4. Build a single DataFrame of all points for pick_points
    points_df = pd.DataFrame(
        [{"latitude": lat, "longitude": lon} for lat, lon in points]
    )

    # 5. Resolve nearest grid points via cached BallTree (no date needed)
    resolved_df = resolve_points_balltree(points_df)

    # 6. Open DuckDB connection and ensure table exists
    db_path = Path(__file__).parent / "data.db"
    try:
        con = duckdb.connect(str(db_path))
    except duckdb.IOException:
        print(f"WARNING: '{db_path}' is not a valid DuckDB file. Removing and recreating.")
        db_path.unlink(missing_ok=True)
        con = duckdb.connect(str(db_path))
    ensure_table_exists(con)
    print("DuckDB table 'hrrr_forecasts' ready.")

    print(f"\n--- Starting HRRR Ingest ---")
    print(f"Run Time (UTC+6):   {ref_date}")
    print(f"Forecast hours:     0 through {args.num_hours}")
    print(f"Variables:          {[v['variable_name'] for v in variables]}")
    print(f"Points:             {len(points)} resolved to nearest HRRR grid")
    print(points_df.to_string(index=False))
    print(f"----------------------------\n")

    total_inserted = 0
    total_skipped = 0
    num_points = len(resolved_df)

    # run_time_utc is the base UTC+6 time (static across all fxx)
    run_time_utc = pd.Timestamp(ref_date)

    # 8. Iterate over each forecast lead time (fxx) from 0 to num_hours
    for fxx in range(args.num_hours + 1):
        # valid_time_utc leverages the fxx delta
        valid_time_utc = run_time_utc + pd.Timedelta(hours=fxx)

        # Check if ALL variables for this fxx are already ingested (skip Herbie entirely)
        all_ingested = all(
            is_data_ingested(con, run_time_utc, valid_time_utc, v["variable_name"], resolved_df)
            for v in variables
        )
        if all_ingested:
            total_skipped += len(variables) * num_points
            print(f"fxx={fxx} | valid={valid_time_utc} | run={run_time_utc} | already ingested, skipping")
            continue

        print(f"fxx={fxx} | valid={valid_time_utc} | run={run_time_utc}")
        try:
            H_ref = Herbie(ref_date, model="hrrr", product="sfc", fxx=fxx)
        except Exception as e:
            print(f"  [ERROR] Could not initialise Herbie for fxx={fxx}: {e}\n")
            continue

        for var in variables:
            index_locator = var["index_locator"]
            short_hand = var["short_hand"]
            variable_name = var["variable_name"]

            # Skip if this variable is already ingested for this fxx
            if is_data_ingested(con, run_time_utc, valid_time_utc, variable_name, resolved_df):
                total_skipped += num_points
                print(f"  {variable_name}: already ingested ({num_points} pts), skipping")
                continue

            print(f"  {variable_name}: fetching ({index_locator}) ...")
            try:
                ds = H_ref.xarray(index_locator)
                var_point_ds = ds.herbie.pick_points(points_df, method="nearest")

                # Transform and load into DuckDB
                rows = transform_rows(H_ref, var_point_ds, resolved_df, var, valid_time_utc, run_time_utc)
                inserted = load_rows(con, rows)
                total_inserted += inserted

                print(f"    -> {inserted} new / {len(rows)} total rows")
            except Exception as e:
                print(f"    [ERROR] Could not fetch {variable_name} at fxx={fxx}: {e}")
            print()
        print()

    print(f"=== Ingest complete: {total_inserted} new rows inserted, {total_skipped} rows skipped (already ingested) ===")
    row_count = con.execute("SELECT COUNT(*) FROM hrrr_forecasts").fetchone()[0]
    print(f"Total rows in hrrr_forecasts: {row_count}")
    con.close()


if __name__ == "__main__":
    main()