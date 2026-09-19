# Executive Opportunity Radar v6

v6 is the consolidated replacement for the unuploaded v4/v4.1/v5 patches. It keeps the existing live history and workflow while replacing the scraper, source registry, interface, and dependencies.

## What changed in v6

### Stricter opportunity detection
- Rejects obvious marketing/service/practice/about/insights/function pages globally.
- Uses source-specific opportunity URL patterns wherever possible instead of treating any link containing “CEO,” “President,” or “Executive Director” as a job.
- Immediately archives legacy false positives after a healthy v6 run instead of leaving them in the active feed.
- Preserves previously known roles when a source fails or returns a suspiciously incomplete result.

### Organization-name extraction
Dedicated parsing now handles the exact failure patterns found during QA, including:
- DSG `Company:` fields.
- LeaderFit CATS titles such as `Chief Executive Officer, Sixth & I`.
- Isaacson, Miller client headings such as Rochester Institute of Technology.
- Scion detail-page language such as `retained by`, `on behalf of`, and `Executive Director of ...`.
- Sandler `ORGANIZATION` fields and program/parent-organization relationships.
- Korn Ferry `About the Organization` / `About the Company` sections.

### Korn Ferry coverage
- Korn Ferry is browser-first and has its own parser.
- The browser pass searches the opportunity portal for CEO, President, and Executive Director terms, scrolls/paginates results, collects direct Korn Ferry Executive Search job URLs, then verifies each detail page.
- A suspiciously low-result Korn Ferry run is marked partial and does not wipe previously known roles.

### Dates, status, and history
- Trustworthy source-posted dates are primary.
- `First seen` is used only when a reliable posted date is unavailable.
- `Last verified` is tracked separately.
- `NEW` is based on a recent real posted date when available, not merely the day the Radar discovered an old role.
- Closed, filled, no-longer-accepting, and expired-deadline roles are removed from the active feed but retained in history.
- Long-running searches remain visible when the source still verifies them as active.

### Multi-tag organization and sector classification
Organization type and sector are separate, overlapping dimensions. A search can therefore be both:
- `Association / Professional Society` and `Health`
- `Foundation / Philanthropy` and `Education`
- `College / University` and `Health`

The Opportunities filters and Market view use the multi-tag arrays rather than forcing each organization into one mutually exclusive bucket.

### Compensation
- Reads compensation from list pages, detail pages, and linked position/leadership-profile PDFs.
- Handles ranges such as `$425,000-$475,000`, shorthand ranges such as `$340k-$375k`, and minimum-only language such as `starting at $230,000`.
- `Compensation disclosed` is clickable and can filter the opportunity feed.

### Nonprofit Form 990 enrichment
v6 uses ProPublica Nonprofit Explorer as the practical public enrichment source for eligible nonprofit organizations. It can add, when confidently matched:
- latest reported CEO/executive compensation and filing year
- reported executive name
- revenue
- assets
- EIN

These values are historical Form 990 data and are labeled **Latest reported CEO compensation**, never “current salary.” Enrichment is best-effort, bounded per run, and never determines whether a role is included.

### Additional market intelligence
The data model can now retain:
- organization type and sector tags
- application deadline
- work arrangement
- organization revenue/assets
- leadership mandate themes
- succession reason where stated
- parent organization where relevant

The Market view includes organization mix, leadership mandate signals, and a seven-day “What changed” summary in addition to the opportunity feed.

## Exact deployment steps

Upload **only these five files** from this v6 package to the root of the existing GitHub repository:

- `aggregate.py`
- `sources.json`
- `index.html`
- `requirements.txt`
- `README.md`

Do **not** delete or replace:

- `jobs.json`
- `history.json`
- `changes.json`
- `meta.json`
- `.github/workflows/update-radar.yml`

Those files preserve accumulated history and the working deployment automation.

After committing the five replacements:

1. Click **Actions** at the top of the GitHub repository.
2. In the left sidebar, click **Update Executive Opportunity Radar**.
3. Click **Run workflow** on the right.
4. Click the green **Run workflow** button.
5. Wait for that run to finish with a green check.
6. Hard-refresh the live GitHub Pages site.

The first v6 run may take longer than earlier runs because Korn Ferry uses a deeper browser crawl and nonprofit enrichment performs additional public-source lookups.

## QA completed before packaging
- Python compile check passed.
- `sources.json` validation passed.
- Browser JavaScript syntax check passed.
- Synthetic parser regression tests passed for the specific failure patterns identified during live review: DSG organization/date extraction; LeaderFit/Sixth & I; Isaacson, Miller/RIT; Scion organization extraction; Sandler organization extraction; Korn Ferry/DBIA; and rejection of false-positive service/about/insights/function URLs.

The first GitHub Action run remains the live-environment stress test for dynamic sites, blocking behavior, and source-page changes.
