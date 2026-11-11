"""Report whether the Census Bureau has published a newer ACS 5-year release.

python -m get_data.check_census

The Census data (population, sex, industry, race and ethnicity, income) comes
from the ACS 5-year estimates, published once a year, usually in December.
Swapping in a new release changes headline numbers, so it is not done
automatically: this check only says when one is available, so a person can
run the rebuild and review the four test questions.

The release probe needs no API key. Prints one line and exits 0 either way.
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import closing

import requests

from .config import DATABASE_PATH
from .fetch_businesses import HEADERS


def current_end_year(db_path=DATABASE_PATH):
    with closing(sqlite3.connect(db_path)) as db:
        vintage = db.execute("SELECT acs_vintage FROM place_data LIMIT 1").fetchone()[0]  # "ACS 2020-2024"
    return int(re.findall(r"\d{4}", vintage)[-1])


def newer_release(db_path=DATABASE_PATH, get=requests.get):
    """The newest ACS 5-year end year after ours that the API serves, or None."""
    year, newest = current_end_year(db_path), None
    while True:
        year += 1
        response = get(f"https://api.census.gov/data/{year}/acs/acs5", headers=HEADERS, timeout=30)
        if response.status_code != 200:
            return newest
        newest = year


def main():
    current = current_end_year()
    newest = newer_release()
    if newest:
        print(f"NEW CENSUS RELEASE: ACS {newest - 4}-{newest} is published (database has {current - 4}-{current}). "
              "Rebuild with `python -m get_data.build_database` and review the four test questions.")
    else:
        print(f"Census is current: ACS {current - 4}-{current} is the newest 5-year release.")


if __name__ == "__main__":
    main()
