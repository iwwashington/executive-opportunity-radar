#!/usr/bin/env python3
"""Portable executive-search aggregator.

Designed to run on a schedule (GitHub Actions, cron, or any server) and write a
static JSON feed consumed by the dashboard.  The parser intentionally favors
precision over volume: only senior executive titles are kept.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
SOURCES_PATH = Path(__file__).with_name("sources.json")
OUT_PATH = ROOT / "data" / "jobs.json"
META_PATH = ROOT / "data" / "meta.json"

HEADERS = {
    "User-Agent": "ExecRadar/1.0 (+personal executive opportunity monitor; respectful public-page polling)"
}
TIMEOUT = 30

STRICT_PATTERNS = [
    r"\bchief executive officer\b", r"\bceo\b", r"\bpresident\b",
    r"\bexecutive director\b", r"\bpresident\s*(?:&|and)\s*ceo\b",
    r"\bpresident/chief executive officer\b"
]
BROAD_PATTERNS = STRICT_PATTERNS + [
    r"\bchief operating officer\b", r"\bchief strategy officer\b",
    r"\bchief communications officer\b", r"\bchief growth officer\b",
    r"\bchief administrative officer\b", r"\bchief of staff\b",
    r"\bchief financial officer\b", r"\bchief program officer\b",
    r"\bchief impact officer\b", r"\bexecutive vice president\b",
    r"\bevp\b", r"\bsenior vice president\b", r"\bsvp\b",
    r"\bvice president\b", r"\bmanaging director\b"
]
ROLE_RE = re.compile("|".join(BROAD_PATTERNS), re.I)
STRICT_RE = re.compile("|".join(STRICT_PATTERNS), re.I)

NOISE = re.compile(
    r"privacy|cookie|contact|about|team|services|insights|news|blog|client|submit|general application|"
    r"search consultant|learn more|read more|details|view all|current searches?$",
    re.I,
)

@dataclass
class Job:
    id: str
    title: str
    organization: str
    source: str
    location: str
    url: str
    source_url: str
    tier: str
    discovered_at: str
    compensation_min: int | None = None
    compensation_max: int | None = None
    status: str = "open"
    raw_text: str = ""


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip(" \t\n\r-|–—:")


def stable_id(source: str, title: str, org: str, url: str) -> str:
    raw = "|".join([source.lower(), title.lower(), org.lower(), url.lower()])
    return hashlib.sha1(raw.encode()).hexdigest()[:14]


def classify_tier(title: str) -> str:
    return "strict" if STRICT_RE.search(title) else "broader"


def split_org_title(text: str) -> tuple[str, str]:
    """Best-effort split for strings such as 'Organization - Chief Executive Officer'."""
    text = clean(text)
    # Find where the executive title starts.
    m = ROLE_RE.search(text)
    if not m:
        return "", text
    title = clean(text[m.start():])
    org = clean(text[:m.start()])
    # Remove dangling separators and labels.
    org = re.sub(r"\b(seeks?|is seeking|search for)\b\s*$", "", org, flags=re.I).strip(" -|–—:")
    # If the anchor is just a title, infer organization later from surrounding text.
    if len(org) < 2:
        org = ""
    return org, title


def infer_location(text: str) -> str:
    text = clean(text)
    if re.search(r"\bremote\b", text, re.I):
        return "Remote"
    # Keep this conservative; source pages often include unrelated addresses.
    m = re.search(r"\b([A-Z][A-Za-z .'-]+),\s*([A-Z]{2})\b", text)
    return clean(m.group(0)) if m else ""


def candidate_from_anchor(a, source: dict, now: str) -> Job | None:
    anchor_text = clean(a.get_text(" ", strip=True))
    parent = a.find_parent(["article", "li", "div", "section"]) or a.parent
    context = clean(parent.get_text(" ", strip=True)) if parent else anchor_text

    # Some list pages put the role in a heading next to a generic "Details" link.
    combined = anchor_text
    if not ROLE_RE.search(combined) and ROLE_RE.search(context):
        # Pull a nearby heading first; otherwise use a small context window.
        heading = parent.find(["h1", "h2", "h3", "h4", "h5"]) if parent else None
        if heading and ROLE_RE.search(heading.get_text(" ", strip=True)):
            combined = clean(heading.get_text(" ", strip=True))
        elif len(context) <= 500:
            combined = context

    if not ROLE_RE.search(combined):
        return None
    if NOISE.search(anchor_text) and not ROLE_RE.search(anchor_text):
        # Generic link is allowed only when nearby context is compact enough to parse.
        if len(context) > 500:
            return None

    org, title = split_org_title(combined)
    if not ROLE_RE.search(title):
        return None

    # Trim title after likely sentence/punctuation noise.
    title = re.split(r"\s{2,}|\.(?:\s|$)|\b(?:Learn More|Details|Read More)\b", title, maxsplit=1, flags=re.I)[0]
    title = clean(title)
    if len(title) > 130:
        # Keep just the role phrase when a whole paragraph was captured.
        m = ROLE_RE.search(title)
        if not m:
            return None
        end = min(len(title), m.end() + 70)
        title = clean(title[m.start():end])

    # Infer organization from context if title-only anchor.
    if not org:
        before = context.split(title, 1)[0] if title in context else ""
        before = clean(before)
        if before and len(before) <= 140:
            org = before
    if not org:
        org = "Organization not parsed"

    href = a.get("href") or source["url"]
    url = urljoin(source["url"], href)
    if urlparse(url).scheme not in {"http", "https"}:
        url = source["url"]

    return Job(
        id=stable_id(source["name"], title, org, url),
        title=title,
        organization=org,
        source=source["name"],
        location=infer_location(context),
        url=url,
        source_url=source["url"],
        tier=classify_tier(title),
        discovered_at=now,
        raw_text=context[:600],
    )


def scrape_source(source: dict, now: str) -> tuple[list[Job], dict]:
    health = {"source": source["name"], "url": source["url"], "ok": False, "count": 0, "error": ""}
    try:
        r = requests.get(source["url"], headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        jobs: dict[str, Job] = {}
        for a in soup.find_all("a", href=True):
            job = candidate_from_anchor(a, source, now)
            if job:
                jobs[job.id] = job
        health["ok"] = True
        health["count"] = len(jobs)
        return list(jobs.values()), health
    except Exception as e:
        health["error"] = f"{type(e).__name__}: {e}"[:300]
        return [], health


def load_existing() -> list[dict]:
    if not OUT_PATH.exists():
        return []
    try:
        payload = json.loads(OUT_PATH.read_text())
        return payload.get("jobs", []) if isinstance(payload, dict) else payload
    except Exception:
        return []


def main() -> int:
    sources = json.loads(SOURCES_PATH.read_text())
    now = datetime.now(timezone.utc).isoformat()
    found: dict[str, Job] = {}
    health = []

    for source in sources:
        if not source.get("enabled", True):
            continue
        jobs, h = scrape_source(source, now)
        health.append(h)
        print(f"{source['name']}: {h['count']} {'OK' if h['ok'] else 'FAILED'}")
        for j in jobs:
            found[j.id] = j

    # Preserve prior open records for failed sources so a temporary network/site issue
    # does not wipe the dashboard. Mark them stale in metadata rather than deleting.
    failed_sources = {h["source"] for h in health if not h["ok"]}
    for old in load_existing():
        if old.get("source") in failed_sources and old.get("id") not in found:
            try:
                found[old["id"]] = Job(**{k: old.get(k) for k in Job.__dataclass_fields__})
            except Exception:
                pass

    jobs_out = [asdict(j) for j in found.values()]
    jobs_out.sort(key=lambda j: (0 if j["tier"] == "strict" else 1, j["source"], j["organization"], j["title"]))

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps({"generated_at": now, "jobs": jobs_out}, indent=2, ensure_ascii=False))
    META_PATH.write_text(json.dumps({"generated_at": now, "sources": health}, indent=2, ensure_ascii=False))
    print(f"Wrote {len(jobs_out)} roles to {OUT_PATH}")
    return 0 if any(h["ok"] for h in health) else 2


if __name__ == "__main__":
    raise SystemExit(main())
