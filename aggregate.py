#!/usr/bin/env python3
"""Executive Opportunity Radar v6 updater.

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
        "AppleWebKit/537.36 Chrome/124 Safari/537.36 ExecutiveOpportunityRadar/6.0"
    )
}
TIMEOUT = 30
MAX_DETAIL_REQUESTS_PER_SOURCE = 18
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

CLOSED_SIGNAL = re.compile(
    r"\b(no longer accepting applications?|position filled|positions? filled|search (?:is )?closed|"
    r"applications? (?:are )?closed|application period (?:is )?closed|"
    r"this search has concluded|search concluded|position has been filled)\b", re.I
)

def has_closed_signal(text: str) -> bool:
    return bool(CLOSED_SIGNAL.search(clean(text)))

SECTOR_RULES = [
    ("Health", r"\b(health|hospital|medical|clinic|care|medicine|patient|wellness|public health)\b"),
    ("Philanthropy", r"\b(foundation|philanthrop|grantmaking|charitable trust|community foundation)\b"),
    ("Education", r"\b(university|college|school|education|academy|student|learning|museum of science)\b"),
    ("Associations", r"\b(association|society|council|federation|membership organization|professional society|trade organization|member organization)\b"),
    ("Civic / Public", r"\b(city|county|public authority|civic|government|municipal|downtown alliance|chamber)\b"),
    ("Environment", r"\b(environment|climate|conservation|sustainab|energy|wildlife|natural resources)\b"),
    ("Human Services", r"\b(housing|homeless|food bank|hunger|human services|social services|family services|community services)\b"),
    ("Justice / Rights", r"\b(social justice|racial justice|civil rights|civil liberties|immigrant rights|legal services|access to justice|human rights)\b"),
    ("Arts / Culture", r"\b(arts|theatre|theater|museum|symphony|opera|culture|cultural)\b"),
    ("Media / Journalism", r"\b(media|journalis|news|press|broadcast|publishing)\b"),
    ("International", r"\b(international|global development|humanitarian|refugee|foreign policy)\b"),
    ("Animal Welfare", r"\b(animal|humane society|veterinary|wildlife rescue)\b"),
    ("Faith", r"\b(church|faith|religio|ministry|diocese|synagogue)\b"),
]

ORGANIZATION_TYPE_RULES = [
    ("Association / Professional Society", r"\b(association|professional society|membership organization|chamber of commerce|federation|academy of|bar association|trade association)\b"),
    ("Foundation / Philanthropy", r"\b(foundation|philanthrop|grantmaking|charitable trust|community foundation)\b"),
    ("College / University", r"\b(university|college|higher education|campus|chancellor)\b"),
    ("K-12 / Education", r"\b(school district|public schools?|independent school|charter school|education association|school boards?)\b"),
    ("Health System / Provider", r"\b(health system|hospital|medical center|clinic|physician|healthcare|health care|patient care)\b"),
    ("Government / Public Entity", r"\b(city of|county of|state of|public authority|government agency|municipal|board of examiners)\b"),
    ("Media / Journalism", r"\b(public radio|journalis|news|media organization|broadcast|publisher|press)\b"),
    ("Arts / Culture", r"\b(museum|theatre|theater|symphony|opera|arts center|cultural)\b"),
    ("Advocacy / Civil Rights", r"\b(advocacy|civil rights|justice|legal services|policy organization|immigrant rights)\b"),
    ("Human Services / Community", r"\b(human services|social services|housing|food bank|community services|youth development|family services)\b"),
    ("Other Nonprofit", r"\b(nonprofit|not-for-profit|501\(c\)|charitable organization|mission-driven)\b"),
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


def infer_sector_tags(organization: str, text: str) -> tuple[list[str], str]:
    """Return every supported sector tag that the public text supports.

    Tags intentionally overlap: e.g. an association can also be Health or Education.
    """
    corpus = clean(f"{organization} {text}")[:16000]
    tags=[]
    for label, pattern in SECTOR_RULES:
        if re.search(pattern, corpus, re.I) and label not in tags:
            tags.append(label)
    return (tags or ["Unclassified"]), ("inferred" if tags else "unclassified")


def infer_organization_types(organization: str, text: str) -> tuple[list[str], str]:
    """Return overlapping organization-type tags rather than forcing one bucket."""
    corpus=clean(f"{organization} {text}")[:18000]
    tags=[]
    for label, pattern in ORGANIZATION_TYPE_RULES:
        if re.search(pattern, corpus, re.I) and label not in tags:
            tags.append(label)
    return (tags or ["Unclassified"]), ("inferred" if tags else "unclassified")


def infer_sector(organization: str, text: str) -> tuple[str, str]:
    tags,status=infer_sector_tags(organization,text)
    return tags[0],status


def infer_organization_type(organization: str, text: str) -> tuple[str, str]:
    tags,status=infer_organization_types(organization,text)
    return tags[0],status


def infer_mandate_tags(text: str) -> list[str]:
    corpus=clean(text)[:24000]
    rules=[
        ("Growth / scale",r"\b(scale|scaling|growth|expand|expansion|grow the organization|new markets?)\b"),
        ("Fundraising / revenue",r"\b(fundrais|development|philanthrop|donor|earned revenue|revenue diversification|capital campaign)\b"),
        ("Strategy / transformation",r"\b(strategic plan|strategy|transform|transformation|turnaround|organizational change|change management)\b"),
        ("Operations / infrastructure",r"\b(operational excellence|operations|infrastructure|systems|process improvement|internal controls)\b"),
        ("External affairs / advocacy",r"\b(advocacy|government relations|public policy|external affairs|public affairs|coalition|legislative)\b"),
        ("Membership / stakeholders",r"\b(membership|members|stakeholder|member engagement|chapter|constituent engagement)\b"),
        ("Culture / talent",r"\b(culture|talent|staff development|organizational culture|employee engagement|team building)\b"),
        ("Digital / AI",r"\b(digital transformation|technology strategy|artificial intelligence|\bAI\b|data strategy|modernize technology)\b"),
        ("Financial sustainability",r"\b(financial sustainability|fiscal sustainability|financial stewardship|budget discipline|long-term sustainability)\b"),
    ]
    return [label for label,pat in rules if re.search(pat,corpus,re.I)]


def infer_succession_reason(text: str) -> str:
    corpus=clean(text)[:20000]
    rules=[
        ("Retirement",r"\b(retir(?:e|es|ed|ement|ing))\b"),
        ("Planned succession",r"\b(planned succession|succession process|succession planning|leadership transition)\b"),
        ("Founder transition",r"\b(founder|founding (?:ceo|president|executive director)).{0,90}\b(transition|depart|step down|retir)\b"),
        ("Interim leadership",r"\b(interim (?:ceo|president|executive director)|currently led by an interim)\b"),
        ("Newly created role",r"\b(newly created|new position|new role|inaugural)\b"),
    ]
    for label,pat in rules:
        if re.search(pat,corpus,re.I): return label
    return ""


def extract_application_deadline(text: str, today: date) -> str | None:
    s=clean(text)
    for pat in [r"(?:application|apply|priority consideration|applications? received by|deadline)\s*(?:deadline|by|through|until|:)??\s*([^|.;]{3,55})"]:
        m=re.search(pat,s,re.I)
        if m:
            d,_=extract_date(m.group(1),today)
            if d: return d.isoformat()
    return None

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


def extract_labeled_posted_date(text: str, today: date) -> tuple[date | None, str]:
    """Extract a posting date only when the page labels it as a posting date."""
    s = clean(text)
    patterns = [
        r"\bdate\s+posted\s*:?\s*(?:\|\s*)?([^|;]{3,45})",
        r"\bposted\s+date\s*:?\s*(?:\|\s*)?([^|;]{3,45})",
        r"\bposted\s*:?\s*(?:\|\s*)?([^|;]{3,45})",
    ]
    for pattern in patterns:
        m = re.search(pattern, s, re.I)
        if not m:
            continue
        parsed, status = extract_date(clean(m.group(1)), today)
        if parsed:
            return parsed, status
    m = re.search(r"\bposted\s+(\d+)\s+(hour|day|week|month)s?\s+ago\b", s, re.I)
    if m:
        return extract_date(m.group(0), today)
    m = re.search(r"\bposted\s+(?:today|yesterday|just now)\b", s, re.I)
    if m:
        return extract_date(m.group(0), today)
    return None, "unavailable"


NONLISTING_PATH = re.compile(
    r"/(?:service(?:s|-types)?|our-services|expertise|functions?|about|team|people|"
    r"insights?(?:-results)?|results|news|contact|practice(?:-areas)?|industr(?:y|ies)|role)/",
    re.I,
)

def blocked_nonlisting_url(url: str, source_url: str = "") -> bool:
    """Reject obvious marketing/service/navigation URLs as job detail pages."""
    if not url:
        return False
    if canonical_url(url) == canonical_url(source_url):
        return False
    try:
        path = urlparse(url).path or "/"
    except Exception:
        return False
    return bool(NONLISTING_PATH.search(path))


def allowed_candidate_url(url: str, source: dict) -> bool:
    """Apply a source-specific detail URL allowlist when one is configured."""
    pattern = source.get("allowed_detail_path_regex")
    if not pattern:
        return not blocked_nonlisting_url(url, source.get("url", ""))
    try:
        path = urlparse(url).path or "/"
    except Exception:
        return False
    return bool(re.search(pattern, path, re.I))


def money_value(token: str) -> int | None:
    t=clean(token).lower().replace("usd","").replace("us$","").replace("$","").replace(",","").strip()
    mult=1000 if t.endswith("k") else 1
    if t.endswith("k"): t=t[:-1].strip()
    try:
        v=float(t)*mult
        if 30000 <= v <= 5000000: return int(round(v))
    except ValueError:
        pass
    return None


def extract_compensation(text: str) -> tuple[str, int | None, int | None, str]:
    """Extract compensation only from salary/pay-labeled context.

    Supports ranges such as USD 425,000.00 - 475,000.00, $340k-$375k,
    and single/starting figures such as salary starting at $230,000.
    """
    s=clean(text)
    token=r"(?:USD\s*)?(?:US\$\s*)?\$?\s*(?:\d{2,3}(?:,\d{3})+(?:\.\d{1,2})?|\d{2,4}(?:\.\d+)?\s*[kK])"
    labels=r"(?:compensation|salary|base salary|base compensation|pay range|annual salary|anticipated (?:base )?(?:salary|compensation)|estimated base compensation|salary range|base pay)"
    # Labeled range first.
    for lm in re.finditer(labels,s,re.I):
        window=s[lm.start():lm.start()+650]
        rm=re.search(rf"({token})\s*(?:-|–|—|to|through)\s*({token})",window,re.I)
        if rm:
            lo,hi=money_value(rm.group(1)),money_value(rm.group(2))
            if lo and hi and hi>=lo:
                return clean(window[:min(len(window),rm.end()+140)]),lo,hi,"published_range"
        sm=re.search(rf"(?:starting at|from|minimum of|minimum|at least)?\s*({token})",window,re.I)
        if sm:
            v=money_value(sm.group(1))
            if v:
                status="published_minimum" if re.search(r"starting at|from|minimum|at least",window[:sm.start()+20],re.I) else "published_single"
                return clean(window[:min(len(window),sm.end()+120)]),v,(None if status=="published_minimum" else v),status
    # Korn Ferry and similar tables sometimes say "Compensation: USD ...".
    m=re.search(rf"Compensation\s*:?\s*({token})\s*(?:-|–|—|to|through)\s*({token})",s,re.I)
    if m:
        lo,hi=money_value(m.group(1)),money_value(m.group(2))
        if lo and hi and hi>=lo:
            return clean(s[max(0,m.start()-20):min(len(s),m.end()+160)]),lo,hi,"published_range"
    return "",None,None,"not_published"

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


def browser_html(url: str, max_scrolls: int = 8) -> str:
    if sync_playwright is None:
        raise RuntimeError("Playwright not installed")
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(user_agent=HEADERS["User-Agent"],viewport={"width":1440,"height":1200})
        page.goto(url,wait_until="domcontentloaded",timeout=60000)
        try: page.wait_for_load_state("networkidle",timeout=12000)
        except Exception: pass
        stable=0; last_sig=None
        for _ in range(max_scrolls):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(650)
            clicked=False
            for selector in ["button:has-text('Load more')","a:has-text('Load more')","button:has-text('Show more')","a:has-text('Show more')","button:has-text('More jobs')","a:has-text('More jobs')","button:has-text('View more')","a:has-text('View more')"]:
                try:
                    loc=page.locator(selector).first
                    if loc.count() and loc.is_visible():
                        loc.click(timeout=1800); page.wait_for_timeout(750); clicked=True; break
                except Exception: pass
            try:
                sig=(page.evaluate("document.body.scrollHeight"),page.locator("a").count())
            except Exception:
                sig=None
            if not clicked and sig==last_sig: stable+=1
            else: stable=0
            last_sig=sig
            if stable>=3: break
        html=page.content(); browser.close(); return html

def browser_korn_html(url: str) -> str:
    """Search Korn Ferry's rendered candidate portal for each target title and aggregate direct job links.

    This avoids trusting a single initial result page, which materially undercounted Korn Ferry in prior versions.
    """
    if sync_playwright is None:
        raise RuntimeError("Playwright not installed")
    collected=set()
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(user_agent=HEADERS["User-Agent"],viewport={"width":1440,"height":1200})

        def collect_links():
            try:
                hrefs=page.locator("a[href]").evaluate_all("els => els.map(e => e.href)")
            except Exception:
                hrefs=[]
            for href in hrefs:
                try:
                    if re.search(r"/job/Korn-Ferry-Executive-Search-[^?#]+/\d+/?$",urlparse(href).path,re.I):
                        collected.add(href)
                except Exception:
                    pass

        def sweep():
            # collect lazy-loaded results on the current result page
            stable=0; last=-1
            for _ in range(18):
                collect_links(); page.evaluate("window.scrollTo(0, document.body.scrollHeight)"); page.wait_for_timeout(450)
                n=len(collected); stable=stable+1 if n==last else 0; last=n
                if stable>=3: break
            # Some deployments paginate instead of lazy-loading. Collect before moving on.
            for _ in range(20):
                collect_links(); nxt=None
                for sel in ["a:has-text('Next')","button:has-text('Next')","[aria-label='Next']","[aria-label*='next' i]"]:
                    try:
                        loc=page.locator(sel).last
                        if loc.count() and loc.is_visible() and not loc.is_disabled(): nxt=loc; break
                    except Exception:
                        pass
                if nxt is None: break
                before=page.url
                try:
                    nxt.click(timeout=2500); page.wait_for_timeout(900)
                    try: page.wait_for_load_state("networkidle",timeout=5000)
                    except Exception: pass
                    if page.url==before:
                        # JS pagination is fine; if no new links appear twice, the outer loop will terminate on disabled/absent Next.
                        pass
                except Exception:
                    break

        queries=[None,"Chief Executive Officer","CEO","President","Executive Director"]
        for q in queries:
            page.goto(url,wait_until="domcontentloaded",timeout=60000)
            try: page.wait_for_load_state("networkidle",timeout=10000)
            except Exception: pass
            if q:
                box=None
                for sel in ["input[placeholder*='Job Title' i]","input[aria-label*='Job Title' i]","input[placeholder*='Keyword' i]","input[type='text']"]:
                    try:
                        loc=page.locator(sel).first
                        if loc.count() and loc.is_visible(): box=loc; break
                    except Exception:
                        pass
                if box is not None:
                    try:
                        box.fill(q); box.press("Enter"); page.wait_for_timeout(1100)
                        try: page.wait_for_load_state("networkidle",timeout=6000)
                        except Exception: pass
                    except Exception:
                        pass
            sweep()
        browser.close()
    if not collected:
        raise ValueError("Korn Ferry browser search exposed no client job detail links")
    return "<html><body>"+"".join(f'<a href="{u}">candidate</a>' for u in sorted(collected))+"</body></html>"


def valid_url(url: str) -> bool:
    try:
        return urlparse(url).scheme in {"http","https"}
    except Exception:
        return False


def extract_pdf_text(url: str, max_bytes: int = 8_000_000) -> str:
    """Best-effort text extraction for linked leadership/position profiles."""
    try:
        r=fetch(url)
        if len(r.content)>max_bytes: return ""
        from io import BytesIO
        from pypdf import PdfReader
        reader=PdfReader(BytesIO(r.content))
        return clean(" ".join((page.extract_text() or "") for page in reader.pages[:80]))[:80000]
    except Exception:
        return ""


def enrich_context_from_linked_profile(dsoup, base_url: str, context: str) -> str:
    if extract_compensation(context)[0]: return context
    for a in dsoup.find_all("a",href=True):
        href=urljoin(base_url,a.get("href")); label=clean(a.get_text(" ",strip=True))
        if href.lower().split("?",1)[0].endswith(".pdf") and re.search(r"profile|position|leadership|prospectus|job description|search",label+" "+href,re.I):
            extra=extract_pdf_text(href)
            if extra: return clean(context+" "+extra)
    return context


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
    sector_tags: list[str] = field(default_factory=list)
    sector_status: str = "unclassified"
    organization_type: str = "Unclassified"
    organization_types: list[str] = field(default_factory=list)
    organization_type_status: str = "unclassified"
    work_arrangement: str = "Unknown"
    parent_organization: str = ""
    application_deadline: str | None = None
    mandate_tags: list[str] = field(default_factory=list)
    succession_reason: str = ""
    latest_reported_ceo_comp: int | None = None
    latest_reported_ceo_comp_year: int | None = None
    latest_reported_ceo_name: str = ""
    latest_reported_ceo_comp_source: str = ""
    org_revenue: int | None = None
    org_assets: int | None = None
    org_ein: str = ""
    location_status: str = "unavailable"
    posted_date_status: str = "unavailable"
    evidence: str = ""
    updated_at: str = ""
    missing_runs: int = 0
    closed_date: str | None = None
    change_count: int = 0
    baseline_seed: bool = False
    link_quality: str = "source_page"

def build_job(*, source: dict, title: str, organization: str, location: str, url: str,
              today: date, context: str = "", posted: date | None = None,
              posted_status: str = "unavailable", parent_organization: str = "") -> Job:
    """Build one normalized role without requiring optional metadata."""
    role=clean(title); organization=clean(organization) or "Organization not parsed"
    location=clean(location); context=clean(context)
    comp_text,comp_min,comp_max,comp_status=extract_compensation(context)
    sector_tags,sector_status=infer_sector_tags(organization,context)
    org_types,organization_type_status=infer_organization_types(organization,context)
    now=now_iso(); source_url=source["url"]
    direct=bool(url and canonical_url(url)!=canonical_url(source_url))
    return Job(
        id=stable_id(source["name"],role,organization,url or source_url),
        title=role,organization=organization,source=source["name"],location=location,
        url=url or source_url,source_url=source_url,
        posted_date=posted.isoformat() if posted else None,first_seen=now,last_seen=now,
        date_basis="posted_relative" if posted_status=="approximate" else "posted" if posted else "first_seen",
        status="open",role_type=role_type(role),compensation_text=comp_text,
        compensation_min=comp_min,compensation_max=comp_max,compensation_status=comp_status,
        sector=sector_tags[0],sector_tags=sector_tags,sector_status=sector_status,
        organization_type=org_types[0],organization_types=org_types,organization_type_status=organization_type_status,
        work_arrangement=infer_work_arrangement(location,context),parent_organization=clean(parent_organization),
        application_deadline=extract_application_deadline(context,today),
        mandate_tags=infer_mandate_tags(context),succession_reason=infer_succession_reason(context),
        location_status="extracted" if location else "unavailable",
        posted_date_status=posted_status if posted else "unavailable",
        evidence=evidence_excerpt(context,role,organization),updated_at=now,missing_runs=0,
        closed_date=None,change_count=0,baseline_seed=False,
        link_quality="direct" if direct else "source_page",
    )

def sibling_block_text(node, stop_tags=("h1","h2","h3"), limit=40) -> tuple[list[str], list]:
    """Collect nearby text/tags after a heading without spilling into the next listing."""
    lines=[]; tags=[]; cur=node.next_sibling; n=0
    while cur is not None and n<limit:
        name=getattr(cur,"name",None)
        if name in stop_tags: break
        if name:
            tags.append(cur)
            for x in cur.stripped_strings:
                c=clean(x)
                if c and (not lines or c!=lines[-1]): lines.append(c)
        cur=cur.next_sibling; n+=1
    return lines,tags


def parse_org_location_line(line: str) -> tuple[str,str]:
    s=clean(line)
    # Moran commonly uses Organization, City, State (remote/hybrid).
    parts=[clean(x) for x in s.split(',')]
    if len(parts)>=3:
        org=', '.join(parts[:-2]); loc=', '.join(parts[-2:])
        if org and loc: return org,loc
    return s,""


def parse_moran(html: str, source: dict, today: date) -> list[Job]:
    soup=BeautifulSoup(html,"lxml"); out=[]
    for h in soup.find_all(["h3","h2"]):
        rt,ro=split_title_org(clean(h.get_text(" ",strip=True)))
        if not is_target_role(rt): continue
        section=h.find_previous(["h1","h2"])
        section_text=clean(section.get_text(" ",strip=True)) if section else ""
        if section_text.lower()!="open positions": continue
        lines,tags=sibling_block_text(h,("h1","h2","h3"),50)
        if not lines: continue
        org_line=lines[0]; org,loc=parse_org_location_line(org_line)
        if ro: org=ro
        href=""
        for tag in tags:
            a=tag.find("a",href=True) if getattr(tag,"find",None) else None
            if a and re.search(r"position profile|full profile|learn more|apply",clean(a.get_text(" ",strip=True)),re.I):
                href=urljoin(source["url"],a.get("href")); break
        context=" | ".join([clean(h.get_text(" ",strip=True))]+lines[:20])
        out.append(build_job(source=source,title=rt,organization=org,location=loc,url=href or source["url"],today=today,context=context))
    return dedupe(out)


def parse_kittleman(html: str, source: dict, today: date) -> list[Job]:
    soup=BeautifulSoup(html,"lxml"); out=[]
    for h in soup.find_all(["h2","h3"]):
        rt,org=split_title_org(clean(h.get_text(" ",strip=True)))
        if not is_target_role(rt): continue
        lines=[]; tags=[]
        for el in h.next_siblings:
            if getattr(el,"name",None) in {"h1","h2","h3"}: break
            if getattr(el,"name",None):
                tags.append(el)
                for x in el.stripped_strings:
                    c=clean(x)
                    if c and (not lines or c!=lines[-1]): lines.append(c)
            if len(lines)>35: break
        # Squarespace can wrap each entry; fall back to local card context.
        if not lines: lines=context_lines(h)
        context=" | ".join([clean(h.get_text(" ",strip=True))]+lines[:35])
        posted,posted_status=extract_date(context,today)
        loc=""
        m=re.search(r"Posted\s+\d{1,2}/\d{1,2}/\d{2,4}\s*[·|\-]\s*([^|]{2,120})",context,re.I)
        if m: loc=clean(m.group(1))
        if not loc: loc=infer_location(lines)
        href=""
        for tag in tags:
            for a in tag.find_all("a",href=True) if getattr(tag,"find_all",None) else []:
                if re.search(r"click here|learn more|apply|position",clean(a.get_text(" ",strip=True)),re.I):
                    href=urljoin(source["url"],a.get("href")); break
            if href: break
        if not href:
            container=local_container(h)
            if container:
                a=container.find("a",href=True,string=re.compile(r"click here|learn more|apply",re.I))
                if a: href=urljoin(source["url"],a.get("href"))
        out.append(build_job(source=source,title=rt,organization=org or infer_organization(rt,lines),location=loc,url=href or source["url"],today=today,context=context,posted=posted,posted_status=posted_status))
    return dedupe(out)


def parse_npag(html: str, source: dict, today: date) -> list[Job]:
    soup=BeautifulSoup(html,"lxml"); out=[]
    for a in soup.find_all("a",href=True):
        rt,ro=split_title_org(clean(a.get_text(" ",strip=True)))
        if not is_target_role(rt): continue
        container=a
        best=a.parent
        # Prefer the *smallest* ancestor that looks like one result card.
        # Keeping the first suitable ancestor prevents a closed neighboring card
        # from contaminating an otherwise active listing.
        for _ in range(7):
            container=getattr(container,"parent",None)
            if not container: break
            txt=clean(container.get_text(" ",strip=True))
            if 28<=len(txt)<=900:
                best=container
                break
            if len(txt)>1800: break
        text=clean(best.get_text(" | ",strip=True)) if best else clean(a.get_text(" ",strip=True))
        if has_closed_signal(text): continue
        lines=[clean(x) for x in best.stripped_strings] if best else [rt]
        org=ro or infer_organization(rt,lines); loc=infer_location(lines)
        url=urljoin(source["url"],a.get("href"))
        out.append(build_job(source=source,title=rt,organization=org,location=loc,url=url,today=today,context=text))
    return dedupe(out)


def _unique_detail_links(soup, source: dict, path_regex: str) -> list[tuple[str, object]]:
    seen=set(); out=[]
    for a in soup.find_all("a", href=True):
        url=urljoin(source["url"], a.get("href"))
        try:
            path=urlparse(url).path or "/"
        except Exception:
            continue
        if not re.search(path_regex, path, re.I):
            continue
        cu=canonical_url(url)
        if cu in seen:
            continue
        seen.add(cu); out.append((url,a))
    return out


def _detail_text_soup(url: str):
    r=fetch(url)
    dsoup=BeautifulSoup(r.text,"lxml")
    pipe=clean(dsoup.get_text(" | ",strip=True))
    plain=clean(dsoup.get_text(" ",strip=True))
    plain=enrich_context_from_linked_profile(dsoup,url,plain)
    return dsoup, pipe, plain


def _field_from_pipe(pipe: str, label: str) -> str:
    # Supports "Label: Value", "Label: | Value", and "Label | Value" table renderings.
    m=re.search(rf"\b{re.escape(label)}(?:\s*:\s*(?:\|\s*)?|\s*\|\s*)([^|]{{1,180}})",pipe,re.I)
    return clean(m.group(1)) if m else ""


def parse_dsg(html: str, source: dict, today: date) -> list[Job]:
    """Only numbered DSG assignment pages count as searches."""
    soup=BeautifulSoup(html,"lxml")
    links=_unique_detail_links(soup,source,r"^/search/\d+(?:-|/)")
    if not links:
        raise ValueError("DSG active-search page contained no assignment detail links")
    out=[]; budget=int(source.get("max_detail_requests",120))
    for url,a in links:
        raw=clean(a.get_text(" ",strip=True))
        if not ROLE_SIGNAL.search(raw):
            continue
        if budget<=0: break
        budget-=1
        try:
            dsoup,pipe,plain=_detail_text_soup(url)
        except Exception:
            continue
        if has_closed_signal(plain):
            continue
        h1=dsoup.find("h1")
        title=clean(h1.get_text(" ",strip=True)) if h1 else ""
        if not is_target_role(title):
            continue
        org=_field_from_pipe(pipe,"Company") or infer_organization(title,[clean(x) for x in dsoup.stripped_strings])
        loc=_field_from_pipe(pipe,"Location") or infer_location([pipe])
        posted,posted_status=extract_labeled_posted_date(pipe,today)
        out.append(build_job(source=source,title=title,organization=org,location=loc,url=url,today=today,context=plain,posted=posted,posted_status=posted_status))
    return dedupe(out)


def parse_lindauer(html: str, source: dict, today: date) -> list[Job]:
    """Only Lindauer open-search assignment links count; service/recent-placement pages do not."""
    soup=BeautifulSoup(html,"lxml")
    links=_unique_detail_links(soup,source,r"^/searches/open-searches/[^/]+/?$")
    if not links:
        raise ValueError("Lindauer open-search page contained no assignment detail links")
    out=[]; budget=int(source.get("max_detail_requests",80))
    for url,a in links:
        title=clean(a.get_text(" ",strip=True))
        if not is_target_role(title):
            continue
        lines=context_lines(a); org=infer_organization(title,lines); loc=infer_location(lines)
        context=" | ".join(lines[:35])
        if budget>0:
            try:
                budget-=1
                dsoup,pipe,plain=_detail_text_soup(url)
                if has_closed_signal(plain):
                    continue
                if org=="Organization not parsed":
                    m=re.search(r"\b([A-Z][^.;|]{2,120}?)\s+seeks\s+(?:an?|its next)\s+"+re.escape(title),plain,re.I)
                    if m:
                        candidate=clean(m.group(1))
                        if likely_org(candidate,title): org=candidate
                if not loc: loc=infer_location([pipe])
                context=plain
            except Exception:
                pass
        out.append(build_job(source=source,title=title,organization=org,location=loc,url=url,today=today,context=context))
    return dedupe(out)


def _target_phrase(text: str) -> str:
    s=clean(text)
    for p in [
        r"\bPresident\s*(?:&|and|/)\s*(?:Chief Executive Officer|CEO)\b",
        r"\bChief Executive Officer\b",
        r"\bExecutive Director\b(?!\s+of\b)",
        r"(?<!Vice\s)(?<!Assistant\s)(?<!Associate\s)\bPresident\b",
        r"\bCEO\b",
    ]:
        m=re.search(p,s,re.I)
        if m: return clean(m.group(0))
    return ""


def parse_batten(html: str, source: dict, today: date) -> list[Job]:
    """Only Batten /open-searches/ assignment cards count; service-type pages are excluded."""
    soup=BeautifulSoup(html,"lxml")
    links=_unique_detail_links(soup,source,r"^/open-searches/[^/]+/?$")
    if not links:
        raise ValueError("Batten jobs page contained no /open-searches/ assignment links")
    out=[]
    for url,a in links:
        raw=clean(a.get_text(" ",strip=True)); title=_target_phrase(raw)
        if not is_target_role(title): continue
        loc=infer_location([raw]); org="Organization not parsed"; context=raw
        m=re.search(r"\bAbout\s+(.{2,120}?)(?=\s+(?:Since|Founded|Established|Fueled|Headquartered|Based|With|The organization|The Foundation|The Association|is a|was founded)\b)",raw,re.I)
        if m:
            candidate=clean(m.group(1))
            if likely_org(candidate,title): org=candidate
        try:
            dsoup,pipe,plain=_detail_text_soup(url)
            if has_closed_signal(plain): continue
            h1=dsoup.find("h1")
            if h1 and is_target_role(clean(h1.get_text(" ",strip=True))): title=clean(h1.get_text(" ",strip=True))
            if org=="Organization not parsed":
                m=re.search(r"\bAbout\s+(.{2,120}?)(?=\s+(?:Since|Founded|Established|Fueled|Headquartered|Based|With|The organization|The Foundation|The Association|is a|was founded)\b)",plain,re.I)
                if m:
                    candidate=clean(m.group(1))
                    if likely_org(candidate,title): org=candidate
            if not loc: loc=infer_location([pipe])
            context=plain
        except Exception:
            pass
        out.append(build_job(source=source,title=title,organization=org,location=loc,url=url,today=today,context=context))
    return dedupe(out)


def parse_odgers(html: str, source: dict, today: date) -> list[Job]:
    """Scope Odgers strictly to its CURRENT OPPORTUNITIES section."""
    soup=BeautifulSoup(html,"lxml"); headings=soup.find_all(["h2","h3","h4"])
    in_section=False; marker_seen=False; out=[]
    for h in headings:
        txt=clean(h.get_text(" ",strip=True))
        if re.search(r"\bcurrent opportunities\b",txt,re.I):
            in_section=True; marker_seen=True; continue
        if in_section and re.fullmatch(r"join us",txt,re.I): break
        if not in_section: continue
        title,org=split_title_org(txt)
        if not is_target_role(title): continue
        lines,tags=sibling_block_text(h,("h2","h3","h4"),60); context=" | ".join([txt]+lines[:40])
        href=""
        for tag in tags:
            for a in tag.find_all("a",href=True) if getattr(tag,"find_all",None) else []:
                if re.search(r"find out more|learn more|position brief|apply",clean(a.get_text(" ",strip=True)),re.I):
                    href=urljoin(source["url"],a.get("href")); break
            if href: break
        if not href:
            a=h.find_next("a",href=True)
            if a and re.search(r"find out more|learn more|position brief|apply",clean(a.get_text(" ",strip=True)),re.I): href=urljoin(source["url"],a.get("href"))
        loc=infer_location(lines)
        if not org: org=infer_organization(title,[txt]+lines)
        out.append(build_job(source=source,title=title,organization=org,location=loc,url=href or source["url"],today=today,context=context))
    if not marker_seen:
        raise ValueError("Odgers page did not expose a CURRENT OPPORTUNITIES section")
    return dedupe(out)


def parse_bridge(html: str, source: dict, today: date) -> list[Job]:
    """Parse only Bridge Partners' Searches-page assignments, never function/practice pages."""
    soup=BeautifulSoup(html,"lxml"); out=[]; seen=set()
    for a in soup.find_all("a",href=True):
        raw=clean(a.get_text(" ",strip=True)); title,_=split_title_org(raw)
        if not is_target_role(title): continue
        href=urljoin(source["url"],a.get("href")); cu=canonical_url(href)
        # On this page Full Description links are frequently PDFs; role anchors may point to the same PDF.
        if cu in seen: continue
        lines=context_lines(a); org=""
        # Nearby all-caps client line is the strongest signal on Bridge's Searches page.
        for line in lines[:14]:
            c=clean(line)
            if c.upper()==c and 2<=len(c)<=120 and likely_org(c,title): org=c.title() if c.isupper() else c; break
        if not org:
            joined=" | ".join(lines[:20])
            m=re.search(r"Bridge Partners is (?:again )?partnering with\s+(.{2,120}?)\s+to recruit",joined,re.I)
            if m: org=clean(m.group(1))
        if not org: org=infer_organization(title,lines)
        context=" | ".join([title]+lines[:35]); direct=href
        # Find the nearest PDF/full-description link in the same card if role anchor itself is not useful.
        container=local_container(a)
        if container:
            for link in container.find_all("a",href=True):
                u=urljoin(source["url"],link.get("href")); lab=clean(link.get_text(" ",strip=True))
                if u.lower().split("?",1)[0].endswith(".pdf") or re.search(r"full description|position profile",lab,re.I): direct=u; break
        if direct.lower().split("?",1)[0].endswith(".pdf"):
            extra=extract_pdf_text(direct)
            if extra: context=clean(context+" "+extra)
        seen.add(canonical_url(direct))
        out.append(build_job(source=source,title=title,organization=org,location=infer_location(lines),url=direct or source["url"],today=today,context=context))
    return dedupe(out)


