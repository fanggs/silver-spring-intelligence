"""Weekly data refresh: everything that can be refreshed safely and for free.

python -m get_data.refresh [--db PATH] [--summary FILE]

1. County records (crime, building permits, pedestrian-struck dispatches)
   from Montgomery County open data, which updates daily. Replaced only if
   every check in refresh_county passes.
2. Place details (street address, opening hours, website, phone) for every
   mapped place, from OpenStreetMap. The slow building-outline step runs for
   Silver Spring only and is skipped quickly if Overpass is down.
3. Census: reports when a newer ACS 5-year release exists. Not applied
   automatically, because it changes headline numbers; a person rebuilds.

Each step runs on its own; one failing never blocks the others, and a step
that fails leaves its data exactly as it was. The summary (Markdown) says
what changed. Exit code 1 means a step needs a person to look (for example
the county changed a field), not that data was damaged.
Costs nothing: public APIs, no keys, no model calls.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import traceback

from .config import DATABASE_PATH


def run(db_path=DATABASE_PATH):
    lines, needs_attention = ["## Weekly data refresh", ""], False

    lines.append("### County records (Montgomery County open data)")
    try:
        from .refresh_county import refresh
        report = refresh(db_path)
        for source_id, result in report.items():
            if not isinstance(result, dict):
                continue
            change = result["rows_after"] - result["rows_before"]
            if result["problems"]:
                status = "kept as before: " + "; ".join(result["problems"])
            elif not report.get("replaced"):
                status = "kept as before, because another county table failed its checks"
            else:
                status = "updated"
            lines.append(f"- **{source_id}**: {result['rows_before']:,} -> {result['rows_after']:,} rows "
                         f"({change:+,}), newest record {result['last_before']} -> {result['last_after']}; "
                         f"{result['same_share']:.1%} of existing records unchanged. {status}.")
            needs_attention |= bool(result["problems"])
    except Exception as error:
        needs_attention = True
        lines.append(f"- Failed, county tables kept as before: `{type(error).__name__}: {error}`")
        traceback.print_exc()

    lines += ["", "### Place details (OpenStreetMap: address, hours, website, phone)"]
    try:
        from .fetch_addresses import build_locations
        counts = build_locations(db_path)
        lines.append(f"- {counts['places']:,} places: {counts['listing'] + counts['building']:,} with a street address "
                     f"({counts['building']:,} from the building they are in), {counts['no_address']:,} without.")
        if counts.get("building_lookup_skipped"):
            lines.append(f"- Building lookup skipped for {counts['building_lookup_skipped']:,} places (Overpass busy); "
                         "places not found on an earlier run stay without an address until a later run reaches them.")
    except Exception as error:
        lines.append(f"- Failed, place details kept as before: `{type(error).__name__}: {error}`")
        traceback.print_exc()

    lines += ["", "### Census"]
    try:
        from .check_census import current_end_year, newer_release
        current, newest = current_end_year(db_path), newer_release(db_path)
        if newest:
            needs_attention = True
            lines.append(f"- **New release: ACS {newest - 4}-{newest}** (database has {current - 4}-{current}). "
                         "Rebuild with `python -m get_data.build_database` and review the four test questions.")
        else:
            lines.append(f"- Current: ACS {current - 4}-{current} is the newest 5-year release.")
    except Exception as error:
        lines.append(f"- Check failed: `{type(error).__name__}: {error}`")
    return "\n".join(lines) + "\n", needs_attention


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DATABASE_PATH)
    parser.add_argument("--summary", type=Path, help="Also write the Markdown summary to this file")
    args = parser.parse_args()
    summary, needs_attention = run(args.db)
    print(summary)
    if args.summary:
        args.summary.write_text(summary)
    raise SystemExit(1 if needs_attention else 0)


if __name__ == "__main__":
    main()
