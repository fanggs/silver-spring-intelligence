# Hosted review corrections

These corrections follow the four-question hosted review on November 11, 2026.
They preserve the installed data and existing model-based question path.

## Corrected behavior

- SQLite literals, quoted identifiers and comments are masked before checking
  statement separators and forbidden tokens. The SQLite parser, read-only
  connection, and authorizer still reject writes, multiple executable
  statements and external file/extension functions. The original e-commerce
  query now executes unchanged, including semicolons inside its text labels.
- Answers carry geography metadata. Official Census place estimates highlight
  the existing official boundary, not the tract containing a place-name point.
  Mixed place/tract evidence retains its separate source notes. Exact
  self-identified gender is explicitly distinguished from Census sex data.
- The map API exposes the stored business category_group. The frontend uses
  that definition for food/retail grouping, eliminating the 78-versus-82
  food-and-drink discrepancy. Other amenity subgroups remain available.
- The one-column layout changes its named grid areas at the same breakpoint.
  Answers appear first on desktop and mobile; map categories are expandable.
  Single returned counts have a number card, and essential scope/measure
  warnings remain visible with full methodology underneath.
- Simplified support tables coexist with every returned raw field. Long
  strings are shortened only in the compact table. Original raw values are
  retained. Simple business count queries also expose up to 200 matching
  listings, using the same filter, with an explicit truncation notice when
  appropriate. Complex comparisons retain their original complete result rows.
- Prompt instructions request relevant evidence and prohibit unsupported
  foot-traffic/demand proxies. Legacy proxy labels are removed from compact
  metric labels and accompanied by an explicit warning; raw evidence is not
  rewritten. Missing venue capacity or commercial demand is never invented.

## Latency and cost

Unambiguous single business counts use a result-derived sentence instead of
a second model call. A bounded in-memory cache retains successful HTTP answers
for five minutes, keyed by exact question, model and database file identity,
size and nanosecond modification time. Database replacement invalidates those
keys. Failures and empty answers are not cached. This is intended for the
existing immutable SQLite snapshot workflow, not a live WAL ingestion service.
The cache holds at most 128 entries per process and is cleared on restart.

Send Cache-Control: no-cache to obtain fresh model output. The evaluation
harness always sends that header, so cached answers cannot disguise model
changes. Server-Timing separates model calls, database execution and total
request time. X-CivicLens-Cache reports HIT/MISS. X-CivicLens-Revision uses
RENDER_GIT_COMMIT when available, otherwise local. No model or pricing was
changed. Cold model latency is external and is not guaranteed below two seconds.

## Verification

Run:

```bash
python -m unittest discover -s evals -p 'test_*.py' -v
python -m get_data.validate_database
node --check page/script.js
```

All 39 tests pass. Thirteen new regression tests cover the captured event-query
failure, actual stacked statements/writes, literal/comment handling, official
place map scope, mixed-source scope, matching records, readable support data,
category consistency, repeat-request reuse, fresh evaluation, expiry/capacity,
and database replacement. Existing tests continue to pass.

Browser checks exercised all four captured SQL inputs against the fixed local
API and actual database, with clearly marked offline provider summaries for
the two planning questions. Each answer was checked at 320, 390, 760, 768, 820,
900, 901 and 1440 pixels: map widths remained positive and page width did not
overflow. Full returned rows stayed available; the restaurant lookup exposed
43 matching records. The sex estimate used boundary scope and no selected
tract. Screenshots and responses are retained in the local review artifacts.

## Release limits

No model key is available locally. Captured-query replay proves the repaired
execution/rendering paths, not fresh model SQL selection or prose quality.
After deploying with the existing hosted credentials, run evals/run.py against
the deployed API, verify the revision header and inspect all four answers.
Prompt changes must be reviewed with fresh model output before claiming that
the live answer-quality and cold-latency goals are met.

The existing database still does not establish actual customer demand,
observed pedestrian traffic, precise agreed Fenton district limits, local
workplace employment, self-identified gender counts, or verified venue
capacity/bookability. Partner source dates remain unconfirmed. These are
explicit coverage limits, not gaps that can honestly be filled by code.
