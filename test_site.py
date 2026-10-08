#!/usr/bin/env python3
"""Fail fast when the radar HTML parses but cannot actually render.

Run locally: python test_site.py
Requires Node.js, playwright, and a Playwright Chromium installation.
"""
import json
import re
import subprocess
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def check_js_syntax():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S | re.I)
    assert len(scripts) == 1, f"Expected one inline script; got {len(scripts)}"
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "radar-inline.js"
        path.write_text(scripts[0], encoding="utf-8")
        subprocess.run(["node", "--check", str(path)], check=True)
    print("PASS: JavaScript syntax")


def run_browser_test():
    handler = partial(QuietHandler, directory=str(ROOT))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            try:
                desktop = browser.new_page(viewport={"width": 1280, "height": 860})
                errors = []
                desktop.on("pageerror", lambda error: errors.append(str(error)))
                desktop.goto(url, wait_until="load")
                desktop.wait_for_function(
                    "() => !document.querySelector('#lastRefresh').textContent.includes('Loading')",
                    timeout=20000,
                )
                desktop.wait_for_function(
                    "() => document.querySelectorAll('#firmList .source-card').length > 0",
                    timeout=20000,
                )
                for view in ["market", "digest", "opportunities", "saved", "firms", "placements", "sources"]:
                    desktop.locator(f'.nav button[data-view="{view}"]').click()
                    assert desktop.locator(f'#{view}View.active').count() == 1, f"View {view} did not open"
                meta = json.loads((ROOT / "meta.json").read_text(encoding="utf-8"))
                flagged = any(s.get("status") in ("blocked", "failed", "partial-suspected", "manual")
                              for s in meta.get("sources", []))
                if flagged:
                    assert desktop.locator("#firmList .blocked-banner").count() > 0, (
                        "Blocked-source warning missing from Firms"
                    )
                assert not errors, f"JavaScript errors on desktop: {errors}"
                print("PASS: Desktop loads data, Firms displays, warnings exist, and all tabs open")

                mobile = browser.new_page(viewport={"width": 390, "height": 844})
                mobile_errors = []
                mobile.on("pageerror", lambda error: mobile_errors.append(str(error)))
                mobile.goto(url, wait_until="load")
                mobile.wait_for_function(
                    "() => document.querySelectorAll('#firmMatrix .fmm-row').length > 0",
                    timeout=20000,
                )
                mobile_display = mobile.locator("#firmMatrix .firm-matrix-mobile").evaluate(
                    "(element) => getComputedStyle(element).display"
                )
                table_display = mobile.locator("#firmMatrix table").evaluate(
                    "(element) => getComputedStyle(element).display"
                )
                assert mobile_display != "none", "Mobile firm matrix is hidden"
                assert table_display == "none", "Desktop matrix is still displayed on mobile"
                assert not mobile_errors, f"JavaScript errors on mobile: {mobile_errors}"
                print("PASS: Mobile loads and shows the compact firm matrix")
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    check_js_syntax()
    run_browser_test()
    print("PASS: Radar frontend smoke test complete")
