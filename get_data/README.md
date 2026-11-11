# Data pipeline

CivicLens serves the committed SQLite snapshot at `data/silverspring.db`.
The service reads the snapshot on demand. Source updates are deliberate.

To rebuild, put the Census API key in the project `.env` as `CENSUS_API_KEY`,
then install the data dependencies and build a review copy:

```sh
python -m pip install requests pyshp
python -m get_data.build_database --output /tmp/civiclens-review.db
python -m get_data.validate_database /tmp/civiclens-review.db
```

Omit `--output` to build and validate a replacement for the local snapshot.
The builder loads current tract estimates, the earlier historical vintage,
tract geometry, OpenStreetMap listings, named-place mappings, official Silver
Spring place estimates, and industry employment. It checks a temporary database
before replacing the requested output. A failed build leaves the previous file
in place. Keep a reviewed snapshot in version control for rollback.

A complete saved Overpass response can be supplied with `--osm-businesses-json`
or `--osm-places-json` to retry a build without repeating that download.

- `place_data` holds official ACS estimates for Silver Spring's Census
  Designated Place. `tract_data` and `tract_history` hold whole-tract estimates.
  Place and tract results describe different boundaries.
- `industry_employment` uses ACS table C24050. It describes employed residents
  by industry. It cannot locate their jobs. Census sex estimates come from
  B01001 and cannot measure self-identified gender.
- `businesses` preserves both `osm_amenity` and `osm_shop` tags and a separate
  broader group. A restaurant is an `amenity=restaurant` listing. Nearby
  matching node/area pairs with the same name and type and compatible addresses
  are merged conservatively. Both source URLs are retained. Listings may be
  incomplete or stale. Named neighborhoods and districts use mapped tracts.

The validator checks required tables, historical coverage, tract links,
industry coverage, official place estimates, original listing types and source
records. It can be run independently against an existing database.

## Supplementary partner data

The bundled `data/partner_snapshot` files can be added with
`python -m get_data.import_community --warehouse data/partner_snapshot/community.duckdb --geometry data/partner_snapshot/tracts.geojson`.
Install `get_data/requirements-community.txt` for this import. The primary build
also imports the bundled pair after rebuilding Census and OSM records; it can
accept a replacement pair with `--partner-warehouse` and `--partner-geometry`.
The normal validator checks both primary and imported source integrity.
See `docs/combined-data.md` for scope and maintenance limitations.
# Dated race and Hispanic-origin estimates

The bundled `data/demographics_2024.json` contains extracted ACS 2020–2024
B02001 and B03002 estimates for all current county tracts and the official
Silver Spring place. Labels and parent categories come from the Census table
shells. `demographic_estimates` stores estimates and margins of error;
`demographic_sources` records download URLs, checksums and timestamps.

Refresh from the official public summary files (no API/model key needed):

```bash
python -m get_data.import_demographics
```

Downloads are processed in a temporary directory. Only the local geography
extract is retained. Reimport that extract without network access:

```bash
python -m get_data.import_demographics --snapshot data/demographics_2024.json
```

The importer validates a separate candidate before replacing the database.
The normal database rebuild reapplies the bundled demographic extract so
these measures are not lost. Refresh the extract separately to incorporate
revisions to the same Census release. Changing the ACS release also requires
matching primary estimates/geography; do not mix vintage years.

Race-alone categories include Hispanic residents unless origin is specified.
Never add a parent total to its subcategories. Named districts still use
whole-tract approximations; old partner tracts are not a direct crosswalk.
