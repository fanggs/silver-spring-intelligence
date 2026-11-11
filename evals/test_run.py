"""Exercise the CLI against a local fake API; no model calls or credentials."""
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("eval_runner", Path(__file__).with_name("run.py"))
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def answered(count=76):
    return {"answer": f"There are {count} restaurants.", "sql": "SELECT COUNT(*) FROM businesses",
            "rows": [{"count": count}],
            "sources": [{"label": "OpenStreetMap", "url": "https://www.openstreetmap.org/copyright"}],
            "highlight_geoids": ["24031702502"]}


def refused(message="I can't answer that from the data I have."):
    return {"answer": message, "sql": None, "sources": [], "table": [], "coverage": "Coverage"}


@contextlib.contextmanager
def api(replies):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            status, payload = replies[len(requests) - 1]
            self.send_response(status)
            self.end_headers()
            self.wfile.write(payload.encode() if isinstance(payload, str) else json.dumps(payload).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class HarnessTests(unittest.TestCase):
    def invoke(self, base, directory, *extra):
        with contextlib.redirect_stdout(io.StringIO()):
            return runner.main(["--base-url", base, "--output-dir", str(directory), *extra])

    def test_refusals_are_visible_and_comparison_keeps_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = [(200, answered()), (200, refused()), (200, refused()), (200, answered())]
            with api(baseline) as (base, requests):
                self.assertEqual(self.invoke(base, root / "first", "--label", "baseline"), 1)
            first = next((root / "first").glob("*/run.json"))
            data = json.loads(first.read_text())
            self.assertEqual(requests, [{"question": q["question"]} for q in json.loads(runner.QUESTIONS.read_text())])
            self.assertEqual([r["status"] for r in data["results"]], ["answered", "refused", "refused", "answered"])
            self.assertIsNone(data["release"])
            current = [(200, answered()), (200, answered(10)), (200, answered(20)), (200, answered(77))]
            with api(current) as (base, requests):
                self.assertEqual(self.invoke(base, root / "second", "--compare", str(first)), 0)
            report = next((root / "second").glob("*/report.md")).read_text()
            self.assertIn("q2-ecommerce-panel: refused → answered", report)
            self.assertIn("+    \"answer\": \"There are 77 restaurants.\"", report)
            self.assertIn('"rows"', report)
            self.assertIn('"sources"', report)

    def test_technical_failures_do_not_stop_the_remaining_questions(self):
        replies = [(200, refused("Could not reach the language model: authentication failed")),
                   (503, {"detail": "Unavailable"}), (200, "<html>not JSON</html>"),
                   (200, {"answer": "Unsupported answer", "sql": "SELECT 1", "rows": [{"n": 1}], "sources": []})]
        with tempfile.TemporaryDirectory() as tmp, api(replies) as (base, requests):
            self.assertEqual(self.invoke(base, Path(tmp)), 2)
            data = json.loads(next(Path(tmp).glob("*/run.json")).read_text())
            self.assertEqual(len(requests), 4)
            self.assertIsNotNone(data["completed_at"])
            self.assertEqual([r["status"] for r in data["results"]],
                             ["api_error", "http_error", "invalid_response", "invalid_response"])
            self.assertEqual(data["results"][2]["raw_body"], "<html>not JSON</html>")

    def test_unreachable_api_saves_four_failures(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(runner, "urlopen", side_effect=TimeoutError("timed out")):
            self.assertEqual(self.invoke("http://localhost:1", Path(tmp)), 2)
            data = json.loads(next(Path(tmp).glob("*/run.json")).read_text())
            self.assertEqual(data["health"]["status"], "error")
            self.assertEqual([r["status"] for r in data["results"]], ["transport_error"] * 4)

    def test_interruption_keeps_finished_questions(self):
        outcomes = [{"http_status": 200, "response": {"ok": True}},
                    {"http_status": 200, "response": answered(), "elapsed_seconds": 0.1}, KeyboardInterrupt()]
        with tempfile.TemporaryDirectory() as tmp, patch.object(runner, "request_json", side_effect=outcomes), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.invoke("http://localhost:1", Path(tmp)), 130)
            data = json.loads(next(Path(tmp).glob("*/run.json")).read_text())
            self.assertIsNone(data["completed_at"])
            self.assertEqual(len(data["results"]), 1)

    def test_incompatible_comparison_is_rejected_before_requests(self):
        questions = json.loads(runner.QUESTIONS.read_text())
        previous = {"format_version": 1, "completed_at": "done", "results": questions}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.json"
            previous["results"][0]["question"] = "A different question"
            path.write_text(json.dumps(previous))
            with self.assertRaises(ValueError):
                runner.load_previous(path, json.loads(runner.QUESTIONS.read_text()))

    def test_invalid_rows_are_not_marked_answered(self):
        for rows in ([], None, ["not a row"]):
            payload = answered()
            payload["rows"] = rows
            self.assertEqual(runner.classify({"http_status": 200, "response": payload})[0], "invalid_response")


if __name__ == "__main__":
    unittest.main()
