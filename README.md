# HRRR Ingest

A command-line tool for ingesting [HRRR (High-Resolution Rapid Refresh)](https://rapidrefresh.noaa.gov/hrrr/) forecast data at specific lat/lon points into a local DuckDB database.

It uses [Herbie](https://herbie.readthedocs.io/) to download GRIB2 forecast files from NOAA's public AWS/NOMADS sources, resolves the nearest HRRR grid points via a cached BallTree (Haversine), and stores the extracted values in a `hrrr_forecasts` DuckDB table. Ingestion is **idempotent** — re-running the same command skips data that has already been stored.

---

## Prerequisites

- **Python 3.11+**
- **Environment variable** `HERBIE_CONFIG_PATH` must be set to the project root directory (where `config.toml` lives). Herbie uses this to resolve cache paths.

  ```bash
  export HERBIE_CONFIG_PATH=/path/to/this/project
  ```

---

## Installation

1. **Clone the repository** and `cd` into it:

   ```bash
   git clone <repo-url>
   cd comity
   ```

2. **Create and activate a virtual environment** (recommended):

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

3. **Install dependencies**:

   ```bash
   pip install -r requirements.txt
   ```

4. **Install the package in editable mode** (this registers the `hrrr-ingest` and `tree_setup` console commands):

   ```bash
   pip install -e .
   ```

5. **Set the environment variable** (add to your shell profile for persistence):

   ```bash
   export HERBIE_CONFIG_PATH=$(pwd)
   ```

---

## Setup: Generate the BallTree Cache

Before running `hrrr-ingest`, you **must** generate the HRRR spatial BallTree cache. This is a one-time setup step that downloads a single reference GRIB2 file, builds a BallTree index for nearest-point lookups, saves it as a `.pkl` file, and cleans up the GRIB2 data.

```bash
tree_setup
```

This will:

1. Check if a cached BallTree `.pkl` already exists under `.cache/herbie/data/BallTree/`.
2. If not, download a reference HRRR GRIB2, build the BallTree, and save it.
3. Clean up the temporary GRIB2 files (only the `.pkl` remains).
4. Validate the cache by cross-checking BallTree resolution against Herbie's `pick_points` method with test coordinates.

You should see output ending with:

```
  RESULT: Both methods produce identical nearest grid points.
```

---

## Usage

```bash
hrrr-ingest <points_file> [OPTIONS]
```

### Arguments

| Argument | Description |
|---|---|
| `points_file` | Path to a text file with lat/lon pairs (comma or newline separated). See [`points.txt`](points.txt) for an example. |

### Options

| Option | Default | Description |
|---|---|---|
| `--run-date` | `latest` | Forecast run date in `YYYY-MM-DD` format. Defaults to the latest available (current UTC time). The reference time is automatically offset to UTC+6. |
| `--variables` | `ALL_SUPPORTED` | Comma-separated list of variable names to ingest (e.g. `temperature_2m,surface_pressure`). Defaults to all variables defined in [`map.json`](map.json). |
| `--num-hours` | `48` | Forecast lead time range. Iterates `fxx` from `0` through `num-hours` (inclusive). HRRR supports `fxx` 0–48 for runs initialised at 00z, 06z, 12z, 18z. |

### Examples

**Ingest all variables for the latest run, full 48-hour forecast:**

```bash
hrrr-ingest points.txt
```

**Ingest only temperature and pressure for a specific date, first 6 hours:**

```bash
hrrr-ingest points.txt --run-date 2025-07-01 --variables temperature_2m,surface_pressure --num-hours 6
```

---

## Points File Format

The points file contains latitude/longitude pairs. Values can be separated by commas, newlines, or both. Each pair is `latitude,longitude`.

Example (`points.txt`):

```
33.784500,-86.052400
31.006900,-88.010300
31.756900,-106.375000
```

---

## Supported Variables

Variables are defined in [`map.json`](map.json). Each entry specifies:

- `variable_name` — Human-readable name (used in `--variables` filter and stored in DuckDB)
- `description` — What the variable measures
- `short_hand` — The xarray dataset key for extracting values
- `index_locator` — The Herbie/GRIB2 search string passed to `H.xarray()`

| Variable Name | Description | Index Locator |
|---|---|---|
| `surface_pressure` | Surface pressure | `:PRES:surface` |
| `surface_roughness` | Surface roughness | `:SFCR:surface` |
| `visible_beam_downward_solar_flux` | Visible beam downward solar flux | `:VBDSF:surface` |
| `visible_diffuse_downward_solar_flux` | Visible diffuse downward solar flux | `:VDDSF:surface` |
| `temperature_2m` | Temperature at 2m above ground | `:TMP:2 m above ground` |
| `dewpoint_2m` | Dew point temperature at 2m above ground | `:DPT:2 m above ground` |
| `relative_humidity_2m` | Relative humidity at 2m above ground | `:RH:2 m above ground` |
| `u_component_wind_10m` | U-component of wind at 10m above ground | `:UGRD:10 m above ground` |
| `v_component_wind_10m` | V-component of wind at 10m above ground | `:VGRD:10 m above ground` |
| `u_component_wind_80m` | U-component of wind at 80m above ground | `:UGRD:80 m above ground` |
| `v_component_wind_80m` | V-component of wind at 80m above ground | `:VGRD:80 m above ground` |

---

## DuckDB Output

Ingested data is stored in a local `data.db` file (DuckDB) in the project directory. The table schema is:

| Column | Type | Description |
|---|---|---|
| `valid_time_utc` | `TIMESTAMP` | UTC timestamp of the forecast valid time (`run_time_utc` + `fxx` hours) |
| `run_time_utc` | `TIMESTAMP` | UTC timestamp of the model run time (the static UTC+6 base time) |
| `latitude` | `FLOAT` | Latitude of the nearest HRRR grid point |
| `longitude` | `FLOAT` | Longitude of the nearest HRRR grid point |
| `variable` | `VARCHAR` | Human-readable variable name |
| `value` | `FLOAT` | The forecasted value |
| `source_s3` | `VARCHAR` | S3 path to the source GRIB2 file |

### Querying the data

```bash
pip install duckdb  # already included in requirements
python -c "import duckdb; con = duckdb.connect('data.db'); print(con.sql('SELECT * FROM hrrr_forecasts LIMIT 10').df())"
```

---

## Idempotency

The ingestion command is fully idempotent:

- Before making any Herbie API call, the tool queries DuckDB to check whether data for the given `(valid_time_utc, run_time_utc, variable, lat/lon points)` combination already exists.
- If all points for a variable/fxx combination are already stored, the Herbie download is **skipped entirely**.
- Duplicate rows are never inserted thanks to a `WHERE NOT EXISTS` guard on inserts.
- Re-running the same command produces `0 new rows inserted` and reports the number of skipped rows.

---

## Testing

The [`testing/`](testing/) folder contains a Jupyter notebook used for exploratory testing and validation of HRRR data retrieval:

- **[`Testing HRRR Data.ipynb`](testing/Testing%20HRRR%20Data.ipynb)** — Interactive notebook for experimenting with Herbie downloads, inspecting xarray datasets, verifying BallTree point resolution, and spot-checking extracted forecast values. Useful for debugging and understanding the data pipeline before running the full ingestion CLI.

To run it:

```bash
pip install jupyter  # if not already installed
jupyter notebook testing/Testing\ HRRR\ Data.ipynb
```

---

## Project Structure

```
comity/
├── cli.py              # Main ingestion logic (entry point: hrrr-ingest)
├── get_nei_tree.py     # BallTree cache generation & validation (entry point: tree_setup)
├── setup.py            # Package setup with console_scripts
├── config.toml         # Herbie configuration (uses $HERBIE_CONFIG_PATH)
├── map.json            # Variable definitions (name, shorthand, GRIB2 search string)
├── points.txt          # Example lat/lon input file
├── requirements.txt    # Python dependencies
├── data.db             # DuckDB database (created on first ingest)
└── testing/
    └── Testing HRRR Data.ipynb  # Exploratory testing & validation notebook
```
