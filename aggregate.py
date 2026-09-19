#!/usr/bin/env python3
"""Executive Opportunity Radar v3 updater.

Capture first, enrich second.

The updater checks each configured public executive-search source, captures any
CEO / President / Executive Director opportunity it can verify, preserves jobs
when a source check is unhealthy, archives roles only after repeated successful
misses, and maintains history/change data for market analysis.

Missing salary, location, date, sector, or organization metadata never excludes
an otherwise verifiable opportunity.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
except Exception:  # Playwright is optional outside GitHub Actions.
    sync_playwright = None

ROOT = Path(__file__).resolve().parent
SOURCES_PATH = ROOT / "sources.json"
JOBS_PATH = ROOT / "jobs.json"
HISTORY_PATH = ROOT / "history.json"
CHANGES_PATH = ROOT / "changes.json"
META_PATH = ROOT / "meta.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 Chrome/124 Safari/537.36 ExecutiveOpportunityRadar/3.0"
    )
}
TIMEOUT = 30
MAX_DETAIL_REQUESTS_PER_SOURCE = 18
SIX_MONTH_DAYS = 183
CLOSE_AFTER_MISSES = 2
MAX_CHANGE_EVENTS = 500

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
    r"opportunities|submit resume|join our|about|contact|services|home|next|previous)$",
    re.I,
)
LOCATIONISH = re.compile(
    r"\b(remote|hybrid|onsite|on-site|anywhere|[A-Z][A-Za-z .'-]+,\s*[A-Z]{2}\b)\b",
    re.I,
)
ROLE_SIGNAL = re.compile(
    r"\b(chief executive officer|ceo|executive director|president)\b", re.I
)

SECTOR_RULES = [
    ("Health", r"\b(health|hospital|medical|clinic|care|medicine|patient|wellness|public health)\b"),
    ("Philanthropy", r"\b(foundation|philanthrop|grantmaking|charitable trust|community foundation)\b"),
    ("Education", r"\b(university|college|school|education|academy|student|learning|museum of science)\b"),
    ("Associations", r"\b(association|society|council|institute of|federation|membership organization|professional society)\b"),
    ("Civic / Public", r"\b(city|county|public authority|civic|government|municipal|downtown alliance|chamber)\b"),
    ("Environment", r"\b(environment|climate|conservation|sustainab|energy|wildlife|natural resources)\b"),
    ("Human Services", r"\b(housing|homeless|food bank|hunger|human services|social services|family services|community services)\b"),
    ("Justice / Rights", r"\b(justice|rights|legal|law|civil liberties|immigrant|advocacy)\b"),
    ("Arts / Culture", r"\b(arts|theatre|theater|museum|symphony|opera|culture|cultural)\b"),
    ("Media / Journalism", r"\b(media|journalis|news|press|broadcast|publishing)\b"),
    ("International", r"\b(international|global development|humanitarian|refugee|foreign policy)\b"),
    ("Animal Welfare", r"\b(animal|humane society|veterinary|wildlife rescue)\b"),
    ("Faith", r"\b(church|faith|religio|ministry|diocese|synagogue)\b"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip(" \t\n\r|–—-")


def normalized(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clean(value).lower()).strip()


def role_type(title: str) -> str:
    t = clean(title).lower()
    if "chief executive officer" in t or re.search(r"\bceo\b", t):
        return "CEO"
    if "executive director" in t:
        return "Executive Director"
    if "president" in t:
        return "President"
    return "Other"


def is_target_role(title: str) -> bool:
    """Keep top executive roles without accidentally including vice presidents."""
    t = clean(title)
    if not t or len(t) > 200:
        return False
    if re.search(r"\bchief executive officer\b|\bceo\b", t, re.I):
        return True
    if re.search(r"\bexecutive director\b", t, re.I):
        # Functional ED titles are not the organization's top ED role.
        if re.search(r"\bexecutive director\s+of\b", t, re.I):
            return False
        return True
    # President must not be Vice/Assistant/Associate President.
    if re.match(r"^(?:global\s+)?president(?:\b|\s*[&/]\s*|\s+and\s+)", t, re.I):
        return True
    if re.search(r"\bpresident\s*(?:&|/|and)\s*(?:chief executive officer|ceo)\b", t, re.I):
        return True
    return False


def canonical_url(url: str) -> str:
    if not url:
        return ""
    try:
        p = urlparse(url)
        return f"{p.scheme.lower()}://{p.netloc.lower()}{p.path.rstrip('/')}" or url
    except Exception:
        return url.split("#", 1)[0]


def stable_id(source: str, title: str, organization: str, url: str) -> str:
    """Prefer organization + title identity so URL changes do not create duplicates."""
    org = normalized(organization)
    if org and org != "organization not parsed":
        raw = "|".join([normalized(source), normalized(title), org])
    else:
        raw = "|".join([normalized(source), normalized(title), canonical_url(url)])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def split_title_org(text: str) -> tuple[str, str]:
    s = clean(text)
    if ":" in s:
        left, right = [clean(x) for x in s.split(":", 1)]
        if is_target_role(right) and not is_target_role(left):
            return right, left
    for sep in (" – ", " — ", " - ", " | "):
        if sep in s:
            left, right = [clean(x) for x in s.split(sep, 1)]
            if is_target_role(left) and not is_target_role(right):
                return left, right
            if is_target_role(right) and not is_target_role(left):
                return right, left
    if "," in s:
        left, right = [clean(x) for x in s.split(",", 1)]
        if is_target_role(left) and not is_target_role(right):
            return left, right
        if is_target_role(right) and not is_target_role(left):
            return right, left
    return s, ""


def likely_org(line: str, role: str) -> bool:
    s = clean(line)
    if not s or s == role or len(s) > 150 or len(s) < 2:
        return False
    if NOISE.match(s) or is_target_role(s):
        return False
    if LOCATIONISH.search(s) and len(s.split()) <= 9:
        return False
    if re.search(r"\bposted\b|\bdate posted\b|\bsalary\b|\bcompensation\b|\bapply by\b", s, re.I):
        return False
    return True


def local_container(node):
    current = node
    best = getattr(node, "parent", node)
    for _ in range(7):
        current = getattr(current, "parent", None)
        if current is None or getattr(current, "name", None) in {"body", "html"}:
            break
        text = clean(current.get_text(" ", strip=True))
        if 20 <= len(text) <= 2400:
            best = current
        elif len(text) > 2400:
            break
    return best


def context_lines(node) -> list[str]:
    container = local_container(node)
    lines: list[str] = []
    for x in container.stripped_strings:
        c = clean(x)
        if c and (not lines or c != lines[-1]):
            lines.append(c)
    return lines


def infer_organization(role: str, lines: list[str]) -> str:
    _, parsed_org = split_title_org(role)
    if parsed_org:
        return parsed_org
    idx = None
    for i, line in enumerate(lines):
        nl = normalized(line)
        nr = normalized(role)
        if nr == nl or (nr and nr in nl):
            idx = i
            break
    if idx is not None:
        for offset in (-1, 1, -2, 2, -3, 3, -4, 4):
            j = idx + offset
            if 0 <= j < len(lines) and likely_org(lines[j], role):
                return clean(lines[j])
    joined = " ".join(lines[:18])
    for pattern in (
        r"(?:on behalf of|partnering with|retained by)\s+(?:our client,?\s+)?([A-Z][^.;]{2,110})",
        r"([A-Z][A-Za-z0-9&'’.,() -]{2,110})\s+(?:seeks|is seeking|has retained)",
    ):
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
            return s if len(s) <= 110 else "Remote"
        m = re.search(r"\b([A-Z][A-Za-z .'-]+),\s*([A-Z]{2})\b", s)
        if m:
            return clean(m.group(0))
        if re.search(r"\b(hybrid|onsite|on-site)\b", s, re.I) and len(s) <= 110:
            return s
    return ""


def infer_work_arrangement(location: str, text: str = "") -> str:
    s = f"{location} {text}".lower()
    if "remote" in s:
        return "Remote"
    if "hybrid" in s:
        return "Hybrid"
    if location:
        return "On-site / unspecified"
    return "Unknown"


def infer_sector(organization: str, text: str) -> tuple[str, str]:
    corpus = clean(f"{organization} {text}")[:9000]
    for label, pattern in SECTOR_RULES:
        if re.search(pattern, corpus, re.I):
            return label, "inferred"
    return "Unclassified", "unclassified"


def safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def extract_date(text: str, today: date) -> tuple[date | None, str]:
    s = clean(text)
    m = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", s)
    if m:
        return safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3))), "source_reported"
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", s)
    if m:
        y = int(m.group(3)); y = y + 2000 if y < 100 else y
        return safe_date(y, int(m.group(1)), int(m.group(2))), "source_reported"
    month_names = "|".join(sorted(MONTHS, key=len, reverse=True))
    m = re.search(rf"\b({month_names})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,)?\s+(20\d{{2}})\b", s, re.I)
    if m:
        return safe_date(int(m.group(3)), MONTHS[m.group(1).lower().rstrip(".")], int(m.group(2))), "source_reported"
    m = re.search(r"\bposted\s+(\d+)\s+(hour|day|week|month)s?\s+ago\b", s, re.I)
    if m:
        n = int(m.group(1)); unit = m.group(2).lower()
        days = 0 if unit == "hour" else n if unit == "day" else n * 7 if unit == "week" else n * 30
        return today - timedelta(days=days), "approximate"
    if re.search(r"\bposted\s+(?:today|just now)\b", s, re.I):
        return today, "approximate"
    if re.search(r"\bposted\s+yesterday\b", s, re.I):
        return today - timedelta(days=1), "approximate"
    return None, "unavailable"


def money_value(token: str) -> int | None:
    t = token.lower().replace("$", "").replace(",", "").strip()
    mult = 1000 if t.endswith("k") else 1
    if t.endswith("k"):
        t = t[:-1].strip()
    try:
        v = float(t) * mult
        if 30000 <= v <= 5000000:
            return int(round(v))
    except ValueError:
        pass
    return None


def extract_compensation(text: str) -> tuple[str, int | None, int | None, str]:
    """Return source wording + normalized min/max without requiring it for capture."""
    s = clean(text)
    token = r"\$?\s*(?:\d{2,3}(?:,\d{3})+|\d{2,4}(?:\.\d+)?\s*[kK])"
    range_re = re.compile(rf"({token})\s*(?:-|–|—|to|through)\s*({token})", re.I)
    for m in range_re.finditer(s):
        lo, hi = money_value(m.group(1)), money_value(m.group(2))
        if lo and hi and hi >= lo:
            start=max(0,m.start()-70); end=min(len(s),m.end()+90)
            snippet=clean(s[start:end])
            return snippet, lo, hi, "published_range"
    single_re = re.compile(rf"\b(?:salary|compensation|pay range|annual salary|base salary)[^.;:]{{0,80}}({token})", re.I)
    m = single_re.search(s)
    if m:
        v=money_value(m.group(1))
        if v:
            start=max(0,m.start()-25); end=min(len(s),m.end()+90)
            return clean(s[start:end]), v, v, "published_single"
    return "", None, None, "not_published"


def evidence_excerpt(text: str, role: str, organization: str) -> str:
    s=clean(text)
    if not s:
        return ""
    needles=[organization if organization != "Organization not parsed" else "", role]
    positions=[s.lower().find(n.lower()) for n in needles if n]
    positions=[p for p in positions if p>=0]
    center=min(positions) if positions else 0
    start=max(0,center-250); end=min(len(s),center+950)
    return clean(s[start:end])[:1200]


def fetch(url: str) -> requests.Response:
    r=requests.get(url,headers=HEADERS,timeout=TIMEOUT,allow_redirects=True)
    r.raise_for_status()
    return r


def browser_html(url: str) -> str:
    if sync_playwright is None:
        raise RuntimeError("Playwright not installed")
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(user_agent=HEADERS["User-Agent"],viewport={"width":1440,"height":1200})
        page.goto(url,wait_until="domcontentloaded",timeout=60000)
        try:
            page.wait_for_load_state("networkidle",timeout=12000)
        except Exception:
            pass
        # Trigger lazy/infinite lists and common load-more controls.
        for _ in range(6):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(450)
            clicked=False
            for selector in [
                "button:has-text('Load more')","a:has-text('Load more')",
                "button:has-text('Show more')","a:has-text('Show more')",
                "button:has-text('More jobs')","a:has-text('More jobs')"
            ]:
                try:
                    loc=page.locator(selector).first
                    if loc.count() and loc.is_visible():
                        loc.click(timeout=1500); page.wait_for_timeout(650); clicked=True; break
                except Exception:
                    pass
            if not clicked:
                # Continue a few scrolls for lazy loading, then stop.
                continue
        html=page.content()
        browser.close()
        return html


def valid_url(url: str) -> bool:
    try:
        return urlparse(url).scheme in {"http","https"}
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
    role_type: str = "Other"
    compensation_text: str = ""
    compensation_min: int | None = None
    compensation_max: int | None = None
    compensation_status: str = "not_published"
    sector: str = "Unclassified"
    sector_status: str = "unclassified"
    work_arrangement: str = "Unknown"
    location_status: str = "unavailable"
    posted_date_status: str = "unavailable"
    evidence: str = ""
    updated_at: str = ""
    missing_runs: int = 0
    closed_date: str | None = None
    change_count: int = 0
    baseline_seed: bool = False
    link_quality: str = "source_page"


def node_href(node, source_url: str) -> str:
    if getattr(node,"name",None)=="a" and node.get("href"):
        href=node.get("href")
    else:
        container=local_container(node)
        a=container.find("a",href=True) if container else None
        href=a.get("href") if a else ""
    url=urljoin(source_url,href) if href else source_url
    if not valid_url(url) or url.lower().startswith("javascript:"):
        return source_url
    return url


def candidate_from_node(node, source: dict, today: date, detail_budget: list[int]) -> Job | None:
    raw=clean(node.get_text(" ",strip=True))
    role, org_from_title=split_title_org(raw)
    if not is_target_role(role):
        container=local_container(node)
        # Look for a nearby heading/strong label carrying the actual role.
        candidates=[]
        if container:
            candidates += container.find_all(["h1","h2","h3","h4","h5","h6","strong"], limit=10)
        for h in candidates:
            rt, ro=split_title_org(clean(h.get_text(" ",strip=True)))
            if is_target_role(rt):
                role, org_from_title=rt, ro
                break
    if not is_target_role(role):
        return None

    lines=context_lines(node)
    organization=org_from_title or infer_organization(role,lines)
    location=infer_location(lines)
    list_text=" | ".join(lines[:40])
    url=node_href(node,source["url"])

    use_source_dates=source.get("date_policy","source")!="first_seen"
    posted, posted_status=extract_date(list_text,today) if use_source_dates else (None,"unavailable")
    detail_text=""

    # Detail pages often contain the missing date, salary, location or org.
    if detail_budget[0] > 0 and url != source["url"] and not url.lower().endswith(".pdf"):
        try:
            detail_budget[0]-=1
            time.sleep(0.08)
            dr=fetch(url)
            dsoup=BeautifulSoup(dr.text,"lxml")
            detail_text=clean(dsoup.get_text(" ",strip=True))[:30000]
            if use_source_dates and posted is None:
                posted, posted_status=extract_date(detail_text,today)
            if not location:
                location=infer_location([detail_text])
            if organization=="Organization not parsed":
                for pattern in (
                    r"on behalf of (?:our client,?\s+)?([A-Z][^.;]{2,110})",
                    r"([A-Z][A-Za-z0-9&'’.,() -]{2,110})\s+(?:seeks|is seeking)\s+(?:an?|its next|a new)\s+"+re.escape(role),
                ):
                    m=re.search(pattern,detail_text,re.I)
                    if m:
                        candidate=clean(m.group(1))
                        if likely_org(candidate,role): organization=candidate; break
        except Exception:
            pass

    combined=clean(f"{list_text} {detail_text}")[:35000]
    comp_text, comp_min, comp_max, comp_status=extract_compensation(combined)
    sector, sector_status=infer_sector(organization,combined)
    now=now_iso()
    return Job(
        id=stable_id(source["name"],role,organization,url),
        title=clean(role), organization=organization, source=source["name"],
        location=location, url=url, source_url=source["url"],
        posted_date=posted.isoformat() if posted else None,
        first_seen=now,last_seen=now,
        date_basis="posted_relative" if posted_status=="approximate" else "posted" if posted else "first_seen",
        status="open", role_type=role_type(role),
        compensation_text=comp_text,compensation_min=comp_min,compensation_max=comp_max,
        compensation_status=comp_status,sector=sector,sector_status=sector_status,
        work_arrangement=infer_work_arrangement(location,combined),
        location_status="extracted" if location else "unavailable",
        posted_date_status=posted_status if posted else "unavailable",
        evidence=evidence_excerpt(combined,role,organization), updated_at=now,
        missing_runs=0, closed_date=None, change_count=0, baseline_seed=False,
        link_quality="direct" if url and url != source["url"] else "source_page",
    )


def candidates_from_html(html: str, source: dict, today: date) -> list[Job]:
    soup=BeautifulSoup(html,"lxml")
    budget=[MAX_DETAIL_REQUESTS_PER_SOURCE]
    found=[]
    # Anchors are best because they preserve direct job URLs.
    for a in soup.find_all("a",href=True):
        job=candidate_from_node(a,source,today,budget)
        if job: found.append(job)
    # Some sites put the role in a heading and link elsewhere in the card.
    for h in soup.find_all(["h1","h2","h3","h4","h5","h6"]):
        if ROLE_SIGNAL.search(clean(h.get_text(" ",strip=True))):
            job=candidate_from_node(h,source,today,budget)
            if job: found.append(job)
    return dedupe(found)


def dedupe(jobs: Iterable[Job]) -> list[Job]:
    out: dict[tuple[str,str,str],Job]={}
    for j in jobs:
        key=(normalized(j.source),normalized(j.title),normalized(j.organization))
        old=out.get(key)
        if not old:
            out[key]=j; continue
        # Prefer direct links and richer metadata.
        score=lambda x: (
            1 if x.link_quality=="direct" else 0,
            1 if x.organization!="Organization not parsed" else 0,
            1 if x.posted_date else 0,
            1 if x.location else 0,
            1 if x.compensation_text else 0,
        )
        if score(j)>score(old): out[key]=j
    return list(out.values())


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def old_open_by_source(history_jobs: list[dict]) -> dict[str,list[dict]]:
    out={}
    for j in history_jobs:
        if j.get("status")=="open": out.setdefault(j.get("source",""),[]).append(j)
    return out


def compatible_job(raw: dict) -> Job:
    fields=Job.__dataclass_fields__
    kwargs={k:raw.get(k,fields[k].default if fields[k].default is not None else None) for k in fields}
    # Required string fallbacks for older files.
    for key in ["id","title","organization","source","location","url","source_url","first_seen","last_seen","date_basis"]:
        if kwargs.get(key) is None: kwargs[key]=""
    if not kwargs.get("role_type"): kwargs["role_type"]=role_type(kwargs.get("title",""))
    if not kwargs.get("updated_at"): kwargs["updated_at"]=kwargs.get("last_seen","")
    return Job(**kwargs)


def find_prior(job: Job, history_jobs: list[dict]) -> dict | None:
    for old in history_jobs:
        if old.get("id")==job.id: return old
    # Soft match permits URL changes and occasional org parser improvements.
    candidates=[x for x in history_jobs if normalized(x.get("source"))==normalized(job.source) and normalized(x.get("title"))==normalized(job.title)]
    if job.organization!="Organization not parsed":
        for x in candidates:
            if normalized(x.get("organization"))==normalized(job.organization): return x
    for x in candidates:
        if canonical_url(x.get("url",""))==canonical_url(job.url): return x
    return candidates[0] if len(candidates)==1 else None


def carry_forward(job: Job, prior: dict) -> Job:
    job.id=prior.get("id") or job.id
    job.first_seen=prior.get("first_seen") or job.first_seen
    job.baseline_seed=bool(prior.get("baseline_seed",False))
    job.change_count=int(prior.get("change_count",0) or 0)
    job.missing_runs=0
    job.closed_date=None
    # Do not lose previously known metadata just because today's parser missed a field.
    for attr in ["organization","location","posted_date","compensation_text","compensation_min","compensation_max","sector"]:
        new=getattr(job,attr)
        old=prior.get(attr)
        missing=new in (None,"","Organization not parsed","Unclassified")
        if missing and old not in (None,""):
            setattr(job,attr,old)
    if job.posted_date:
        job.date_basis=prior.get("date_basis") if prior.get("posted_date")==job.posted_date else job.date_basis
        job.posted_date_status=prior.get("posted_date_status") if prior.get("posted_date")==job.posted_date else job.posted_date_status
    job.compensation_status = prior.get("compensation_status",job.compensation_status) if not job.compensation_text else job.compensation_status
    job.sector_status = prior.get("sector_status",job.sector_status) if job.sector==prior.get("sector") else job.sector_status
    job.location_status = "extracted" if job.location else prior.get("location_status","unavailable")
    job.work_arrangement=infer_work_arrangement(job.location,job.evidence)
    job.link_quality="direct" if job.url and job.url!=job.source_url else prior.get("link_quality",job.link_quality)
    return job


def changed_fields(prior: dict, current: Job) -> dict:
    changes={}
    for fieldname in ["title","organization","location","url","posted_date","compensation_text","compensation_min","compensation_max","sector","work_arrangement"]:
        before=prior.get(fieldname); after=getattr(current,fieldname)
        if before not in (None,"") and after not in (None,"") and before!=after:
            changes[fieldname]={"from":before,"to":after}
    return changes


def scrape_source(source: dict, today: date, prior_count: int) -> tuple[list[Job],dict]:
    now=now_iso()
    health={
        "source":source["name"],"url":source["url"],"mode":source["mode"],
        "group":source.get("group",""),"notes":source.get("notes",""),
        "status":source["mode"],"ok":None,"count":0,"prior_count":prior_count,
        "error":"","checked_at":now,"fetch_mode":"","preserved":False,
    }
    if source["mode"]!="automated": return [],health

    errors=[]; all_found=[]; fetch_modes=[]
    urls=source.get("urls") or [source["url"]]
    for url in urls:
        html=""
        try:
            r=fetch(url); html=r.text; fetch_modes.append("static")
            all_found.extend(candidates_from_html(html,{**source,"url":url},today))
        except Exception as exc:
            errors.append(f"static {type(exc).__name__}: {exc}")
        # Browser rendering is a fallback when static returned no useful roles for this page.
        if source.get("render_fallback") and not any(j.source==source["name"] for j in all_found):
            try:
                html=browser_html(url); fetch_modes.append("browser")
                all_found.extend(candidates_from_html(html,{**source,"url":url},today))
            except Exception as exc:
                errors.append(f"browser {type(exc).__name__}: {exc}")

    found=dedupe(all_found)
    health["count"]=len(found); health["fetch_mode"]="+".join(dict.fromkeys(fetch_modes))
    if not fetch_modes:
        health["ok"]=False; health["status"]="failed"; health["error"]=" | ".join(errors)[:500]
        return [],health

    # Unexpected collapses are treated as partial, not as mass closures.
    if prior_count>=5 and len(found)<max(2,int(prior_count*0.35)) and not source.get("allow_zero",False):
        health["ok"]=False; health["status"]="partial-suspected"; health["preserved"]=True
        health["error"]=(f"Found {len(found)} vs {prior_count} previously open; preserving prior roles pending another healthy parse. " + " | ".join(errors))[:500]
        return found,health

    health["ok"]=True
    health["status"]="ok" if found else "checked-no-matches"
    health["error"]=" | ".join(errors)[:500]
    return found,health


def main() -> int:
    today=datetime.now(timezone.utc).date(); now=now_iso(); cutoff=today-timedelta(days=SIX_MONTH_DAYS)
    sources=load_json(SOURCES_PATH,[])
    history_payload=load_json(HISTORY_PATH,{"jobs":[]}); history_jobs=history_payload.get("jobs",[])
    changes_payload=load_json(CHANGES_PATH,{"events":[]}); events=changes_payload.get("events",[])
    old_open=old_open_by_source(history_jobs)
    updated_history=[dict(x) for x in history_jobs]
    by_id={x.get("id"):x for x in updated_history if x.get("id")}
    health_rows=[]
    seen_ids=set()

    for source in sources:
        prior_source=old_open.get(source["name"],[])
        found,health=scrape_source(source,today,len(prior_source))
        health_rows.append(health)
        print(f"{source['name']}: {health['status']} ({health['count']})")

        unhealthy=health["status"] in {"failed","partial-suspected"}
        if unhealthy:
            # We may still merge richer roles we did find, but never close anything from this source.
            for job in found:
                prior=find_prior(job,updated_history)
                if prior:
                    job=carry_forward(job,prior); job.last_seen=now; seen_ids.add(job.id)
                    changes=changed_fields(prior,job)
                    if changes:
                        job.change_count=int(prior.get("change_count",0) or 0)+1; job.updated_at=now
                        events.append({"at":now,"type":"updated","job_id":job.id,"source":job.source,"title":job.title,"organization":job.organization,"changes":changes})
                    by_id[job.id]=asdict(job)
                else:
                    seen_ids.add(job.id); by_id[job.id]=asdict(job)
                    events.append({"at":now,"type":"new","job_id":job.id,"source":job.source,"title":job.title,"organization":job.organization})
            continue

        # Healthy source: merge current roles.
        source_seen=set()
        for job in found:
            prior=find_prior(job,updated_history)
            if prior:
                job=carry_forward(job,prior)
                if prior.get("status")=="closed":
                    events.append({"at":now,"type":"reopened","job_id":prior.get("id"),"source":job.source,"title":job.title,"organization":job.organization})
                changes=changed_fields(prior,job)
                if changes:
                    job.change_count=int(prior.get("change_count",0) or 0)+1; job.updated_at=now
                    events.append({"at":now,"type":"updated","job_id":job.id,"source":job.source,"title":job.title,"organization":job.organization,"changes":changes})
            else:
                events.append({"at":now,"type":"new","job_id":job.id,"source":job.source,"title":job.title,"organization":job.organization})
            job.status="open"; job.closed_date=None; job.last_seen=now; job.missing_runs=0
            by_id[job.id]=asdict(job); source_seen.add(job.id); seen_ids.add(job.id)

        # A role must disappear on two healthy runs before being archived.
        for prior in prior_source:
            pid=prior.get("id")
            if pid in source_seen: continue
            current=dict(by_id.get(pid,prior))
            misses=int(current.get("missing_runs",0) or 0)+1
            current["missing_runs"]=misses
            if misses>=CLOSE_AFTER_MISSES:
                current["status"]="closed"; current["closed_date"]=today.isoformat(); current["updated_at"]=now
                events.append({"at":now,"type":"closed","job_id":pid,"source":current.get("source"),"title":current.get("title"),"organization":current.get("organization")})
            by_id[pid]=current

    # Rebuild history and current-open feed.
    all_history=list(by_id.values())
    # Keep known posted dates within six months on current feed; first-seen-only roles are retained while open.
    current=[]
    for j in all_history:
        if j.get("status")!="open": continue
        if j.get("posted_date"):
            try:
                if date.fromisoformat(j["posted_date"])<cutoff: continue
            except ValueError:
                j["posted_date"]=None; j["date_basis"]="first_seen"; j["posted_date_status"]="unavailable"
        current.append(j)

    def recency(j): return j.get("posted_date") or (j.get("first_seen") or "")[:10] or "0000-00-00"
    current.sort(key=lambda j:(recency(j),j.get("source",""),j.get("organization","")),reverse=True)
    all_history.sort(key=lambda j:((j.get("first_seen") or ""),j.get("source",""),j.get("organization","")),reverse=True)
    events=events[-MAX_CHANGE_EVENTS:]

    baseline=history_payload.get("baseline_initialized_at") or history_payload.get("generated_at") or now
    JOBS_PATH.write_text(json.dumps({"generated_at":now,"cutoff":cutoff.isoformat(),"baseline_initialized_at":baseline,"jobs":current},indent=2,ensure_ascii=False),encoding="utf-8")
    HISTORY_PATH.write_text(json.dumps({"generated_at":now,"baseline_initialized_at":baseline,"jobs":all_history},indent=2,ensure_ascii=False),encoding="utf-8")
    CHANGES_PATH.write_text(json.dumps({"generated_at":now,"events":events},indent=2,ensure_ascii=False),encoding="utf-8")
    META_PATH.write_text(json.dumps({"generated_at":now,"baseline_initialized_at":baseline,"source_count":len(sources),"sources":health_rows},indent=2,ensure_ascii=False),encoding="utf-8")
    print(f"Wrote {len(current)} open roles; {len(all_history)} total historical roles; {len(sources)} tracked sources.")
    return 0


if __name__=="__main__":
    raise SystemExit(main())
