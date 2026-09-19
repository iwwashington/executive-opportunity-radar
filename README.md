# Executive Opportunity Radar

Personal executive-search aggregator with a static dashboard and scheduled public-source refresh.

## GitHub Pages

The dashboard is deployable directly from the repository root. In GitHub: **Settings → Pages → Deploy from a branch → main / root**.

## Automated refresh

The repository includes `.github/workflows/update.yml`, which runs the scraper daily and writes refreshed data to `data/jobs.json` and `data/meta.json`.

If the hidden `.github` folder is lost during a browser upload, use the visible `UPDATE-WORKFLOW.yml` file as the source for a new file named `.github/workflows/update.yml` in GitHub.
