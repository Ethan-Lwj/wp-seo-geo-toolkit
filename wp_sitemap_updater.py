#!/usr/bin/env python3
"""WP Sitemap Updater & Instant Indexing

Pulls all published post URLs from a WordPress site via the REST API,
rebuilds a plain-text sitemap file in the media library, and submits
the URL list to Google through the Rank Math Instant Indexing API.

Environment variables (required):
  WP_SITE_URL       e.g. https://example.com
  WP_USERNAME       WordPress login name
  WP_APP_PASSWORD   WordPress Application Password (WP Admin > Users > Profile)

Usage:
  python3 wp_sitemap_updater.py

Optional environment variables:
  RANKMATH_SUBMIT   "1" to submit URLs via Rank Math Instant Indexing (default: on)
"""

import asyncio
import json
import os
import sys

import aiohttp

TIMEOUT = aiohttp.ClientTimeout(total=30)


def load_config() -> dict:
    """Build the site config from environment variables."""
    site_url = os.environ.get("WP_SITE_URL", "").rstrip("/")
    username = os.environ.get("WP_USERNAME", "")
    app_password = os.environ.get("WP_APP_PASSWORD", "")

    missing = [k for k, v in {
        "WP_SITE_URL": site_url,
        "WP_USERNAME": username,
        "WP_APP_PASSWORD": app_password,
    }.items() if not v]

    if missing:
        print(f"[error] Missing environment variables: {', '.join(missing)}")
        sys.exit(1)

    return {
        "site_url": site_url,
        "rest_api": f"{site_url}/wp-json/wp/v2",
        "rankmath_endpoint": f"{site_url}/wp-json/rankmath/v1/in/submitUrls",
        "username": username,
        "app_password": app_password,
    }


async def collect_published_urls(session: aiohttp.ClientSession, config: dict) -> list[str]:
    """Fetch every published post URL, handling pagination."""
    all_urls: list[str] = []
    page = 1
    total_pages = 1

    auth = aiohttp.BasicAuth(config["username"], config["app_password"])
    rest_api = config["rest_api"]

    while page <= total_pages:
        posts_url = (
            f"{rest_api}/posts?status=publish&per_page=100"
            f"&page={page}&_fields=id,link"
        )
        async with session.get(posts_url, auth=auth) as resp:
            if resp.status == 401:
                print("[error] Authentication failed (HTTP 401). "
                      "Check WP_USERNAME / WP_APP_PASSWORD.")
                sys.exit(1)
            resp.raise_for_status()

            if page == 1:
                total_pages = int(resp.headers.get("X-WP-TotalPages", "1"))
                total_posts = int(resp.headers.get("X-WP-Total", "0"))
                print(f"[1/4] {total_posts} posts across {total_pages} page(s)")

            posts = await resp.json()
            for post in posts:
                link = post.get("link")
                if link:
                    all_urls.append(link)
            page += 1

    return all_urls


async def upload_sitemap(session: aiohttp.ClientSession, config: dict, sitemap_content: str) -> str:
    """Replace the sitemap-urls.txt media item with a fresh copy.

    WordPress does not support in-place media updates, so the old file
    is deleted before uploading the new one.
    """
    auth = aiohttp.BasicAuth(config["username"], config["app_password"])
    rest_api = config["rest_api"]

    # Find existing media item
    async with session.get(f"{rest_api}/media?search=sitemap-urls.txt", auth=auth) as resp:
        resp.raise_for_status()
        existing_items = await resp.json()

    # Delete old copies so the public URL stays stable
    for item in existing_items:
        mid = item.get("id")
        if mid:
            async with session.delete(f"{rest_api}/media/{mid}?force=true", auth=auth) as d_resp:
                d_resp.raise_for_status()

    # Upload fresh file
    upload_headers = {
        "Content-Disposition": 'attachment; filename="sitemap-urls.txt"',
        "Content-Type": "text/plain",
    }
    async with session.post(
        f"{rest_api}/media",
        auth=auth,
        headers=upload_headers,
        data=sitemap_content.encode("utf-8"),
    ) as resp:
        if resp.status not in (200, 201):
            body = await resp.text()
            print(f"[error] Sitemap upload failed (HTTP {resp.status})\n{body[:300]}")
            sys.exit(1)
        media_data = await resp.json()

    return media_data.get("source_url", "")


async def submit_to_google(session: aiohttp.ClientSession, config: dict, urls: list[str]) -> tuple[bool, str]:
    """Push the URL list to Google via Rank Math Instant Indexing."""
    auth = aiohttp.BasicAuth(config["username"], config["app_password"])
    body = {"urls": "\n".join(urls)}

    try:
        async with session.post(config["rankmath_endpoint"], auth=auth, json=body) as resp:
            text = await resp.text()
            if resp.status == 200:
                return True, text[:500]
            return False, f"HTTP {resp.status}: {text[:300]}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


async def main():
    config = load_config()
    print(f"[config] site={config['site_url']}")

    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        # Step 1: collect published URLs
        print("[1/4] Collecting published post URLs ...")
        all_urls = await collect_published_urls(session, config)
        print(f"[1/4] Collected {len(all_urls)} URL(s)")

        if not all_urls:
            print("[warn] No published posts found. Nothing to do.")
            return

        # Step 2: build the sitemap body
        sitemap_content = "\n".join(all_urls) + "\n"
        print(f"[2/4] Sitemap body built ({len(all_urls)} lines)")

        # Step 3: upload to the media library
        print("[3/4] Refreshing sitemap-urls.txt in the media library ...")
        media_url = await upload_sitemap(session, config, sitemap_content)
        print(f"[3/4] Uploaded -> {media_url}")

        # Step 4: submit to Google
        if os.environ.get("RANKMATH_SUBMIT", "1") != "1":
            print("[4/4] Skipped (RANKMATH_SUBMIT != 1)")
            submit_ok, detail = False, "skipped"
        else:
            print("[4/4] Submitting URLs to Google (Rank Math Instant Indexing) ...")
            submit_ok, detail = await submit_to_google(session, config, all_urls)
            print(f"[4/4] {'OK' if submit_ok else 'FAILED'}: {detail}")

        result = {
            "site": config["site_url"],
            "url_count": len(all_urls),
            "sitemap_url": media_url,
            "google_submit": submit_ok,
        }
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
