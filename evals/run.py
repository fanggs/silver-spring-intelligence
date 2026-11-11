#!/usr/bin/env python3
"""Capture the four fixed API questions. Python standard library only."""
from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = Path(__file__).with_name("questions.json")
DEFAULT_API = "https://civiclens-api-ack0.onrender.com"
REFUSALS = {"I can't answer that from the data I have.", "No data matched that question."}


def now():
    return datetime.now(timezone.utc).isoformat()


def request_json(url, timeout, body=None):
    """Never retry POSTs: a retry could incur another model charge."""
    request = Request(url, data=None if body is None else json.dumps(body).encode(),
                      headers={"Accept": "application/json", "Content-Type": "application/json"})
    started = time.monotonic()
    result = {"started_at": now(), "http_status": None}
    try:
        try:
            response = urlopen(request, timeout=timeout)
        except HTTPError as exc:
            response = exc  # Keep the error body as evidence too.
        with response:
            result["http_status"] = response.code
            raw = response.read().decode("utf-8", errors="replace")
        try:
            result["response"] = json.loads(raw)
        except ValueError:
            result["raw_body"] = raw
            result["error"] = "Response was not JSON."
    except (URLError, OSError, TimeoutError) as exc:
        result["error"] = str(exc)
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result


def classify(result):
    """Structural checks, NOT a factual accuracy or prose quality score."""
    status = result["http_status"]
    if status is None:
        return "transport_error", result.get("error", "No HTTP response.")
    if not 200 <= status < 300:
        return "http_error", f"HTTP {status}"
    payload = result.get("response")
    if not isinstance(payload, dict):
        return "invalid_response", "Expected a JSON object."
    answer = payload.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        return "invalid_response", "Missing answer text."
    # The current API returns HTTP 200 + coverage for both refusals AND
    # model/SQL failures. Do not label a dead API key as a missing dataset.
    if "coverage" in payload:
        if answer in REFUSALS:
            return "refused", "API declined to answer; review data coverage and SQL."
        return "api_error", answer
    if not isinstance(payload.get("sql"), str) or not payload["sql"].strip():
        return "invalid_response", "Answered without SQL."
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) for r in rows):
        return "invalid_response", "Answered without nonempty database rows."
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources or not all(
        isinstance(s, dict) and isinstance(s.get("label"), str) and s["label"].strip()
        and isinstance(s.get("url"), str) and s["url"].strip() for s in sources
    ):
        return "invalid_response", "Answered without source labels and URLs."
    return "answered", "SQL, rows and sources present; human review still required."


def git_metadata():
    try:
        def git(*args):
            return subprocess.check_output(["git", "-C", str(ROOT), *args],
                                           text=True, stderr=subprocess.DEVNULL).strip()
        return {"revision": git("rev-parse", "HEAD"),
                "dirty": bool(git("status", "--porcelain"))}
    except (OSError, subprocess.CalledProcessError):
        return {"revision": None, "dirty": None}


def comparable(result):
    # Keep all API fields, including SQL, citations, rows and highlights.
    # Omit request timestamps and latency to make actual changes legible.
    return {key: result[key] for key in
            ("status", "http_status", "response", "raw_body", "error") if key in result}


def fence(text, language=""):
    ticks = "`" * max(3, max((len(m[0]) + 1 for m in re.finditer(r"`+", text)), default=3))
    return f"{ticks}{language}\n{text}\n{ticks}"


def report(run, previous=None):
    lines = ["# CivicLens four-question run", "", f"Started (UTC): {run['started_at']}",
             f"API: {run['base_url']}", f"Label: {run['label']}",
             f"Deployed release (operator supplied): {run['release'] or 'unknown'}",
             f"Runner checkout: {json.dumps(run['runner_checkout'])}", "",
             "Automated statuses check response structure only. They do not prove numerical "
             "grounding, correctness, a good recommendation, or mobile usability.", "",
             f"Health: {run['health']['status']}", "",
             "| Question | Status | HTTP | Seconds |", "|---|---|---|---|"]
    for r in run["results"]:
        lines.append(f"| {r['id']} | {r['status']} | {r['http_status']} | {r['elapsed_seconds']} |")
    if not run["completed_at"]:
        lines += ["", "Run incomplete; completed requests are saved below."]
    for r in run["results"]:
        lines += ["", f"## {r['id']}: {r['category']}", "", r["question"], "",
                  f"Status: **{r['status']}**. {r['check']}", "", "Answer:", "",
                  fence(str((r.get("response") or {}).get("answer", "No answer received."))
                        if isinstance(r.get("response"), dict) else "No answer received."),
                  "", f"Review: {r['review']}", "", "Full captured response:", "",
                  fence(json.dumps(comparable(r), indent=2, ensure_ascii=False, sort_keys=True), "json")]
    if previous:
        lines += ["", "## Comparison", "", f"Previous run (UTC): {previous['started_at']}",
                  f"Previous API: {previous['base_url']}",
                  f"Previous release: {previous.get('release') or 'unknown'}", "",
                  "Changed text is not automatically a regression. Review the evidence and "
                  "question-specific guidance above."]
        old = {r["id"]: r for r in previous["results"]}
        for r in run["results"]:
            before = old[r["id"]]
            diff = "\n".join(difflib.unified_diff(
                json.dumps(comparable(before), indent=2, ensure_ascii=False, sort_keys=True).splitlines(),
                json.dumps(comparable(r), indent=2, ensure_ascii=False, sort_keys=True).splitlines(),
                fromfile="previous", tofile="current", lineterm=""))
            lines += ["", f"### {r['id']}: {before['status']} → {r['status']}", "",
                      fence(diff, "diff") if diff else "No response changes."]
    return "\n".join(lines) + "\n"


