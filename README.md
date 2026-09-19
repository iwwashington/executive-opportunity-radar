# Executive Opportunity Radar

A private career-monitoring dashboard for CEO, president, and executive director searches.

## Source of truth

The monitored firms and source status come from Irving Washington's Google Drive document **CEO Listings Site - Check #2**. The configuration currently tracks 33 unique sources, including sources that are automated, manual-check only, retired, legacy/duplicate, or do not publish public search listings.

## How dates work

- When a source publishes a reliable posting date, the radar uses it.
- When a source only publishes a relative age (for example, "posted 3 weeks ago"), the radar converts that to an approximate date and labels it accordingly.
- When a source does not expose a reliable posting date, the radar displays **First seen** instead of inventing a date.
- Isaacson, Miller and Lindauer are explicitly configured to use first-seen dates because their public dates have previously been unreliable in the old aggregator.
- The default view is **Newest first**, grouped by date.

## Role filter

The automated feed is intentionally narrow:

- Chief Executive Officer / CEO
- President / President & CEO
- Executive Director

It rejects obvious false positives such as vice presidents and functional titles such as "Executive Director of Annual Giving."

## Daily refresh

GitHub Actions runs `aggregate.py` once per day and rewrites `jobs.json` and `meta.json`. One broken search-firm site should not take down the whole dashboard; failures appear in **Source Health**.

The workflow must live at `.github/workflows/update.yml`. A YAML file stored in the repository root will not run as a GitHub Action.

## GitHub Pages

Publish from the `main` branch and `/ (root)`. The dashboard is `index.html`; `jobs.json` and `meta.json` are the live data files.