def extract_org_from_detail(title: str, plain: str, pipe: str = "") -> tuple[str,str]:
    """Return (organization,parent organization) from common retained-search prose."""
    text=clean(plain); parent=""
    patterns=[
        r"(?:on behalf of)\s+(?:our client,?\s+)?(?:the\s+)?(.{2,130}?)(?=,\s+(?:an?|the)\b|\s+to\s+(?:identify|recruit|conduct|lead)\b|[.;])",
        r"(?:exclusively retained by|retained by|partnering with|in partnership with)\s+(?:our client,?\s+)?(?:the\s+)?(.{2,130}?)(?=\s+to\s+(?:identify|recruit|conduct|lead)\b|,\s+(?:an?|the)\b|[.;])",
        r"(?:Executive Director|Chief Executive Officer|President(?:\s*&\s*CEO)?)\s+of\s+(.{2,130}?)(?=,\s+(?:an?|the)\b|[.;])",
        r"\b([A-Z][A-Za-z0-9&'’.,()\- ]{2,130})\s+(?:seeks|is seeking|has retained|is recruiting)\s+(?:an?|its next|a new)\s+(?:Chief Executive Officer|CEO|President(?:\s*&\s*CEO)?|Executive Director)",
    ]
    for pat in patterns:
        m=re.search(pat,text,re.I)
        if m:
            cand=clean(m.group(1))
            if likely_org(cand,title): return cand,parent
    return "",parent


