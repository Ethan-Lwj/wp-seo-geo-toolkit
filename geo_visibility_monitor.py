#!/usr/bin/env python3
"""GEO (Generative Engine Optimization) Visibility Monitor

Monthly monitor that measures how visible your sites and brand terms are
in AI-driven / search-driven answers to buying-intent queries, and checks
the technical primitives that generative engines rely on.

Pipeline:
  1. Run a fixed panel of test prompts through a pluggable search backend
     and check whether target domains / brand terms appear in the results.
  2. Health-check every site: robots.txt, llms.txt, wp-sitemap.xml and the
     IndexNow key file (GEO readiness).
  3. Persist monthly snapshots into SQLite and diff against last month
     (gained / lost / stable visibility).
  4. Generate insights (statistics-based, optionally enhanced by an
     LLM if OPENAI_API_KEY is set) and write a Markdown report.

Configuration: config.json (see config.example.json).
Usage: python3 geo_visibility_monitor.py [config.json]
"""

import asyncio
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import aiohttp

DB_PATH = os.environ.get("GEO_MONITOR_DB", "./geo_monitor_state.db")
DEFAULT_CONFIG_PATH = "config.example.json"


# ---------------------------------------------------------------- config

def load_config(path: str) -> dict:
    if not os.path.exists(path):
        print(f"[error] Config file not found: {path}")
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- search backend

class SearchBackend:
    """Pluggable search backend interface.

    Any provider that can turn a query into a list of results works:
    Bing Web Search, Brave Search API, SerpAPI, Google CSE, a self-hosted
    SearxNG instance, etc. Implement `search()` returning a list of
    dicts: {"url": str, "title": str, "snippet": str}.
    """

    async def search(self, session: aiohttp.ClientSession, query: str) -> list[dict]:
        raise NotImplementedError


