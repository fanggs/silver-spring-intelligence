# Data pipeline

This folder builds the SQLite database consumed by the rest of CivicLens.

Planned scripts:

- `fetch_census.py`: all Montgomery County ACS tract estimates.
- `fetch_geometry.py`: Census tract GeoJSON and land area.
- `fetch_businesses.py`: public business points.
- `build_database.py`: creates `data/silverspring.db`.
- `validate_database.py`: verifies the database before a commit.

The output database is not committed. The scripts, schema, and source metadata are.

## Run

Create a project-root `.env` with `CENSUS_API_KEY=...`, install `requests pyshp`, then run:

`python -m get_data.build_database`

Validate with:

`python -m get_data.validate_database`