def parse_scion(html: str, source: dict, today: date) -> list[Job]:
    soup=BeautifulSoup(html,"lxml"); out=[]; seen=set()
    for a in soup.find_all("a",href=True):
        url=urljoin(source["url"],a.get("href"))
        if not re.search(r"^/job/\d+/?$",urlparse(url).path,re.I): continue
        raw=clean(a.get_text(" ",strip=True))
        if not ROLE_SIGNAL.search(raw): continue
        cu=canonical_url(url)
        if cu in seen: continue
        seen.add(cu)
        try: dsoup,pipe,plain=_detail_text_soup(url)
        except Exception: continue
        if has_closed_signal(plain): continue
        h1=dsoup.find("h1"); raw=clean(h1.get_text(" ",strip=True)) if h1 else clean(a.get_text(" ",strip=True))
        title,_=split_title_org(raw)
        if not is_target_role(title):
            # CATS pages often show title separately in the body.
            for h in dsoup.find_all(["h1","h2","h3"]):
                t=clean(h.get_text(" ",strip=True)); rt,_=split_title_org(t)
                if is_target_role(rt): title=rt; break
        if not is_target_role(title): continue
        org,_=extract_org_from_detail(title,plain,pipe)
        if not org: org=infer_organization(title,[clean(x) for x in dsoup.stripped_strings])
        loc=_field_from_pipe(pipe,"Location") or infer_location([pipe])
        posted,ps=extract_labeled_posted_date(pipe,today)
        out.append(build_job(source=source,title=title,organization=org,location=loc,url=url,today=today,context=plain,posted=posted,posted_status=ps))
    return dedupe(out)


