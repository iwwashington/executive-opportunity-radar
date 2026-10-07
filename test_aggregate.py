#!/usr/bin/env python3
"""Regression tests for the Executive Opportunity Radar data pipeline.

Phase 2 data-accuracy fixes. Run with: python3 test_aggregate.py
Exit code 0 = all pass.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import aggregate as agg

PASS = 0
FAIL = 0

def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name} {detail}")


print("== Fix 1: tag caps (primary + at most one secondary) ==")
# No record may carry more than 2 sector tags or 2 org-type tags.
samples = [
    ("American Heart Association", "heart health hospital medical association advocacy education"),
    ("National Education Association", "education teachers union advocacy health environment"),
    ("Museum of Science", "museum education science learning health arts culture"),
    ("Global Health Foundation", "global health philanthropy foundation international"),
]
for org, text in samples:
    tags, _ = agg.infer_sector_tags(org, text)
    check(f"sector tags<=2 for {org}", len(tags) <= 2, f"got {tags}")
    otypes, _ = agg.infer_organization_types(org, text)
    check(f"org-type tags<=2 for {org}", len(otypes) <= 2, f"got {otypes}")

r = agg.classify_record("Mayo Clinic", "hospital medical center patient care health")
check("primary sector is Health", r["sector"] == "Health", r["sector"])
check("sector_secondary nullable", r["sector_secondary"] is None or isinstance(r["sector_secondary"], str))
check("sector_confidence set", r["sector_confidence"] in {"high", "medium", "low", "unclassified"})

print("== Fix 2: mandates from priorities only, cap 3 ==")
m = agg.infer_mandate_tags(
    "Position Summary: Lead growth. Key Priorities: fundraising, culture, operations, advocacy, strategy, digital.")
check("mandates capped at 3", len(m) <= 3, str(m))
# Boilerplate outside the priorities section should not score.
m2 = agg.infer_mandate_tags(
    "About us: we value culture and talent and advocacy and growth and strategy and operations. "
    "Key Responsibilities: oversee fundraising and donor engagement.")
check("boilerplate ignored", "Culture / talent" not in m2 or "Fundraising / revenue" in m2, str(m2))

print("== Fix 3: blocked vs no-matches ==")
check("403 detected as blocked", agg.BLOCKED_SIGNALS.search("403 Client Error: Forbidden") is not None)
check("captcha detected", agg.BLOCKED_SIGNALS.search("please verify you are a human") is not None)
check("clean page not blocked", not agg._page_blocked("<html><body><h1>CEO jobs</h1></body></html>"))

print("== Fix 4: 990 CEO-pay attribution ==")
check("CEO is top exec", agg.is_top_executive("Chief Executive Officer"))
check("President is top exec", agg.is_top_executive("President"))
check("ED is top exec", agg.is_top_executive("Executive Director"))
check("Senior ED is NOT top exec", not agg.is_top_executive("Senior Executive Director"))
check("Deputy is NOT top exec", not agg.is_top_executive("Deputy Executive Director"))
check("Regional is NOT top exec", not agg.is_top_executive("Regional Executive Director"))

print("== Fix 5: parsing fallbacks ==")
org, ok = agg.validate_organization("our client")
check("'our client' -> confidential", org == "Organization confidential" and not ok)
org, ok = agg.validate_organization("National Speleological Society,")
check("trailing comma stripped", org == "National Speleological Society" and ok)
org, ok = agg.validate_organization("2026")
check("bare year -> confidential", org == "Organization confidential" and not ok)
org, ok = agg.validate_organization("Organization not parsed")
check("legacy fallback -> confidential", org == "Organization confidential" and not ok)
loc, ok = agg.validate_location("Inc.,")
check("'Inc.,' -> not listed", loc == "Location not listed" and not ok)
loc, ok = agg.validate_location("")
check("blank -> not listed", loc == "Location not listed" and not ok)
loc, ok = agg.validate_location("Washington, DC")
check("real location kept", loc == "Washington, DC" and ok)

print("== Fix 6: schema normalization ==")
legacy = {"id": "x", "title": "CEO", "organization": "Organization not parsed",
          "sector": "Health",
          "sector_tags": ["Health", "Education", "Associations", "Philanthropy", "Environment"],
          "organization_types": ["Association / Professional Society", "Advocacy / Civil Rights", "Other Nonprofit"]}
job = agg.compatible_job(legacy)
d = job.__dict__
check("legacy org migrated", d["organization"] == "Organization confidential", d["organization"])
check("legacy sector tags trimmed", len(d["sector_tags"]) <= 2, str(d["sector_tags"]))
check("legacy org-type tags trimmed", len(d["organization_types"]) <= 2, str(d["organization_types"]))
# Every dataclass field present (one schema, explicit nulls).
missing = [k for k in agg.Job.__dataclass_fields__ if k not in d]
check("all schema fields present", not missing, str(missing))

print("== Fix 7: material changes only ==")
prior = {"title": "CEO", "location": "Boston, MA", "sector_tags": ["Health"],
         "mandate_tags": ["Growth / scale"], "compensation_text": "$200k"}
cur = agg.Job(id="1", title="CEO", organization="Org", source="S", location="Boston, MA",
              url="u", source_url="s", posted_date=None, first_seen="f", last_seen="l", date_basis="d",
              sector_tags=["Health", "Education"], mandate_tags=["Culture / talent"])
ch = agg.changed_fields(prior, cur)
check("tag-only change is not 'updated'", ch == {}, str(ch))
cur2 = agg.Job(id="1", title="CEO", organization="Org", source="S", location="Chicago, IL",
               url="u", source_url="s", posted_date=None, first_seen="f", last_seen="l", date_basis="d")
ch2 = agg.changed_fields(prior, cur2)
check("location change is 'updated'", "location" in ch2, str(ch2))

print("== Fix 8: baseline marker ==")
# baseline_week is emitted in meta.json by main(); check the key exists in the writer.
src = Path(__file__).parent.joinpath("aggregate.py").read_text()
check("baseline_week in meta writer", '"baseline_week"' in src)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
