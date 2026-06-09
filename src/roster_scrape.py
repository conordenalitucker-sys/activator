"""Steptoe roster scraper (Phase 3b) — headless Chromium (gets past the site's anti-bot).

Scrapes the firm's PRACTICE pages (each lists its attorneys) to populate:
  practice_areas, attorneys, attorney_practices.
This gives the practice->attorney mapping cross-sell needs from ~124 pages (vs 547 bios).
Weekly re-scrape diffs: attorneys/practices not seen this run -> marked departed/retired.

Run: python3 src/roster_scrape.py [practice_limit]
Python 3.9 compatible. Requires: playwright + chromium (python -m playwright install chromium).
"""
from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402

from playwright.sync_api import sync_playwright  # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
NOW = dt.datetime.utcnow().isoformat()
SITEMAP = "https://www.steptoe.com/sitemap.xml"


def clean_name(t):
    return re.sub(r"\s+", " ", (t or "").strip())


def practice_group(url):
    m = re.search(r"/practices/([^/]+)/", url)
    return m.group(1).replace("-", " ").title() if m else None


def scrape(limit=0):
    out = {}  # practice_url -> {"name":..., "group":..., "attorneys":[(name,bio_url)]}
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_context(user_agent=UA, viewport={"width": 1280, "height": 900}).new_page()
        pg.goto(SITEMAP, wait_until="domcontentloaded", timeout=30000)
        pg.wait_for_timeout(1500)
        urls = sorted(set(re.findall(
            r"https://www\.steptoe\.com/en/services/practices/[^<\s]+\.html", pg.content())))
        if limit:
            urls = urls[:limit]
        print(f"{len(urls)} practice pages to scrape")
        for i, url in enumerate(urls, 1):
            try:
                pg.goto(url, wait_until="domcontentloaded", timeout=30000)
                pg.wait_for_timeout(1800)
                name = (pg.title() or "").split("|")[0].strip()
                links = pg.eval_on_selector_all(
                    "a[href*='/lawyers/']",
                    "els => els.map(e => [e.textContent.trim(), e.getAttribute('href')])")
                seen, atts = set(), []
                for text, href in links:
                    if not href or "vcard" in href or not href.endswith(".html"):
                        continue
                    nm = clean_name(text)
                    h = href if href.startswith("http") else "https://www.steptoe.com" + href
                    if nm and "/lawyers/" in h and h not in seen:
                        seen.add(h)
                        atts.append((nm, h))
                out[url] = {"name": name, "group": practice_group(url), "attorneys": atts}
                print(f"  [{i}/{len(urls)}] {name}: {len(atts)} attorneys")
            except Exception as e:
                sys.stderr.write(f"  fail {url}: {str(e)[:80]}\n")
        b.close()
    return out


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 0
    data = scrape(limit)

    practices = {p["name"].lower(): p for p in db.get("practice_areas?select=id,name")}
    attorneys = {a["bio_url"]: a for a in db.get("attorneys?select=id,bio_url") if a.get("bio_url")}
    links = {(l["attorney_id"], l["practice_area_id"])
             for l in db.get("attorney_practices?select=attorney_id,practice_area_id")}

    seen_practices, seen_attorneys = set(), set()
    for url, pr in data.items():
        if not pr["name"]:
            continue
        key = pr["name"].lower()
        seen_practices.add(key)
        if key in practices:
            pid = practices[key]["id"]
            db.patch(f"practice_areas?id=eq.{pid}", {"last_seen_scrape": NOW, "status": "active"})
        else:
            row = db.post("practice_areas", {"name": pr["name"], "parent_group": pr["group"],
                                             "url": url, "last_seen_scrape": NOW})
            pid = row[0]["id"]
            practices[key] = {"id": pid}
        for nm, bio in pr["attorneys"]:
            seen_attorneys.add(bio)
            if bio in attorneys:
                aid = attorneys[bio]["id"]
                db.patch(f"attorneys?id=eq.{aid}", {"last_seen_scrape": NOW, "status": "active",
                                                    "departed_detected_at": None})
            else:
                row = db.post("attorneys", {"name": nm, "bio_url": bio, "last_seen_scrape": NOW})
                aid = row[0]["id"]
                attorneys[bio] = {"id": aid}
            if (aid, pid) not in links:
                db.post("attorney_practices", {"attorney_id": aid, "practice_area_id": pid},
                        prefer="return=minimal")
                links.add((aid, pid))

    # Diff: mark active-but-unseen as departed/retired (only on a full scrape).
    departed = 0
    if not limit:
        for bio, a in attorneys.items():
            if bio not in seen_attorneys:
                db.patch(f"attorneys?id=eq.{a['id']}&status=eq.active",
                         {"status": "departed", "departed_detected_at": NOW})
                departed += 1

    print(f"\nDone. {len(seen_practices)} practices, {len(seen_attorneys)} attorneys this run; "
          f"{departed} marked departed.")


if __name__ == "__main__":
    main()