def parse_leaderfit(html: str, source: dict, today: date) -> list[Job]:
    soup=BeautifulSoup(html,"lxml"); out=[]; seen=set()
    for a in soup.find_all("a",href=True):
        url=urljoin(source["url"],a.get("href"))
        if "leaderfit.catsone.com" not in urlparse(url).netloc.lower() or not re.search(r"/careers/\d+/jobs/\d+",urlparse(url).path,re.I): continue
        cu=canonical_url(url)
        if cu in seen: continue
        seen.add(cu)
        try: dsoup,pipe,plain=_detail_text_soup(url)
        except Exception: continue
        if has_closed_signal(plain): continue
        h1=dsoup.find("h1"); raw=clean(h1.get_text(" ",strip=True)) if h1 else clean(a.get_text(" ",strip=True))
        title,org=split_title_org(raw)
        # Example: Chief Executive Officer, Sixth & I
        if not is_target_role(title):
            m=re.search(r"(Chief Executive Officer|Executive Director|President(?:\s*&\s*CEO)?)[,:\-]\s*([^|]{2,120})",plain,re.I)
            if m: title,org=clean(m.group(1)),clean(m.group(2))
        if not is_target_role(title): continue
        if not org:
            m=re.search(r"\bABOUT\s+([^|]{2,100})",pipe,re.I)
            if m and likely_org(m.group(1),title): org=clean(m.group(1))
        if not org: org,_=extract_org_from_detail(title,plain,pipe)
        if not org: org=infer_organization(title,[clean(x) for x in dsoup.stripped_strings])
        loc=_field_from_pipe(pipe,"Location") or infer_location([pipe])
        posted,ps=extract_labeled_posted_date(pipe,today)
        out.append(build_job(source=source,title=title,organization=org,location=loc,url=url,today=today,context=plain,posted=posted,posted_status=ps))
    return dedupe(out)


