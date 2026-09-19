# Executive Opportunity Radar v5

This patch supersedes the unuploaded v4/v4.1 patches. Upload these files together, then run the existing GitHub Action once.

## v5 additions
- Korn Ferry is now browser-first with a dedicated parser for direct client-job detail URLs; it scrolls the rendered board and verifies each CEO/president/executive-director page.
- Compensation extraction is stricter and richer: it reads labeled salary/compensation ranges on detail pages and, when needed, linked position/leadership-profile PDFs.
- Organization Type is separate from Sector, enabling filters such as Association / Professional Society, Foundation / Philanthropy, College / University, Health System / Provider, and other nonprofit types.
- The Compensation disclosed market card is clickable and opens the filtered opportunity feed.
- A compensation filter supports posted compensation and latest reported CEO compensation.
- Best-effort nonprofit enrichment uses public ProPublica Nonprofit Explorer / IRS Form 990 data for latest reported CEO compensation, revenue, and assets. These are explicitly historical reported figures, never labeled as current salary. Enrichment is bounded per run and never gates a role.
- Existing v4.1 protections remain: direct assignment URLs, closed/filled detection, posted-date semantics, stale-source preservation, and source-specific DSG/Moran/NPAG/Lindauer/Batten/Odgers logic.

## Upload
Replace these files in the repository root:
- aggregate.py
- sources.json
- index.html
- requirements.txt
- README.md

Do not replace jobs.json, history.json, changes.json, meta.json, or the workflow. Run the Action once after upload.