def save(run, directory, previous):
    for name, content in (("run.json", json.dumps(run, indent=2, ensure_ascii=False) + "\n"),
                          ("report.md", report(run, previous))):
        temporary = directory / (name + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(directory / name)


def load_previous(path, questions):
    previous = json.loads(path.read_text(encoding="utf-8"))
    expected = [(q["id"], q["question"]) for q in questions]
    actual = [(r["id"], r["question"]) for r in previous["results"]]
    if previous["format_version"] != 1 or not previous["completed_at"] or actual != expected:
        raise ValueError("Comparison requires a complete version-1 run with the same four questions.")
    return previous


def positive_timeout(value):
    number = float(value)
    if not 0 < number < float("inf"):
        raise argparse.ArgumentTypeError("Timeout must be a finite positive number.")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_API, help="API origin, default: %(default)s")
    parser.add_argument("--label", default="manual", help="Human-readable run label")
    parser.add_argument("--release", help="Deployed commit/tag, if known (never inferred from local HEAD)")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).with_name("runs"))
    parser.add_argument("--compare", type=Path, metavar="RUN_JSON", help="Earlier run.json to diff")
    parser.add_argument("--timeout", type=positive_timeout, default=180, help="Seconds per request (default: 180)")
    args = parser.parse_args(argv)
    base = args.base_url.rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password \
            or parsed.query or parsed.fragment:
        parser.error("Use an HTTP(S) API URL without credentials, query or fragment.")
    questions = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    try:
        previous = load_previous(args.compare, questions) if args.compare else None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(f"Cannot compare: {exc}")
    label = re.sub(r"[^a-zA-Z0-9_-]+", "-", args.label).strip("-")[:60] or "manual"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = args.output_dir.resolve() / f"{stamp}-{label}"
    directory.mkdir(parents=True, exist_ok=False)
    run = {"format_version": 1, "started_at": now(), "completed_at": None,
           "base_url": base, "label": args.label, "release": args.release,
           "runner_checkout": git_metadata(), "timeout_seconds": args.timeout,
           "compare_to": str(args.compare.resolve()) if args.compare else None,
           "health": {"status": "pending"}, "results": []}
    save(run, directory, previous)
    print(f"Saving to {directory}", flush=True)
    try:
        print("Checking /health (allows time for a sleeping service to wake)...", flush=True)
        health = request_json(base + "/health", args.timeout)
        payload = health.get("response")
        health["status"] = "ok" if health["http_status"] == 200 and isinstance(payload, dict) \
            and payload.get("ok") is True else "error"
        run["health"] = health
        save(run, directory, previous)
        for question in questions:
            print(f"Asking {question['id']}...", flush=True)
            result = request_json(base + "/ask", args.timeout, {"question": question["question"]})
            result["status"], result["check"] = classify(result)
            run["results"].append({**question, **result})
            save(run, directory, previous)
            print(f"  {result['status']} (HTTP {result['http_status']}, {result['elapsed_seconds']}s)", flush=True)
        run["completed_at"] = now()
        save(run, directory, previous)
    except KeyboardInterrupt:
        print(f"\nInterrupted; partial results saved to {directory}", file=sys.stderr)
        return 130
    print(f"Report: {directory / 'report.md'}", flush=True)
    statuses = {r["status"] for r in run["results"]}
    if run["health"]["status"] != "ok" or statuses - {"answered", "refused"}:
        return 2
    return 1 if "refused" in statuses else 0


if __name__ == "__main__":
    sys.exit(main())