def parse_developmentguild(html: str, source: dict, today: date) -> list[Job]:
    soup=BeautifulSoup(html,"lxml"); out=[]; seen=set()
    for a in soup.find_all("a",href=True):
        url=urljoin(source["url"],a.get("href")); path=urlparse(url).path
        if not re.search(r"^/current-searches/[^/]+/?$",path,re.I): continue
        cu=canonical_url(url)
        if cu in seen: continue
        seen.add(cu)
        raw=clean(a.get_text(" ",strip=True)); title,_=split_title_org(raw)
        if not is_target_role(title): continue
        org=""
        prev=a.find_previous(["h3","h4","h5","h6"])
        if prev:
            cand=clean(prev.get_text(" ",strip=True))
            if likely_org(cand,title): org=cand
        lines=context_lines(a); loc=infer_location(lines); context=" | ".join(lines[:40])
        try:
            dsoup,pipe,plain=_detail_text_soup(url)
            if has_closed_signal(plain): continue
            if org=="Organization not parsed" or not org:
                org=infer_organization(title,[clean(x) for x in dsoup.stripped_strings])
            if not loc: loc=infer_location([pipe])
            context=plain
        except Exception: pass
        out.append(build_job(source=source,title=title,organization=org,location=loc,url=url,today=today,context=context))
    return dedupe(out)