class BraveSearchBackend(SearchBackend):
    """Reference implementation using the Brave Search API.

    Set BRAVE_API_KEY (https://brave.com/search/api/) to activate.
    """

    BASE_URL = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def search(self, session: aiohttp.ClientSession, query: str) -> list[dict]:
        headers = {"Accept": "application/json", "X-Subscription-Token": self.api_key}
        params = {"q": query, "count": 20}
        async with session.get(self.BASE_URL, headers=headers, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()
        results = []
        for item in (data.get("web") or {}).get("results", []):
            results.append({
                "url": item.get("url", ""),
                "title": item.get("title", ""),
                "snippet": item.get("description", ""),
            })
        return results


def build_backend(config: dict) -> SearchBackend:
    provider = config.get("search_backend", "brave").lower()
    api_key = os.environ.get("BRAVE_API_KEY", "")
    if provider == "brave":
        if not api_key:
            print("[warn] BRAVE_API_KEY not set — visibility tests will be skipped.")
            print("       Implement SearchBackend with any provider and plug it in.")
        return BraveSearchBackend(api_key)
    raise ValueError(f"Unknown search backend: {provider}")


# ---------------------------------------------------------------- visibility tests

async def test_one_prompt(prompt_info: dict, all_domains: list, all_brands: list,
                          backend: SearchBackend, session: aiohttp.ClientSession,
                          sem: asyncio.Semaphore) -> dict:
    query = prompt_info["prompt"]
    async with sem:
        print(f"  [search] {query}")
        try:
            results = await backend.search(session, query)
        except Exception as e:  # noqa: BLE001
            return {"prompt": query, "target_site": prompt_info.get("target_site"),
                    "total_results": 0, "checks": [], "error": str(e)}

    checks = []
    # site domains
    for domain in all_domains:
        match = None
        for i, r in enumerate(results):
            url = (r.get("url") or "").lower()
            snippet = (r.get("snippet") or "").lower()
            if domain.lower() in url or domain.lower() in snippet:
                match = {"entity_type": "site", "entity_value": domain, "found": True,
                         "position": i + 1, "title": r.get("title", ""), "url": r.get("url", "")}
                break
        checks.append(match or {"entity_type": "site", "entity_value": domain, "found": False})

    # brand terms
    for brand in all_brands:
        term = brand["term"].lower()
        match = None
        for i, r in enumerate(results):
            haystacks = [(r.get("url") or "").lower(), (r.get("title") or "").lower(),
                         (r.get("snippet") or "").lower()]
            if any(term in h for h in haystacks):
                match = {"entity_type": "brand", "entity_value": brand["term"], "found": True,
                         "position": i + 1, "title": r.get("title", ""), "url": r.get("url", "")}
                break
        checks.append(match or {"entity_type": "brand", "entity_value": brand["term"], "found": False})

    return {"prompt": query, "target_site": prompt_info.get("target_site"),
            "total_results": len(results), "checks": checks}


async def run_visibility_tests(config: dict, backend: SearchBackend,
                               session: aiohttp.ClientSession) -> list:
    prompts = config["test_prompts"]
    domains = [s["domain"] for s in config["sites"]]
    brands = config.get("brand_terms", [])

    print(f"\n=== Phase 1: visibility tests ({len(prompts)} prompts) ===")
    sem = asyncio.Semaphore(config.get("concurrency", 4))
    tasks = [test_one_prompt(p, domains, brands, backend, session, sem) for p in prompts]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    valid = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            print(f"  [error] {prompts[i]['prompt']}: {r}")
            valid.append({"prompt": prompts[i]["prompt"], "target_site": prompts[i].get("target_site"),
                          "total_results": 0, "checks": [], "error": str(r)})
        else:
            valid.append(r)
    return valid


# ---------------------------------------------------------------- health checks

async def check_domain_health(site: dict, session: aiohttp.ClientSession, sem: asyncio.Semaphore) -> dict:
    """Real HTTP health probe: robots.txt / llms.txt / sitemap / IndexNow key."""
    domain = site["domain"]
    indexnow_key = site.get("indexnow_key")
    base = f"https://{domain}"

    async def fetch(path: str) -> dict:
        async with sem:
            try:
                async with session.get(f"{base}/{path}", allow_redirects=True) as resp:
                    content = ""
                    if resp.status == 200:
                        raw = await resp.text(errors="ignore")
                        content = raw[:2000]
                    return {"status": "ok" if resp.status == 200 else "not_found",
                            "status_code": resp.status, "content": content}
            except Exception as e:  # noqa: BLE001
                return {"status": "error", "status_code": 0, "content": "", "error": str(e)}

    robots, llms_txt, sitemap = await asyncio.gather(
        fetch("robots.txt"), fetch("llms.txt"), fetch("wp-sitemap.xml"),
    )

    # IndexNow: the key file must exist at the site root
    if not indexnow_key:
        indexnow = "pending_deploy"
    else:
        key_check = await fetch(f"{indexnow_key}.txt")
        indexnow = "configured" if key_check["status"] == "ok" else "not_configured"

    llms_lines = [ln.strip() for ln in llms_txt["content"].split("\n") if ln.strip()][:10] \
        if llms_txt["status"] == "ok" else []

    return {
        "domain": domain,
        "robots_txt": robots["status"],
        "llms_txt": llms_txt["status"],
        "llms_txt_lines": llms_lines,
        "sitemap_xml": sitemap["status"],
        "indexnow": indexnow,
    }


async def run_health_checks(config: dict, session: aiohttp.ClientSession) -> list:
    print(f"\n=== Phase 2: health checks ({len(config['sites'])} sites) ===")
    sem = asyncio.Semaphore(config.get("concurrency", 4))
    tasks = [check_domain_health(s, session, sem) for s in config["sites"]]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    valid = []
    for i, r in enumerate(results):
        domain = config["sites"][i]["domain"]
        if isinstance(r, Exception):
            print(f"  [error] {domain}: {r}")
            valid.append({"domain": domain, "robots_txt": "error", "llms_txt": "error",
                          "llms_txt_lines": [], "sitemap_xml": "error", "indexnow": "unknown"})
        else:
            valid.append(r)
            print(f"  [health] {domain}: robots={r['robots_txt']}, llms={r['llms_txt']}, "
                  f"sitemap={r['sitemap_xml']}, indexnow={r['indexnow']}")
    return valid


# ---------------------------------------------------------------- snapshot store

def init_db(conn: sqlite3.Connection):
    conn.execute("""CREATE TABLE IF NOT EXISTS snapshots (
        month TEXT NOT NULL, prompt TEXT NOT NULL,
        entity_type TEXT NOT NULL, entity_value TEXT NOT NULL,
        found INTEGER NOT NULL DEFAULT 0,
        result_position INTEGER, result_title TEXT, result_url TEXT,
        PRIMARY KEY (month, prompt, entity_type, entity_value)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS runs (
        month TEXT PRIMARY KEY, run_at TEXT NOT NULL,
        report_path TEXT, status TEXT
    )""")


def save_snapshot(conn, month, prompt, entity_type, entity_value, found, position=None, title=None, url=None):
    conn.execute(
        "INSERT OR REPLACE INTO snapshots (month, prompt, entity_type, entity_value, "
        "found, result_position, result_title, result_url) VALUES (?,?,?,?,?,?,?,?)",
        (month, prompt, entity_type, entity_value, int(found), position, title, url),
    )


def get_prev_snapshots(conn, prev_month: str) -> dict:
    rows = conn.execute(
        "SELECT prompt, entity_type, entity_value, found, result_position "
        "FROM snapshots WHERE month=?", (prev_month,),
    ).fetchall()
    result = {}
    for prompt, et, ev, found, pos in rows:
        result.setdefault(prompt, {})[f"{et}:{ev}"] = {"found": bool(found), "position": pos}
    return result


def compare_with_previous(current: list, previous: dict, prompts: list) -> dict:
    curr_map = {(s["prompt"], f"{s['entity_type']}:{s['entity_value']}"): s["found"] for s in current}
    gained, lost, stable_found, stable_lost = [], [], [], []

    for prompt in [p["prompt"] for p in prompts]:
        for (p, entity), found in curr_map.items():
            if p != prompt:
                continue
            prev = previous.get(p, {}).get(entity)
            if prev is None:
                continue
            if found and not prev["found"]:
                gained.append({"prompt": p, "entity": entity})
            elif not found and prev["found"]:
                lost.append({"prompt": p, "entity": entity})
            elif found and prev["found"]:
                stable_found.append({"prompt": p, "entity": entity})
            else:
                stable_lost.append({"prompt": p, "entity": entity})

    return {"gained": gained, "lost": lost, "stable_found": stable_found, "stable_lost": stable_lost}


# ---------------------------------------------------------------- insights

@dataclass
class Insights:
    overview: str = ""
    key_findings: list = field(default_factory=list)
    recommendations: list = field(default_factory=list)
    risk_warnings: list = field(default_factory=list)


def build_stats_insights(config: dict, visibility: list, health: list,
                          comparison: dict, has_prev: bool) -> Insights:
    """Deterministic, statistics-based insights. No LLM required."""
    total = len(config["test_prompts"])
    site_hits = {}
    for s in config["sites"]:
        d = s["domain"]
        site_hits[d] = sum(1 for v in visibility for c in v["checks"]
                           if c["entity_type"] == "site" and c["entity_value"] == d and c.get("found"))

    best = max(site_hits.items(), key=lambda kv: kv[1]) if site_hits else None
    overview = (f"Visibility across {len(config['sites'])} site(s): "
                + ", ".join(f"{d} {c}/{total}" for d, c in site_hits.items()))

    findings = [
        f"Top performer: {best[0]} visible in {best[1]}/{total} prompts" if best and best[1] > 0
        else "No site appeared in search results this month",
    ]
    if has_prev:
        findings.append(f"Month over month: +{len(comparison['gained'])} gained, "
                        f"-{len(comparison['lost'])} lost, {len(comparison['stable_found'])} stable")

    unhealthy = [h["domain"] for h in health
                 if h["llms_txt"] != "ok" or h["robots_txt"] != "ok" or h["sitemap_xml"] != "ok"]
    if unhealthy:
        findings.append(f"GEO primitives missing on: {', '.join(unhealthy)}")

    recommendations = []
    for h in health:
        if h["llms_txt"] != "ok":
            recommendations.append(f"{h['domain']}: deploy llms.txt so LLM crawlers can map your content")
        if h["indexnow"] != "configured":
            recommendations.append(f"{h['domain']}: configure IndexNow for near-instant URL discovery")
        if h["sitemap_xml"] != "ok":
            recommendations.append(f"{h['domain']}: fix wp-sitemap.xml availability")
    if not recommendations:
        recommendations.append("All GEO primitives healthy — invest in content depth and FAQ schema coverage")

    warnings = []
    if has_prev and len(comparison["lost"]) > len(comparison["gained"]) * 2:
        warnings.append("Visibility is shrinking fast — review recent content and indexing status")

    return Insights(overview=overview, key_findings=findings,
                    recommendations=recommendations, risk_warnings=warnings)


async def llm_enhance(insights: Insights, context: str) -> Insights:
    """Optional: refine insights with an OpenAI-compatible chat API.

    Used only when OPENAI_API_KEY is set; otherwise the statistics-based
    insights are returned untouched.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "")
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    if not api_key:
        return insights

    payload = {
        "model": model,
        "messages": [{"role": "user", "content":
                      "You are reviewing a GEO (generative engine optimization) monthly report. "
                      "Refine the following draft insights. Keep it factual; do not invent data.\n\n"
                      f"Draft:\n{json.dumps(insights.__dict__, ensure_ascii=False, indent=2)}\n\n"
                      f"Data:\n{context}\n\n"
                      "Return JSON: {\"overview\": str, \"key_findings\": [str], "
                      "\"recommendations\": [str], \"risk_warnings\": [str]}"}],
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }
    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(f"{base}/chat/completions",
                             headers={"Authorization": f"Bearer {api_key}"},
                             json=payload) as resp:
                resp.raise_for_status()
                data = await resp.json()
        parsed = json.loads(data["choices"][0]["message"]["content"])
        return Insights(
            overview=parsed.get("overview", insights.overview),
            key_findings=parsed.get("key_findings", insights.key_findings),
            recommendations=parsed.get("recommendations", insights.recommendations),
            risk_warnings=parsed.get("risk_warnings", insights.risk_warnings),
        )
    except Exception as e:  # noqa: BLE001
        print(f"  [llm] enhancement failed ({e}), keeping statistical insights")
        return insights


# ---------------------------------------------------------------- report

def generate_report(target_month: str, config: dict, visibility: list, health: list,
                    comparison: dict, has_prev: bool, insights: Insights) -> str:
    total = len(config["test_prompts"])
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        f"# GEO Visibility Report — {target_month}",
        f"\n**Generated**: {now} (UTC+8)",
        f"\n**Sites**: {len(config['sites'])} | **Prompts**: {total} "
        f"| **Brand terms**: {len(config.get('brand_terms', []))}",
        "\n---\n",
        "## 1. Overview\n", insights.overview, "\n",
        "## 2. Site Visibility\n",
        "| Site | Description | Visibility | Status |",
        "|------|-------------|------------|--------|",
    ]

    for s in config["sites"]:
        d = s["domain"]
        count = sum(1 for v in visibility for c in v["checks"]
                    if c["entity_type"] == "site" and c["entity_value"] == d and c.get("found"))
        pct = round(count / total * 100) if total else 0
        status = "good" if pct >= 50 else ("fair" if pct >= 20 else "needs work")
        lines.append(f"| {d} | {s.get('desc', '')} | {count}/{total} ({pct}%) | {status} |")

    if config.get("brand_terms"):
        lines.append("\n## 3. Brand Term Visibility\n")
        lines.append("| Term | Description | Visibility |")
        lines.append("|------|-------------|------------|")
        for b in config["brand_terms"]:
            t = b["term"]
            count = sum(1 for v in visibility for c in v["checks"]
                        if c["entity_type"] == "brand" and c["entity_value"] == t and c.get("found"))
            lines.append(f"| {t} | {b.get('desc', '')} | {count}/{total} |")

    lines.append("\n## 4. Prompt Details\n")
    for v in visibility:
        target = f"-> {v['target_site']}" if v.get("target_site") else "-> brand"
        err = " (search error)" if v.get("error") else ""
        lines.append(f"### {v['prompt']} {target}{err}\n")
        found_items = [c for c in v["checks"] if c.get("found")]
        missing = [c["entity_value"] for c in v["checks"] if not c.get("found")]
        for c in found_items:
            icon = "[site]" if c["entity_type"] == "site" else "[brand]"
            lines.append(f"- {icon} **{c['entity_value']}** — rank #{c.get('position', '?')} "
                         f"| {(c.get('title') or '')[:60]}")
        if missing:
            lines.append(f"- not found: {', '.join(missing)}")
        if not v["checks"]:
            lines.append("- (no checks executed)")
        lines.append("")

    lines.append("\n## 5. Health Checks\n")
    lines.append("| Domain | robots.txt | llms.txt | sitemap.xml | IndexNow |")
    lines.append("|--------|-----------|----------|-------------|----------|")
    icons = {"ok": "OK", "not_found": "MISSING", "error": "ERROR", "unknown": "?",
             "configured": "OK", "not_configured": "MISSING", "pending_deploy": "PENDING"}
    for h in health:
        lines.append(f"| {h['domain']} | {icons.get(h['robots_txt'], h['robots_txt'])} "
                     f"| {icons.get(h['llms_txt'], h['llms_txt'])} "
                     f"| {icons.get(h['sitemap_xml'], h['sitemap_xml'])} "
                     f"| {icons.get(h['indexnow'], h['indexnow'])} |")

    for h in health:
        if h["llms_txt_lines"]:
            lines.append(f"\n**{h['domain']} llms.txt (first lines)**:\n")
            for ln in h["llms_txt_lines"][:5]:
                lines.append(f"  > {ln[:120]}")

    if has_prev:
        lines.append("\n## 6. Month over Month\n")
        lines.append(f"- **Gained**: {len(comparison['gained'])}")
        for item in comparison["gained"][:10]:
            lines.append(f"  - + {item['entity']} @ {item['prompt']}")
        lines.append(f"- **Lost**: {len(comparison['lost'])}")
        for item in comparison["lost"][:10]:
            lines.append(f"  - - {item['entity']} @ {item['prompt']}")
        lines.append(f"- **Stable visible**: {len(comparison['stable_found'])} "
                     f"| **Stable missing**: {len(comparison['stable_lost'])}")
    else:
        lines.append("\n## 6. Month over Month\n")
        lines.append("First run — no previous snapshot to compare. Trends appear next month.")

    if insights.recommendations:
        lines.append("\n## 7. Recommendations\n")
        for r in insights.recommendations:
            lines.append(f"- {r}")
    if insights.risk_warnings:
        lines.append("\n## 8. Warnings\n")
        for w in insights.risk_warnings:
            lines.append(f"- {w}")

    lines.append("\n---\n")
    lines.append("*Method: fixed panel of buying-intent prompts is issued to a search backend; "
                 "results are scanned for target domains and brand terms. Health checks are plain "
                 "HTTP probes for robots.txt / llms.txt / wp-sitemap.xml and the IndexNow key file.*")
    return "\n".join(lines)


# ---------------------------------------------------------------- main

async def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CONFIG_PATH
    config = load_config(config_path)
    backend = build_backend(config)
    now = datetime.now(timezone(timedelta(hours=8)))
    target_month = now.strftime("%Y-%m")

    output_dir = config.get("output_dir", "./reports")
    os.makedirs(output_dir, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    print("=" * 50)
    print(f"GEO visibility monitor — {target_month}")
    print("=" * 50)

    timeout = aiohttp.ClientTimeout(total=45)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        visibility_task = asyncio.create_task(run_visibility_tests(config, backend, session))
        health_task = asyncio.create_task(run_health_checks(config, session))
        visibility_results, health_results = await asyncio.gather(visibility_task, health_task)

    # persist snapshots
    print("\n=== Saving snapshots ===")
    all_snapshots = []
    for v in visibility_results:
        for c in v["checks"]:
            save_snapshot(conn, target_month, v["prompt"], c["entity_type"],
                         c["entity_value"], c.get("found", False),
                         position=c.get("position"), title=c.get("title"), url=c.get("url"))
            all_snapshots.append({"prompt": v["prompt"], "entity_type": c["entity_type"],
                                  "entity_value": c["entity_value"], "found": c.get("found", False)})
    conn.commit()

    # month-over-month comparison
    y, m = int(target_month[:4]), int(target_month[5:7])
    prev_month = f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"
    prev_snapshots = get_prev_snapshots(conn, prev_month)
    has_prev = len(prev_snapshots) > 0
    comparison = compare_with_previous(all_snapshots, prev_snapshots, config["test_prompts"]) \
        if has_prev else {"gained": [], "lost": [], "stable_found": [], "stable_lost": []}

    # insights (statistics first, optional LLM polish)
    insights = build_stats_insights(config, visibility_results, health_results, comparison, has_prev)
    insights = await llm_enhance(insights, json.dumps({
        "visibility": [(v["prompt"], v["total_results"]) for v in visibility_results],
        "health": [(h["domain"], h["robots_txt"], h["llms_txt"], h["sitemap_xml"], h["indexnow"])
                   for h in health_results],
    }, ensure_ascii=False))

    # report
    print("\n=== Generating report ===")
    report = generate_report(target_month, config, visibility_results, health_results,
                             comparison, has_prev, insights)
    report_path = os.path.join(output_dir, f"geo_report_{target_month}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"  report: {report_path}")

    conn.execute("INSERT OR REPLACE INTO runs (month, run_at, report_path, status) VALUES (?,?,?,?)",
                 (target_month, now.isoformat(timespec="seconds"), report_path, "success"))
    conn.commit()
    conn.close()

    print(f"\nDone. {insights.overview}")


if __name__ == "__main__":
    asyncio.run(main())
