# Executive Opportunity Radar — v4 patch

This is a **code/config patch** for the live radar. It intentionally does not include `jobs.json`, `history.json`, `changes.json`, or `meta.json`, so uploading it will not overwrite the history already collected by the live site.

## v4 fixes

- Fixes the **All firms** and **All sectors** dropdowns on Opportunities.
- Uses the source-reported **Posted date** as the primary date; `First seen` is only the fallback.
- Does **not** remove a role merely because it is old. If a source still presents it as active, it remains visible and is labeled `Open N days` after 90 days.
- Avoids a misleading `New` badge when the radar discovers a role that has an older published posting date.
- Adds `Verified <date>` to cards from the most recent healthy observation.
- Adds source-specific handling for **The Moran Company**, **Kittleman Associates**, and **NPAG**.
- **Moran:** reads only Open Positions into the active feed; Positions Filled are excluded immediately.
- **NPAG:** excludes cards that say `No Longer Accepting Applications`.
- **DSG:** follows detail pages and rejects explicit closed/no-longer-accepting language.
- **Kittleman:** preserves exact posted dates and prefers the role-specific link.
- **WittKieffer:** tries browser rendering first to work around the static 403.
- Sources with explicit status handling can close known false-active records after one healthy miss; failed/partial source checks still preserve prior roles.

## Upload

Upload the four files in this patch to the root of the GitHub repository and replace the existing files:

- `index.html`
- `aggregate.py`
- `sources.json`
- `README.md`

Then run **Actions → Update Executive Opportunity Radar → Run workflow** once. The existing workflow does not need to be replaced.
