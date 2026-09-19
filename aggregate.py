#!/usr/bin/env python3
"""Executive Opportunity Radar updater.

Reads the source list from sources.json, checks public search pages, keeps only
CEO / President / Executive Director roles, and writes jobs.json + meta.json.

Design goals:
- One broken source never wipes the whole dashboard.
- Posted dates are used only when a source actually exposes a date.
- When no reliable posted date exists, first_seen is preserved and clearly
  labeled as such in the UI rather than inventing a date.
- Known posted dates older than six months are excluded.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
SOURCES_PATH = ROOT / "sources.json"
JOBS_PATH = ROOT / "jobs.json"
META_PATH = ROOT / "meta.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 Chrome/124 Safari/537.36 ExecOpportunityRadar/2.0"
    )
}
TIMEOUT = 30
MAX_DETAIL_REQUESTS_PER_SOURCE = 15
SIX_MONTH_DAYS = 183

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6,
    "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9,
    "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

NOISE = re.compile(
    r"^(?:apply|apply now|learn more|read more|details|click here|full description|"
    r"view all|current searches?|open searches?|search jobs?|job search|careers|"
    r"opportunities|submit resume|join our|about|contact|services|home)$",
    re.I,
)
LOCATIONISH = re.compile(
    r"\b(remote|hybrid|onsite|on-site|anywhere|[A-Z][A-Za-z .'-]+,\s*[A-Z]{2}\b)\b",
    re.I,
)


def clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip(" \t\n\r|–—-")


def normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clean(value).lower()).strip()


def is_target_role(title: str) -> bool:
    """Match top-executive titles without treating Vice President as President."""
    t = clean(title)
    tl = t.lower()
    if not t or len(t) > 180:
        return False
    if re.search(r"\bchief executive officer\b|\bceo\b", t, re.I):
        return True
    if re.search(r"\bexecutive director\b", t, re.I):
        # Exclude functional fundraising/academic titles such as
        # "Executive Director of Annual Giving".
        if re.search(r"\bexecutive director\s+of\b", t, re.I):
            return False
        if re.search(r"\binterim\s+executive director\b", t, re.I):
            return False
        return True
    # President must be the actual role, not vice/associate/assistant president.
    if re.match(r"^(?:global\s+)?president(?:\b|\s*[&/]\s*|\s+and\s+)", t, re.I):
        return True
    if re.search(r"\bpresident\s*(?:&|/|and)\s*(?:chief executive officer|ceo)\b", t, re.I):
        return True
    # "Executive Director and President" is already caught above.
    return False


def stable_id(source: str, title: str, organization: str, url: str) -> str:
    raw = "|".join([
        normalized(source), normalized(title), normalized(organization),
        (url or "").split("#", 1)[0].lower(),
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def split_title_org(text: str) -> tuple[str, str]:
    """Extract a clean role and organization when both appear in one string."""
    s = clean(text)

    # Nonprofit HR style: "Organization: Executive Director"
    if ":" in s:
        left, right = [clean(x) for x in s.split(":", 1)]
        if is_target_role(right) and not is_target_role(left):
            return right, left

    # DRiWaterstone style: "Chief Executive Officer – KleinLife"
    for sep in (" – ", " — ", " - "):
        if sep in s:
            left, right = [clean(x) for x in s.split(sep, 1)]
            if is_target_role(left) and not is_target_role(right):
                return left, right
            if is_target_role(right) and not is_target_role(left):
                return right, left

    # Kittleman style: "President & CEO, FIND Food Bank"
    if "," in s:
        left, right = [clean(x) for x in s.split(",", 1)]
        if is_target_role(left) and not is_target_role(right):
            return left, right
        if is_target_role(right) and not is_target_role(left):
            return right, left

    return s, ""


def likely_org(line: str, role: str) -> bool:
    s = clean(line)
    if not s or s == role or len(s) > 130 or len(s) < 2:
        return False
    if NOISE.match(s) or is_target_role(s):
        return False
    if LOCATIONISH.search(s) and len(s.split()) <= 8:
        return False
    if re.search(r"\bposted\b|\bdate posted\b|\bsalary\b|\bcompensation\b", s, re.I):
        return False
    if s.startswith("#"):
        return False
    return True


def local_container(anchor):
    node = anchor
    best = anchor.parent
    for _ in range(6):
        node = getattr(node, "parent", None)
        if node is None or getattr(node, "name", None) in {"body", "html"}:
            break
        text = clean(node.get_text(" ", strip=True))
        if 20 <= len(text) <= 1800:
            best = node
        elif len(text) > 1800:
            break
    return best


def context_lines(anchor) -> list[str]:
    container = local_container(anchor)
    lines = []
    for x in container.stripped_strings:
        c = clean(x)
        if c and (not lines or c != lines[-1]):
            lines.append(c)
    return lines


def infer_organization(role: str, lines: list[str]) -> str:
    # First check whether the role line itself contains organization data.
    parsed_role, parsed_org = split_title_org(role)
    if parsed_org:
        return parsed_org

    # Find the role line and inspect nearby labels on both sides.
    idx = None
    for i, line in enumerate(lines):
        if normalized(role) == normalized(line) or normalized(role) in normalized(line):
            idx = i
            break
    if idx is not None:
        for offset in (-1, 1, -2, 2, -3, 3):
            j = idx + offset
            if 0 <= j < len(lines) and likely_org(lines[j], role):
                return lines[j]

    joined = " ".join(lines[:12])
    patterns = [
        r"(?:on behalf of|partnering with|retained by)\s+(?:our client,?\s+)?([A-Z][^.;]{2,90})",
        r"([A-Z][A-Za-z0-9&'’., -]{2,90})\s+(?:seeks|is seeking|has retained)",
    ]
    for pattern in patterns:
        m = re.search(pattern, joined)
        if m:
            org = clean(m.group(1))
            if likely_org(org, role):
                return org
    return "Organization not parsed"


def infer_location(lines: list[str]) -> str:
    for line in lines:
        s = clean(line)
        if re.search(r"\bremote\b", s, re.I):
            if len(s) <= 100:
                return s
            return "Remote"
        m = re.search(r"\b([A-Z][A-Za-z .'-]+),\s*([A-Z]{2})\b", s)
        if m:
            return clean(m.group(0))
        if re.search(r"\b(hybrid|onsite|on-site)\b", s, re.I) and len(s) <= 100:
            return s
    return ""


def safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def extract_date(text: str, today: date) -> date | None:
    s = clean(text)
    if not s:
        return None

    # ISO yyyy-mm-dd
    m = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", s)
    if m:
        return safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # mm/dd/yy or mm/dd/yyyy
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", s)
    if m:
        y = int(m.group(3))
        if y < 100:
            y += 2000
        return safe_date(y, int(m.group(1)), int(m.group(2)))

    # Month d, yyyy / Mon d yyyy
    month_names = "|".join(sorted(MONTHS, key=len, reverse=True))
    m = re.search(rf"\b({month_names})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,)?\s+(20\d{{2}})\b", s, re.I)
    if m:
        return safe_date(int(m.group(3)), MONTHS[m.group(1).lower().rstrip(".")], int(m.group(2)))

    # Explicit relative posting ages.
    m = re.search(r"\bposted\s+(\d+)\s+(hour|day|week|month)s?\s+ago\b", s, re.I)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        days = 0 if unit == "hour" else n if unit == "day" else n * 7 if unit == "week" else n * 30
        return today - timedelta(days=days)
    if re.search(r"\bposted\s+(?:today|just now)\b", s, re.I):
        return today
    if re.search(r"\bposted\s+yesterday\b", s, re.I):
        return today - timedelta(days=1)

    return None


def fetch(url: str) -> requests.Response:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
    r.raise_for_status()
    return r


def valid_url(url: str) -> bool:
    try:
        return urlparse(url).scheme in {"http", "https"}
    except Exception:
        return False


@dataclass
class Job:
    id: str
    title: str
    organization: str
    source: str
    location: str
    url: str
    source_url: str
    posted_date: str | None
    first_seen: str
    last_seen: str
    date_basis: str
    status: str = "open"


def candidate_from_anchor(anchor, source: dict, today: date, detail_budget: list[int]) -> Job | None:
    raw_anchor = clean(anchor.get_text(" ", strip=True))
    role, org_from_title = split_title_org(raw_anchor)

    if not is_target_role(role):
        # Some sites use a generic Details link with the actual role in a nearby heading.
        container = local_container(anchor)
        heading = container.find(["h1", "h2", "h3", "h4", "h5", "h6"]) if container else None
        if heading:
            heading_text = clean(heading.get_text(" ", strip=True))
            role2, org2 = split_title_org(heading_text)
            if is_target_role(role2):
                role, org_from_title = role2, org2
        if not is_target_role(role):
            return None

    role = clean(role)
    lines = context_lines(anchor)
    organization = org_from_title or infer_organization(role, lines)
    location = infer_location(lines)
    context = " | ".join(lines[:30])
    use_source_dates = source.get("date_policy", "source") != "first_seen"
    posted = extract_date(context, today) if use_source_dates else None

    href = anchor.get("href") or source["url"]
    url = urljoin(source["url"], href)
    if not valid_url(url):
        url = source["url"]

    # If list page has no date, check the detail page where practical.
    if use_source_dates and posted is None and detail_budget[0] > 0 and url != source["url"] and not url.lower().endswith(".pdf"):
        try:
            detail_budget[0] -= 1
            time.sleep(0.12)
            dr = fetch(url)
            dsoup = BeautifulSoup(dr.text, "lxml")
            dtext = clean(dsoup.get_text(" ", strip=True))[:12000]
            posted = extract_date(dtext, today)
            if organization == "Organization not parsed":
                # Common detail-page phrasing.
                for pattern in (
                    r"on behalf of (?:our client,?\s+)?([A-Z][^.;]{2,90})",
                    r"([A-Z][A-Za-z0-9&'’., -]{2,90})\s+(?:seeks|is seeking)\s+(?:an?|its next|a new)\s+" + re.escape(role),
                ):
                    m = re.search(pattern, dtext, re.I)
                    if m:
                        candidate = clean(m.group(1))
                        if likely_org(candidate, role):
                            organization = candidate
                            break
        except Exception:
            pass

    now_iso = datetime.now(timezone.utc).isoformat()
    return Job(
        id=stable_id(source["name"], role, organization, url),
        title=role,
        organization=organization,
        source=source["name"],
        location=location,
        url=url,
        source_url=source["url"],
        posted_date=posted.isoformat() if posted else None,
        first_seen=now_iso,
        last_seen=now_iso,
        date_basis="posted" if posted else "first_seen",
    )


def dedupe(jobs: Iterable[Job]) -> list[Job]:
    out: dict[tuple[str, str, str], Job] = {}
    for j in jobs:
        key = (normalized(j.source), normalized(j.title), normalized(j.organization))
        old = out.get(key)
        if not old:
            out[key] = j
        elif (not old.posted_date and j.posted_date) or (old.organization == "Organization not parsed" and j.organization != old.organization):
            out[key] = j
    return list(out.values())


def load_existing() -> list[dict]:
    if not JOBS_PATH.exists():
        return []
    try:
        raw = json.loads(JOBS_PATH.read_text(encoding="utf-8"))
        return raw.get("jobs", []) if isinstance(raw, dict) else raw
    except Exception:
        return []


def prior_lookup(existing: list[dict]):
    by_id = {x.get("id"): x for x in existing if x.get("id")}
    by_soft = {}
    for x in existing:
        key = (normalized(x.get("source", "")), normalized(x.get("title", "")), normalized(x.get("organization", "")))
        by_soft[key] = x
    return by_id, by_soft


def scrape(source: dict, today: date) -> tuple[list[Job], dict]:
    now = datetime.now(timezone.utc).isoformat()
    health = {
        "source": source["name"], "url": source["url"], "mode": source["mode"],
        "group": source.get("group", ""), "notes": source.get("notes", ""),
        "status": source["mode"], "ok": None, "count": 0, "error": "", "checked_at": now,
    }

    if source["mode"] != "automated":
        return [], health

    try:
        r = fetch(source["url"])
        soup = BeautifulSoup(r.text, "lxml")
        budget = [MAX_DETAIL_REQUESTS_PER_SOURCE]
        found = []
        for a in soup.find_all("a", href=True):
            job = candidate_from_anchor(a, source, today, budget)
            if job:
                found.append(job)
        found = dedupe(found)
        health["ok"] = True
        health["count"] = len(found)
        page_text = clean(soup.get_text(" ", strip=True))
        health["status"] = "ok" if found else ("empty-dynamic" if len(page_text) < 500 else "checked-no-matches")
        return found, health
    except Exception as exc:
        health["ok"] = False
        health["status"] = "failed"
        health["error"] = f"{type(exc).__name__}: {exc}"[:300]
        return [], health


def main() -> int:
    today = datetime.now(timezone.utc).date()
    now = datetime.now(timezone.utc).isoformat()
    cutoff = today - timedelta(days=SIX_MONTH_DAYS)
    sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    existing = load_existing()
    by_id, by_soft = prior_lookup(existing)
    old_by_source: dict[str, list[dict]] = {}
    for old in existing:
        old_by_source.setdefault(old.get("source", ""), []).append(old)

    all_jobs: list[Job] = []
    health_rows = []

    for source in sources:
        jobs, health = scrape(source, today)
        health_rows.append(health)
        print(f"{source['name']}: {health['status']} ({health['count']})")

        # A failed / obviously dynamic-empty source should not erase yesterday's jobs.
        preserve_old = health["status"] in {"failed", "empty-dynamic"}
        if preserve_old:
            for old in old_by_source.get(source["name"], []):
                try:
                    all_jobs.append(Job(**{k: old.get(k) for k in Job.__dataclass_fields__}))
                except Exception:
                    pass
            continue

        for job in jobs:
            prior = by_id.get(job.id)
            if not prior:
                key = (normalized(job.source), normalized(job.title), normalized(job.organization))
                prior = by_soft.get(key)
            if prior:
                job.first_seen = prior.get("first_seen") or prior.get("discovered_at") or job.first_seen
                job.posted_date = job.posted_date or prior.get("posted_date")
                job.date_basis = "posted" if job.posted_date else "first_seen"
            job.last_seen = now
            all_jobs.append(job)

    # Deduplicate and apply the six-month rule to roles with a real source-posted date.
    final_map: dict[tuple[str, str, str], Job] = {}
    for job in all_jobs:
        if job.posted_date:
            try:
                if date.fromisoformat(job.posted_date) < cutoff:
                    continue
            except ValueError:
                job.posted_date = None
                job.date_basis = "first_seen"
        key = (normalized(job.source), normalized(job.title), normalized(job.organization))
        current = final_map.get(key)
        if not current or (job.posted_date and not current.posted_date):
            final_map[key] = job

    jobs_out = [asdict(j) for j in final_map.values()]

    def recency_key(j: dict):
        d = j.get("posted_date") or (j.get("first_seen") or "")[:10] or "0000-00-00"
        return (d, j.get("source", ""), j.get("organization", ""))

    jobs_out.sort(key=recency_key, reverse=True)

    JOBS_PATH.write_text(json.dumps({"generated_at": now, "cutoff": cutoff.isoformat(), "jobs": jobs_out}, indent=2, ensure_ascii=False), encoding="utf-8")
    META_PATH.write_text(json.dumps({"generated_at": now, "source_count": len(sources), "sources": health_rows}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(jobs_out)} roles from {len(sources)} tracked sources.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
