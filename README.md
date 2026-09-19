# Executive Opportunity Radar

A public-source market monitor for CEO, president and executive director searches across tracked executive-search firms.

## What v3 changes

- Market-intelligence homepage plus a date-first opportunity feed.
- Mobile-first responsive layout.
- Entire opportunity card is clickable; the updater prefers a direct job posting URL whenever the source exposes one.
- Missing compensation, location, sector or posted date never excludes a legitimate role.
- Posted dates are used only when the source provides them; otherwise the site shows `First seen`.
- `history.json` retains closed roles instead of deleting them, so trend analysis improves over time.
- Jobs are archived only after two healthy checks fail to find them.
- Failed or suspiciously partial source checks preserve previously known roles.
- `changes.json` records new, updated, reopened and closed search events.
- Favorites are stored only in the visitor's browser via local storage.

## Files

- `index.html` — public dashboard
- `jobs.json` — current active roles
- `history.json` — open + archived role history
- `changes.json` — recent data-change events
- `meta.json` — source health from the most recent run
- `sources.json` — tracked source configuration
- `aggregate.py` — scraper, enrichment, history and health logic
- `requirements.txt` — Python dependencies
- `.github/workflows/update-radar.yml` — daily GitHub Actions updater

## Automation

The GitHub workflow runs daily at 10:17 UTC and can also be run manually from the Actions tab. It:

1. visits each automated public source;
2. uses normal HTTP parsing first and browser rendering for configured dynamic sites;
3. captures qualifying CEO / president / executive director roles even when optional fields are missing;
4. enriches fields when possible;
5. compares the result with prior history;
6. preserves roles when a source fails or appears suspiciously incomplete;
7. updates the JSON data files; and
8. commits only when the data changes.

## Link behavior

The card itself opens the opportunity. `link_quality: direct` means the scraper found a role-specific URL. `link_quality: source_page` means the public source did not expose a reliable role-specific URL in the captured markup, so the card opens the source listing instead. The site labels those cards `Source listing` rather than pretending the link is direct.

## Coverage caveat

This project monitors public listings from configured executive-search sources. It does not imply exhaustive coverage of all executive searches, including confidential searches and firms that do not publish active searches.
