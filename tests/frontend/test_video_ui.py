"""Behaviour tests for the Phase 9 video UI, run in a REAL headless browser against the REAL page.

The harness (``harness.js``) loads ``frontend/index.html`` in an iframe, replaces ``fetch`` with a mock that returns the
Phase 8 response shapes, drives the real UI (selecting files, clicking buttons, pressing keys) and reports one result per
scenario. No backend, no Gemini and no network call to the analysis service is involved. The browser still downloads the
page's CDN libraries (Tailwind, Chart.js, Lucide), so these tests are skipped when no browser is installed or the page
cannot initialise (for example offline).
"""
from __future__ import annotations

import functools
import json
import mimetypes
import re
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
HARNESS_DIR = Path(__file__).resolve().parent
BROWSER_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]
mimetypes.add_type("text/javascript", ".js")


def find_browser() -> str | None:
    for name in ("chrome", "google-chrome", "chromium", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return next((p for p in BROWSER_CANDIDATES if Path(p).is_file()), None)


class Handler(BaseHTTPRequestHandler):
    """Serves the real frontend files plus the harness. Every /api/ request answers 404 (the harness mocks fetch)."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        target = None
        if path in ("/", "/index.html"):
            target = FRONTEND / "index.html"
        elif path.startswith(("/js/", "/css/")):
            target = FRONTEND / path.lstrip("/")
        elif path.startswith("/__tests__/"):
            name = path[len("/__tests__/"):]
            target = HARNESS_DIR / ("harness.html" if name == "harness.html" else name)
        if target is None or not target.is_file() or ".." in path:
            body = b'{"detail":"Not Found","code":"NOT_FOUND"}'
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
        else:
            body = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(str(target))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def harness_results():
    browser = find_browser()
    if browser is None:
        pytest.skip("no Chrome/Edge found for the browser behaviour tests")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    profile = tempfile.mkdtemp(prefix="scamshield-ui-test-")
    try:
        completed = subprocess.run(
            [browser, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-extensions",
             f"--user-data-dir={profile}", "--virtual-time-budget=600000", "--dump-dom", f"http://127.0.0.1:{port}/__tests__/harness.html"],
            capture_output=True, timeout=420, text=True, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        pytest.fail("the browser harness did not finish in time")
    finally:
        server.shutdown()
        shutil.rmtree(profile, ignore_errors=True)
    match = re.search(r'<pre id="results">(.*?)</pre>', completed.stdout, re.S)
    if not match:
        pytest.fail(f"the harness produced no results. stderr: {completed.stderr[-500:]}")
    text = match.group(1).replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    try:
        data = json.loads(text)
    except ValueError:
        pytest.fail(f"the harness result was not JSON: {text[:300]}")
    if not data.get("done"):
        pytest.fail("the harness did not complete every scenario within the time budget")
    if any(r.get("init_failed") for r in data["results"]):
        pytest.skip("the real page could not initialise in the headless browser (CDN scripts unreachable?)")
    return {r["name"]: r for r in data["results"]}


SCENARIOS = re.findall(r"^test\('([^']+)'", (HARNESS_DIR / "harness.js").read_text(encoding="utf-8"), re.M)


def test_the_harness_lists_the_expected_scenarios():
    assert len(SCENARIOS) >= 20 and len(set(SCENARIOS)) == len(SCENARIOS)


@pytest.mark.parametrize("name", SCENARIOS)
def test_video_ui_scenario(harness_results, name):
    result = harness_results.get(name)
    assert result is not None, f"scenario {name!r} did not run"
    assert result["ok"], result.get("detail")
