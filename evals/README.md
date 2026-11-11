# Four-question release check

Run the four fixed stakeholder questions against `/ask` before and after a
release or model/prompt change. The harness observes the public API; it does
not import the server, need an API key, or change the answer pipeline.
Python 3.10+ is sufficient, with no extra packages.

From the repo root:

```bash
python3 evals/run.py --label baseline
```

This defaults to `https://civiclens-api-ack0.onrender.com`. For a local API:

```bash
python3 evals/run.py --base-url http://127.0.0.1:8000 --label local
```

Each run creates `evals/runs/<UTC-timestamp>-<label>/` containing:

- `run.json`: full API responses, exact questions, HTTP codes, timings,
  health response, local checkout revision/dirty flag, and run metadata.
- `report.md`: answers, manual review guidance, full evidence and, when
  requested, a per-question diff of statuses and all response fields.

Capture again after a release and compare with the actual earlier file:

```bash
python3 evals/run.py --label after-change --release DEPLOYED_COMMIT \
  --compare evals/runs/BASELINE_DIRECTORY/run.json
```

Replace the uppercase placeholders. `--release` is optional and supplied by
the operator: the runner cannot know what commit a remote service deployed.
Its local checkout metadata is recorded separately. A comparison requires a
completed run with the same four question IDs and exact question text.
Different APIs/releases are allowed and identified in the report.

Runs are ignored by git. Preserve chosen baseline folders somewhere stable
or deliberately add a reviewed fixture to the repo. Do not edit
`questions.json` when experimenting with prompts: the questions must stay
fixed for comparisons to mean anything.

## Statuses and exit codes

| Exit | Meaning |
|---|---|
| 0 | All four returned answers with nonempty SQL, database rows and source labels/URLs; health was OK. |
| 1 | At least one refusal, with no technical failures. |
| 2 | HTTP/transport error, malformed response, API-reported model/query failure, or failed health check. Also used for invalid CLI/comparison input. |
| 130 | Interrupted; completed requests remain saved in an incomplete run. |

The current API reports refusals and model/query errors using HTTP 200.
The harness distinguishes its known refusal messages from error responses;
an unknown `coverage` response is conservatively marked `api_error`.
The initial baseline's two data-gap refusals therefore return exit 1. They
are visible limitations, not waived passes. Later ingestion may change them
to answered without any harness changes.

**An `answered` status is a structural check, not proof of grounding or answer
quality.** Review the SQL, raw rows, sources, geography and prose together.
Use the question-specific guidance to judge relevance and clarity. No fixed
restaurant total is asserted because the dataset can change. The harness
cannot assess visuals or mobile layout; review those in the frontend.

## Request behavior

One health request first, then four sequential POSTs. The default timeout is
180 seconds per request to allow the free-tier service to wake; override it
with `--timeout SECONDS`. Even if health or a question fails, all four are
attempted and saved. No automatic retries: each POST can trigger up to two
billable model calls. Responses are saved after each request, so a later
failure or interruption doesn't discard earlier answers.

To verify the harness without model calls:

```bash
python3 -m unittest discover -s evals -p 'test_*.py' -v
```
