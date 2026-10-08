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
                expected_nav = ["market", "digest", "opportunities", "checkyourself", "saved", "firms", "placements", "sources"]
                actual_nav = desktop.locator(".nav button[data-view]").evaluate_all(
                    "(buttons) => buttons.map(button => button.dataset.view)"
                )
                assert actual_nav == expected_nav, f"Unexpected navigation order: {actual_nav}"
                for view in expected_nav:
                    desktop.locator(f'.nav button[data-view="{view}"]').click()
                    assert desktop.locator(f'#{view}View.active').count() == 1, f"View {view} did not open"
                meta = json.loads((ROOT / "meta.json").read_text(encoding="utf-8"))
                # The Firms directory stays clean: no large cannot-auto-check boxes,
                # while source-status labels still identify checks needing attention.
                assert desktop.locator("#firmList .blocked-banner").count() == 0, (
                    "Firms still contains large blocked-source warning boxes"
                )
                assert desktop.locator("#firmList .firm-blocked").count() == 0, (
                    "Firms still applies oversized warning card styling"
                )
                needs_attention = any(
                    src.get("status") in ("blocked", "failed", "partial-suspected")
                    for src in meta.get("sources", [])
                )
                if needs_attention:
                    assert desktop.locator("#firmList .source-status.s-blocked").count() > 0, (
                        "Firms lost compact source-status labels"
                    )

                check_types = {
                    "attention": {"blocked", "failed", "partial-suspected"},
                    "manual": {"manual", "auto", "awaiting-first-run"},
                    "noboard": {"no-public-list"},
                }
                expected = {
                    kind: sum(
                        1 for s in meta["sources"]
                        if s.get("status") in states
                        and s.get("mode") != "legacy"
                    ) for kind, states in check_types.items()
                }
                expected_total = sum(expected.values())
                desktop.locator('.nav button[data-view="checkyourself"]').click()
                assert desktop.locator("#checkyourselfView.active").count() == 1
                assert desktop.locator("#checkList .check-card").count() == expected_total, (
                    f"Check Yourself should list {expected_total} coverage gaps"
                )
                assert desktop.locator("#checkList .check-card a[href^='https://']").count() == expected_total, (
                    "Every listed firm should link to its source website"
                )
                assert desktop.locator("#checkSummary .mini-stat b").first.inner_text() == str(
                    expected["attention"] + expected["manual"]
                ), "Direct-check counter mismatch"
                for kind, count in expected.items():
                    desktop.locator("#checkCategory").select_option(kind)
                    assert desktop.locator("#checkList .check-card").count() == count, (
                        f"Filter {kind} should display {count} sources"
                    )
                desktop.locator("#checkCategory").select_option("")
                desktop.locator("#checkSearch").fill("unlikely-radar-firm-00000")
                assert desktop.locator("#checkList .check-card").count() == 0, "Search filter is broken"
                desktop.locator("#checkSearch").fill("")
                assert desktop.locator("#checkList .check-card").count() == expected_total
                if expected["noboard"]:
                    desktop.locator("#checkCategory").select_option("noboard")
                    assert desktop.locator("#checkList a").first.inner_text().startswith(
                        "Visit firm website"
                    ), "No-public-list sources must not be presented as verified openings"
                    desktop.locator("#checkCategory").select_option("")
                print(
                    f"PASS: Check Yourself shows {expected_total} firm sources with "
                    "accurate status counts, usable links, and working filters"
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

                mobile.locator('.nav button[data-view="checkyourself"]').click()
                assert mobile.locator("#checkyourselfView.active").count() == 1
                assert mobile.locator("#checkList .check-card").count() == expected_total
                assert mobile.locator("#checkList .check-card a").first.is_visible()
                assert mobile.locator("#checkSearch").is_visible()
                assert mobile.locator("#checkCategory").is_visible()
                print("PASS: Check Yourself works on mobile")

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