def parse_isaacson(html: str, source: dict, today: date) -> list[Job]:
    soup=BeautifulSoup(html,"lxml"); out=[]; seen=set()
    for a in soup.find_all("a",href=True):
        url=urljoin(source["url"],a.get("href")); path=urlparse(url).path
        if not re.search(r"^/open-searches/[^/]+/[^/]+/?$",path,re.I): continue
        raw=clean(a.get_text(" ",strip=True))
        if not ROLE_SIGNAL.search(raw): continue
        cu=canonical_url(url)
        if cu in seen: continue
        seen.add(cu)
        try: dsoup,pipe,plain=_detail_text_soup(url)
        except Exception: continue
        if has_closed_signal(plain): continue
        h1=dsoup.find("h1"); title=clean(h1.get_text(" ",strip=True)) if h1 else clean(a.get_text(" ",strip=True))
        if not is_target_role(title): continue
        org=""
        # Isaacson detail convention: h1 role, first h2 client organization | location.
        for h2 in dsoup.find_all("h2"):
            txt=clean(h2.get_text(" ",strip=True))
            if not txt or is_target_role(txt): continue
            left=clean(txt.split("|",1)[0])
            if likely_org(left,title): org=left; break
        if not org: org=infer_organization(title,[clean(x) for x in dsoup.stripped_strings])
        loc=infer_location([pipe])
        # IM dates are known to be unreliable/missing; first seen is the honest fallback.
        out.append(build_job(source=source,title=title,organization=org,location=loc,url=url,today=today,context=plain,posted=None,posted_status="unavailable"))
    return dedupe(out)


def parse_sandler(html: str, source: dict, today: date) -> list[Job]:
    soup=BeautifulSoup(html,"lxml"); out=[]; seen=set()
    for a in soup.find_all("a",href=True):
        url=urljoin(source["url"],a.get("href")); path=urlparse(url).path
        if not re.search(r"^/job/[^/]+/?$",path,re.I): continue
        cu=canonical_url(url)
        if cu in seen: continue
        seen.add(cu)
        try: dsoup,pipe,plain=_detail_text_soup(url)
        except Exception: continue
        if has_closed_signal(plain): continue
        title=_field_from_pipe(pipe,"POSITION")
        org=_field_from_pipe(pipe,"ORGANIZATION")
        loc=_field_from_pipe(pipe,"LOCATION")
        if not is_target_role(title):
            h1=dsoup.find("h1"); title=clean(h1.get_text(" ",strip=True)) if h1 else clean(a.get_text(" ",strip=True))
        if not is_target_role(title): continue
        parent=""
        # RootOne is a program/brand; preserve The Jewish Education Project as parent org if present.
        h1=dsoup.find("h1")
        brand=""
        for h in dsoup.find_all(["h2","h3","h4","h5"]):
            cand=clean(h.get_text(" ",strip=True))
            if cand and cand.lower()!=title.lower() and likely_org(cand,title):
                brand=cand; break
        if brand and brand.lower() not in {title.lower(),org.lower()}:
            if brand.lower()=="rootone" and org: parent=org; org=brand
        if not org: org,_=extract_org_from_detail(title,plain,pipe)
        if not org: org=infer_organization(title,[clean(x) for x in dsoup.stripped_strings])
        posted,ps=extract_labeled_posted_date(pipe,today)
        out.append(build_job(source=source,title=title,organization=org,location=loc,url=url,today=today,context=plain,posted=posted,posted_status=ps,parent_organization=parent))
    return dedupe(out)


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
    if url != source["url"] and not allowed_candidate_url(url, source):
        return None

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
            detail_text=clean(dsoup.get_text(" ",strip=True))[:50000]
            detail_text=enrich_context_from_linked_profile(dsoup,url,detail_text)
            if use_source_dates and posted is None:
                # Detail pages may contain many unrelated dates. Trust only a labeled posting date.
                posted, posted_status=extract_labeled_posted_date(detail_text,today)
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
    # Explicit closure language outranks page presence. This is deliberately narrow
    # so words such as "close collaboration" do not create false closures.
    if has_closed_signal(combined):
        return None
    return build_job(source=source,title=role,organization=organization,location=location,url=url,today=today,context=combined,posted=posted,posted_status=posted_status)


