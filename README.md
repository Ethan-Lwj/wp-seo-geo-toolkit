# WP SEO / GEO Toolkit

Automation toolkit for running **multi-site WordPress content operations** —
the boring-but-critical plumbing that keeps a portfolio of niche sites
crawlable by Google and quotable by AI answer engines (ChatGPT, Perplexity,
Copilot).

Two self-contained tools, zero framework dependencies, cron-friendly:

| Tool | What it does |
|------|--------------|
| `wp_sitemap_updater.py` | Pulls every published post from the WP REST API, rebuilds a plain-text sitemap in the media library, and submits URLs to Google via Rank Math Instant Indexing. |
| `geo_visibility_monitor.py` | Monthly GEO (Generative Engine Optimization) monitor: runs a fixed panel of buying-intent prompts through a pluggable search backend, checks whether your domains and brand terms show up, health-checks `robots.txt` / `llms.txt` / `wp-sitemap.xml` / IndexNow, diffs against last month in SQLite, and writes a Markdown report. |

## Why

Classic SEO covers the Google side. **GEO** is the newer problem: when someone
asks an AI engine *"best X for Y"*, does your content get cited? That depends
on primitives (`llms.txt`, IndexNow, clean robots rules) plus actual
visibility in search-backed answers — none of which Search Console shows you.
This toolkit monitors both, per site, every month, with month-over-month
trends instead of vanity metrics.

## Quick start

```bash
pip install -r requirements.txt
```

### 1. Sitemap updater

Create a WordPress Application Password first (WP Admin → Users → Profile →
Application Passwords), then:

```bash
export WP_SITE_URL="https://example.com"
export WP_USERNAME="your-wp-user"
export WP_APP_PASSWORD="xxxx xxxx xxxx xxxx"

python3 wp_sitemap_updater.py
```

What happens: published posts are paged through the REST API → a
`sitemap-urls.txt` is rebuilt in the media library (old copy deleted so the
public URL stays stable) → the URL list is submitted to the Rank Math
Instant Indexing endpoint. Designed to run on a cron (e.g. every other day).

### 2. GEO visibility monitor

```bash
cp config.example.json config.json   # edit: your domains, brand terms, prompts
export BRAVE_API_KEY="..."           # or implement your own SearchBackend

python3 geo_visibility_monitor.py config.json
```

Output: `reports/geo_report_YYYY-MM.md` + a SQLite snapshot store
(`geo_monitor_state.db`) that powers the month-over-month diff section.

Optional LLM polish for the insights section — works with any
OpenAI-compatible endpoint, silently skipped when unset:

```bash
export OPENAI_API_KEY="..."
export OPENAI_MODEL="gpt-4o-mini"        # optional
export OPENAI_BASE_URL="https://api.openai.com/v1"  # optional
```

## Architecture notes

- **Pluggable search backend.** `SearchBackend` is a 20-line interface;
  `BraveSearchBackend` ships as the reference implementation. Bing, SerpAPI,
  Google CSE or a self-hosted SearxNG all drop in the same way.
- **Health checks are plain HTTP probes** — no SDK, no browser. If
  `llms.txt` or the IndexNow key file isn't reachable, the report says so.
- **SQLite over time series DBs.** One file, zero setup, enough to answer
  "did we gain or lose visibility last month".
- **Secrets only via environment variables.** No credentials in code, ever.

## GEO primitives this monitors

| Primitive | Role for generative engines |
|-----------|------------------------------|
| `llms.txt` | Lets LLM crawlers map your content structure (emerging convention) |
| `robots.txt` | Must not block AI user agents you want citations from |
| `wp-sitemap.xml` | Canonical URL inventory for crawlers |
| IndexNow key file | Near-instant URL discovery for Bing & partners |

## License

[MIT](LICENSE)
