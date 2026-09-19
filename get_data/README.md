# Data pipeline

This folder builds the SQLite database consumed by the rest of CivicLens.

Planned scripts:

- `fetch_census.py`: all Montgomery County ACS tract estimates.
- `fetch_geometry.py`: Census tract GeoJSON and land area.
- `fetch_businesses.py`: public business points.
- `build_database.py`: creates `data/silverspring.db`.
- `validate_database.py`: verifies the database before a commit.

The validated output database, `data/silverspring.db`, is committed so the API/LLM
teammate can query the same reviewed artifact. The project-root `.env` remains
private and must never be committed.

## Run

Create a project-root `.env` with `CENSUS_API_KEY=...`, install `requests pyshp`, then run:

`python -m get_data.build_database`

Validate with:

`python -m get_data.validate_database`