def parse_korn(html: str, source: dict, today: date) -> list[Job]:
    """Parse Korn Ferry's rendered client board and verify every target-role detail page."""
    soup=BeautifulSoup(html,"lxml"); seen=set(); out=[]; links=[]
    for a in soup.find_all("a",href=True):
        url=urljoin(source["url"],a.get("href"))
        if not re.search(r"/job/Korn-Ferry-Executive-Search-[^?#]+/\d+/?$",urlparse(url).path,re.I): continue
        cu=canonical_url(url)
        if cu not in seen: seen.add(cu); links.append(url)
    if not links: raise ValueError("Korn Ferry rendered board exposed no client job detail links")
    for url in links[:int(source.get("max_detail_requests",240))]:
        try: dsoup,pipe,plain=_detail_text_soup(url)
        except Exception: continue
        if re.search(r"Job Expired or Not Found",plain,re.I) or has_closed_signal(plain): continue
        h1=dsoup.find("h1"); raw=clean(h1.get_text(" ",strip=True)) if h1 else ""
        title,org_from_title=split_title_org(raw)
        if not org_from_title:
            for sep in (" - "," – "," — "):
                if sep in raw:
                    left,right=[clean(x) for x in raw.rsplit(sep,1)]
                    if is_target_role(right) and len(right)<=60:
                        title,org_from_title=right,left; break
        if not is_target_role(title): continue
        loc=_field_from_pipe(pipe,"Location") or infer_location([pipe])
        posted,posted_status=extract_labeled_posted_date(pipe,today)
        org=org_from_title
        # Korn details often use explicit 'The Organization' or an opening descriptive paragraph.
        if not org:
            m=re.search(r"(?:The Organization|About the Organization|The Company|About the Company)\s*[|:]?\s*([^|]{2,140})",pipe,re.I)
            if m:
                cand=clean(m.group(1))
                # If the first chunk is generic prose, take leading proper-name phrase before 'is'.
                mm=re.match(r"(.{2,120}?)\s+is\s+(?:one of|a |an |the )",cand,re.I)
                cand=clean(mm.group(1)) if mm else cand
                if likely_org(cand,title): org=cand
        if not org:
            m=re.search(r"(?:The Organization|About the Organization|The Company|About the Company)\s+([A-Z][^.!?]{2,140}?)\s+is\s+(?:one of|a |an |the )",plain,re.I)
            if m and likely_org(m.group(1),title): org=clean(m.group(1))
        if not org:
            # 'The Design-Build Institute of America (DBIA) is...' style opening paragraph.
            for pat in [r"\b([A-Z][A-Za-z0-9&'’.,()\- ]{3,130}\([A-Z]{2,10}\))\s+is\s+",r"\b([A-Z][A-Za-z0-9&'’.,()\- ]{3,130})\s+is\s+(?:one of|a |an |the nation)"]:
                m=re.search(pat,plain)
                if m:
                    cand=clean(m.group(1))
                    if likely_org(cand,title): org=cand; break
        if not org: org=infer_organization(title,[clean(x) for x in dsoup.stripped_strings])
        out.append(build_job(source=source,title=title,organization=org,location=loc,url=url,today=today,context=plain,posted=posted,posted_status=posted_status))
    return dedupe(out)

def candidates_from_html(html: str, source: dict, today: date) -> list[Job]:
    parser=source.get("parser")
    if parser=="moran": return parse_moran(html,source,today)
    if parser=="kittleman": return parse_kittleman(html,source,today)
    if parser=="npag": return parse_npag(html,source,today)
    if parser=="dsg": return parse_dsg(html,source,today)
    if parser=="lindauer": return parse_lindauer(html,source,today)
    if parser=="batten": return parse_batten(html,source,today)
    if parser=="odgers": return parse_odgers(html,source,today)
    if parser=="bridge": return parse_bridge(html,source,today)
    if parser=="scion": return parse_scion(html,source,today)
    if parser=="leaderfit": return parse_leaderfit(html,source,today)
    if parser=="developmentguild": return parse_developmentguild(html,source,today)
    if parser=="isaacson": return parse_isaacson(html,source,today)
    if parser=="sandler": return parse_sandler(html,source,today)
    if parser=="korn": return parse_korn(html,source,today)
    soup=BeautifulSoup(html,"lxml")
    budget=[int(source.get("max_detail_requests",MAX_DETAIL_REQUESTS_PER_SOURCE))]
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
    fields=Job.__dataclass_fields__; kwargs={}
    from dataclasses import MISSING
    for k,f in fields.items():
        if k in raw: kwargs[k]=raw[k]
        elif f.default is not MISSING: kwargs[k]=f.default
        elif f.default_factory is not MISSING: kwargs[k]=f.default_factory()
        else: kwargs[k]=None
    for key in ["id","title","organization","source","location","url","source_url","first_seen","last_seen","date_basis"]:
        if kwargs.get(key) is None: kwargs[key]=""
    if not kwargs.get("role_type"): kwargs["role_type"]=role_type(kwargs.get("title",""))
    if not kwargs.get("updated_at"): kwargs["updated_at"]=kwargs.get("last_seen","")
    if not kwargs.get("sector_tags"):
        kwargs["sector_tags"]=[kwargs.get("sector") or "Unclassified"]
    if not kwargs.get("organization_types"):
        kwargs["organization_types"]=[kwargs.get("organization_type") or "Unclassified"]
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
    for attr in ["organization","location","posted_date","compensation_text","compensation_min","compensation_max","sector","sector_tags","organization_type","organization_types","parent_organization","application_deadline","mandate_tags","succession_reason","latest_reported_ceo_comp","latest_reported_ceo_comp_year","latest_reported_ceo_name","latest_reported_ceo_comp_source","org_revenue","org_assets","org_ein"]:
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
    job.organization_type_status = prior.get("organization_type_status",job.organization_type_status) if job.organization_type==prior.get("organization_type") else job.organization_type_status
    job.location_status = "extracted" if job.location else prior.get("location_status","unavailable")
    job.work_arrangement=infer_work_arrangement(job.location,job.evidence)
    job.link_quality="direct" if job.url and job.url!=job.source_url else prior.get("link_quality",job.link_quality)
    return job


def changed_fields(prior: dict, current: Job) -> dict:
    changes={}
    for fieldname in ["title","organization","location","url","posted_date","compensation_text","compensation_min","compensation_max","sector_tags","organization_types","work_arrangement","application_deadline","mandate_tags","succession_reason","latest_reported_ceo_comp","org_revenue","org_assets"]:
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

    errors=[]; all_found=[]; fetch_modes=[]; parse_successes=0
    urls=source.get("urls") or [source["url"]]
    for url in urls:
        html=""; page_found=[]
        if source.get("browser_first"):
            try:
                html=(browser_korn_html(url) if source.get("parser")=="korn" else browser_html(url,int(source.get("browser_max_scrolls",8)))); fetch_modes.append("browser")
                page_found=candidates_from_html(html,{**source,"url":url},today)
                parse_successes+=1
                all_found.extend(page_found)
            except Exception as exc:
                errors.append(f"browser {type(exc).__name__}: {exc}")
        if not source.get("browser_first") or not page_found:
            try:
                r=fetch(url); html=r.text; fetch_modes.append("static")
                page_found=candidates_from_html(html,{**source,"url":url},today)
                parse_successes+=1
                all_found.extend(page_found)
            except Exception as exc:
                errors.append(f"static {type(exc).__name__}: {exc}")
        # Browser rendering is a fallback when static returned no useful roles for this page.
        if source.get("render_fallback") and not page_found and not source.get("browser_first"):
            try:
                html=(browser_korn_html(url) if source.get("parser")=="korn" else browser_html(url,int(source.get("browser_max_scrolls",8)))); fetch_modes.append("browser")
                page_found=candidates_from_html(html,{**source,"url":url},today)
                parse_successes+=1
                all_found.extend(page_found)
            except Exception as exc:
                errors.append(f"browser {type(exc).__name__}: {exc}")

    found=dedupe(all_found)
    health["count"]=len(found); health["fetch_mode"]="+".join(dict.fromkeys(fetch_modes))
    if not fetch_modes or parse_successes==0:
        health["ok"]=False; health["status"]="failed"; health["preserved"]=True
        health["error"]=("No source page parsed successfully. " + " | ".join(errors))[:500]
        return [],health

    min_expected=int(source.get("min_expected_matches",0) or 0)
    if min_expected and len(found)<min_expected:
        health["ok"]=False; health["status"]="partial-suspected"; health["preserved"]=True
        health["error"]=(f"Found only {len(found)} matching roles; this source is expected to expose at least {min_expected}. Preserving prior roles and flagging coverage for review. " + " | ".join(errors))[:500]
        return found,health

    # Unexpected collapses are treated as partial, not as mass closures.
    if prior_count>=5 and len(found)<max(2,int(prior_count*0.35)) and not source.get("allow_zero",False) and not source.get("authoritative_parser",False):
        health["ok"]=False; health["status"]="partial-suspected"; health["preserved"]=True
        health["error"]=(f"Found {len(found)} vs {prior_count} previously open; preserving prior roles pending another healthy parse. " + " | ".join(errors))[:500]
        return found,health

    health["ok"]=True
    health["status"]="ok" if found else "checked-no-matches"
    health["error"]=" | ".join(errors)[:500]
    return found,health


