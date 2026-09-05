import pickle
import shutil
import sys

import numpy as np
import pandas as pd
from pathlib import Path
from herbie import Herbie
import herbie


def resolve_points_balltree(points_df):
    """Resolve nearest HRRR grid points by querying the cached BallTree pkl directly."""
    cache_dir = Path(herbie.config["default"]["save_dir"])
    ball_tree_dir = cache_dir / "BallTree"
    pkl_files = sorted(ball_tree_dir.glob("hrrr_*.pkl"))
    if not pkl_files:
        print("ERROR: No cached BallTree pkl found. Run a Herbie download first to generate the cache.")
        sys.exit(1)

    pkl_path = pkl_files[0]
    with open(pkl_path, "rb") as f:
        tree = pickle.load(f)

    EARTH_RADIUS_KM = 6371
    query_rad = np.deg2rad(points_df[["latitude", "longitude"]].values)
    dist, ind = tree.query(query_rad, k=1)
    dist_km = dist * EARTH_RADIUS_KM

    tree_data = np.asarray(tree.data)
    nearest_coords = np.rad2deg(tree_data[ind.flatten()])

    resolved_df = pd.DataFrame({
        "latitude": nearest_coords[:, 0],
        "longitude": nearest_coords[:, 1],
        "distance_km": dist_km.flatten(),
    })
    resolved_df["longitude"] = resolved_df["longitude"].apply(
        lambda x: x - 360 if x > 180 else x
    )
    return resolved_df


def resolve_points_pick_points(ds, points_df):
    """Resolve nearest HRRR grid points using Herbie's ds.herbie.pick_points."""
    point_ds = ds.herbie.pick_points(points_df, method="nearest")

    resolved_df = pd.DataFrame({
        "latitude": point_ds["latitude"].values,
        "longitude": point_ds["longitude"].values,
        "distance_km": point_ds["point_grid_distance"].values,
    })
    resolved_df["longitude"] = resolved_df["longitude"].apply(
        lambda x: x - 360 if x > 180 else x
    )
    return resolved_df


def validate_point_resolution(balltree_df, pick_points_df, tolerance=1e-4):
    """Compare resolved grid points from both methods and report any differences."""
    print("\n--- Point Resolution Validation ---")
    all_match = True

    for col in ["latitude", "longitude", "distance_km"]:
        bt_vals = balltree_df[col].values
        pp_vals = pick_points_df[col].values
        if np.allclose(bt_vals, pp_vals, atol=tolerance):
            print(f"  [OK] '{col}' values match (tolerance={tolerance})")
        else:
            print(f"  [MISMATCH] '{col}':")
            for i in range(len(bt_vals)):
                diff = abs(bt_vals[i] - pp_vals[i])
                flag = " <-- DIFF" if diff > tolerance else ""
                print(f"    Point {i+1}: BallTree={bt_vals[i]:.6f}  pick_points={pp_vals[i]:.6f}  diff={diff:.8f}{flag}")
            all_match = False

    if all_match:
        print("  RESULT: Both methods produce identical nearest grid points.")
    else:
        print("  RESULT: Differences detected between methods!")
    print("-----------------------------------\n")
    return all_match


def validate_cache(ds_ref, test_points):
    """Validate the cached BallTree against Herbie's pick_points using test points.

    Parameters
    ----------
    ds_ref : xr.Dataset
        A Herbie xarray dataset to use for pick_points resolution.
    test_points : pd.DataFrame
        DataFrame with 'latitude' and 'longitude' columns.
    """
    balltree_df = resolve_points_balltree(test_points)
    pick_points_df = resolve_points_pick_points(ds_ref, test_points)

    return validate_point_resolution(balltree_df, pick_points_df)


def generate_hrrr_cache():
    # 1. Define the cache directory (from Herbie's resolved config)
    cache_dir = Path(herbie.config["default"]["save_dir"])

    ref_date = "2024-01-01 06:00"
    search_query = ":TMP:2 m above ground"

    # 2. Check if the .pkl BallTree file already exists
    cache_existed = False
    if cache_dir.exists():
        pkl_files = list(cache_dir.rglob("*.pkl"))
        if pkl_files:
            print(f"Cache already exists! Found BallTree at: {pkl_files[0]}")
            cache_existed = True

    if not cache_existed:
        print("Cache not found. Generating now...")

        print("1. Initializing Herbie and downloading reference GRIB2...")
        H_ref = Herbie(ref_date, model="hrrr", product="sfc", fxx=0)
        ds_ref = H_ref.xarray(search_query)

        print("2. Building and saving the spatial BallTree (.pkl)...")
        dummy_point = pd.DataFrame({"latitude": [40.0], "longitude": [-100.0]})
        _ = ds_ref.herbie.pick_points(dummy_point, method="nearest")

        # Close the dataset so the file is unlocked and can be deleted
        ds_ref.close()

        print("3. Cleaning up reference GRIB2 and index files...")
        hrrr_folder = cache_dir / "hrrr"

        if hrrr_folder.exists():
            shutil.rmtree(hrrr_folder)
            print(f"Deleted hrrr data folder at: {hrrr_folder}")

        pkl_files = list(cache_dir.rglob("*.pkl"))
        print(f"\nSuccess! Only the spatial cache remains: {pkl_files[0]}")

    # 4. Validate the BallTree cache against Herbie's pick_points
    print("\n4. Validating BallTree cache against Herbie pick_points...")
    H_val = Herbie(ref_date, model="hrrr", product="sfc", fxx=0)
    ds_val = H_val.xarray(search_query)

    test_points = pd.DataFrame({
        "latitude": [40.0, 35.0, 30.0],
        "longitude": [-100.0, -90.0, -85.0],
    })
    validate_cache(ds_val, test_points)
    ds_val.close()

    # Clean up validation reference data
    hrrr_folder = cache_dir / "hrrr"
    if hrrr_folder.exists():
        shutil.rmtree(hrrr_folder)
        print(f"Cleaned up validation data at: {hrrr_folder}")


if __name__ == "__main__":
    generate_hrrr_cache()