def _state_from_location(location: str) -> str:
    m=re.search(r",\s*([A-Z]{2})\b",location or "")
    return m.group(1) if m else ""


def enrich_nonprofit_990(jobs: list[dict], limit: int = 20) -> None:
    """Best-effort public Form 990 enrichment; never gates a listing."""
    done=0
    for j in jobs:
        if done>=limit: break
        if j.get("latest_reported_ceo_comp") or j.get("org_ein"): continue
        org=j.get("organization","")
        if not org or org=="Organization not parsed": continue
        ots=j.get("organization_types") or [j.get("organization_type","")]
        eligible=any(any(x in ot for x in ["Association","Foundation","University","Education","Health","Media","Arts","Advocacy","Human Services","Nonprofit"]) for ot in ots)
        if not eligible: continue
        try:
            q=requests.utils.quote(org[:120])
            data=fetch(f"https://projects.propublica.org/nonprofits/api/v2/search.json?q={q}").json()
            orgs=data.get("organizations") or []
            if not orgs: continue
            target=normalized(org); state=_state_from_location(j.get("location","")); scored=[]
            for o in orgs[:25]:
                name=normalized(o.get("name","")); score=0
                if name==target: score+=100
                elif target in name or name in target: score+=65
                score+=len(set(target.split()) & set(name.split()))*5
                if state and str(o.get("state","")).upper()==state: score+=15
                scored.append((score,o))
            score,o=max(scored,key=lambda x:x[0])
            if score<55: continue
            ein=str(o.get("ein") or "")
            if not ein: continue
            j["org_ein"]=ein
            detail=fetch(f"https://projects.propublica.org/nonprofits/api/v2/organizations/{ein}.json").json()
            filings=detail.get("filings_with_data") or []
            if filings:
                f=filings[0]
                j["org_revenue"]=f.get("totrevenue") or f.get("totrev")
                j["org_assets"]=f.get("totassetsend") or f.get("totassets")
            page=fetch(f"https://projects.propublica.org/nonprofits/organizations/{ein}").text
            ps=BeautifulSoup(page,"lxml")
            best=None
            for tr in ps.find_all("tr"):
                cells=[clean(x.get_text(" ",strip=True)) for x in tr.find_all(["th","td"])]
                if len(cells)<2: continue
                name_role=cells[0]
                if not re.search(r"\b(CEO|Chief Executive|President|Executive Director)\b",name_role,re.I): continue
                vals=[]
                for c in cells[1:4]:
                    m=re.search(r"\$([\d,]+)",c)
                    if m: vals.append(int(m.group(1).replace(",","")))
                if vals:
                    total=sum(vals)
                    if best is None or total>best[0]: best=(total,name_role)
            if best:
                j["latest_reported_ceo_comp"]=best[0]
                j["latest_reported_ceo_name"]=best[1]
                j["latest_reported_ceo_comp_source"]=f"https://projects.propublica.org/nonprofits/organizations/{ein}"
                years=re.findall(r"Fiscal Year Ending[^0-9]*(20\d{2})",clean(ps.get_text(" ",strip=True)))
                if years: j["latest_reported_ceo_comp_year"]=int(years[0])
            done+=1
            time.sleep(.08)
        except Exception:
            continue


def main() -> int:
    today=datetime.now(timezone.utc).date(); now=now_iso()
    sources=load_json(SOURCES_PATH,[])
    history_payload=load_json(HISTORY_PATH,{"jobs":[]}); history_jobs=history_payload.get("jobs",[])
    changes_payload=load_json(CHANGES_PATH,{"events":[]}); events=changes_payload.get("events",[])
    old_open=old_open_by_source(history_jobs)
    updated_history=[dict(x) for x in history_jobs]
    by_id={x.get("id"):x for x in updated_history if x.get("id")}
    health_rows=[]
    seen_ids=set()

    # v6 hygiene: immediately archive legacy false positives that are obviously
    # marketing/service/insight URLs or violate a source-specific detail allowlist.
    source_map={x.get("name"):x for x in sources}
    for row in updated_history:
        if row.get("status")!="open": continue
        cfg=source_map.get(row.get("source"),{})
        url=row.get("url","")
        bad=blocked_nonlisting_url(url,cfg.get("url",""))
        allow=cfg.get("allowed_detail_path_regex")
        if allow and url and canonical_url(url)!=canonical_url(cfg.get("url","")):
            try: bad=bad or not bool(re.search(allow,urlparse(url).path or "/",re.I))
            except Exception: pass
        if bad:
            row["status"]="closed"; row["closed_date"]=today.isoformat(); row["updated_at"]=now
            events.append({"at":now,"type":"removed_false_positive","job_id":row.get("id"),"source":row.get("source"),"title":row.get("title"),"organization":row.get("organization"),"url":url})

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
            close_after=int(source.get("close_after_misses",CLOSE_AFTER_MISSES))
            if misses>=close_after:
                current["status"]="closed"; current["closed_date"]=today.isoformat(); current["updated_at"]=now
                events.append({"at":now,"type":"closed","job_id":pid,"source":current.get("source"),"title":current.get("title"),"organization":current.get("organization")})
            by_id[pid]=current

    # Rebuild history and current-open feed. Age alone never removes a role.
    # If a source still presents an old search as active, it remains market data;
    # closure comes from explicit source status or repeated healthy disappearance.
    all_history=list(by_id.values())
    current=[]
    for j in all_history:
        if j.get("status")!="open": continue
        if j.get("posted_date"):
            try:
                date.fromisoformat(j["posted_date"])
            except ValueError:
                j["posted_date"]=None; j["date_basis"]="first_seen"; j["posted_date_status"]="unavailable"
        current.append(j)

    # Enrich a bounded number of eligible nonprofit records per run with public 990 data.
    enrich_nonprofit_990(current,limit=20)

    def recency(j): return j.get("posted_date") or (j.get("first_seen") or "")[:10] or "0000-00-00"
    current.sort(key=lambda j:(recency(j),j.get("source",""),j.get("organization","")),reverse=True)
    all_history.sort(key=lambda j:((j.get("first_seen") or ""),j.get("source",""),j.get("organization","")),reverse=True)
    events=events[-MAX_CHANGE_EVENTS:]

    baseline=history_payload.get("baseline_initialized_at") or history_payload.get("generated_at") or now
    JOBS_PATH.write_text(json.dumps({"generated_at":now,"baseline_initialized_at":baseline,"jobs":current},indent=2,ensure_ascii=False),encoding="utf-8")
    HISTORY_PATH.write_text(json.dumps({"generated_at":now,"baseline_initialized_at":baseline,"jobs":all_history},indent=2,ensure_ascii=False),encoding="utf-8")
    CHANGES_PATH.write_text(json.dumps({"generated_at":now,"events":events},indent=2,ensure_ascii=False),encoding="utf-8")
    META_PATH.write_text(json.dumps({"generated_at":now,"baseline_initialized_at":baseline,"source_count":len(sources),"sources":health_rows},indent=2,ensure_ascii=False),encoding="utf-8")
    print(f"Wrote {len(current)} open roles; {len(all_history)} total historical roles; {len(sources)} tracked sources.")
    return 0


if __name__=="__main__":
    raise SystemExit(main())
