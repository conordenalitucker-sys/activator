"""Monitoring pipeline for Project Activator.

For each company, builds search queries from: the company name, its enabled entities
(corporate family / peers / industry), the company's watch_terms + industries, and the
interests of contacts at that company. Pulls candidate hits from Google News (RSS) and,
when a token is configured, CourtListener. Claude (Haiku) relevance-filters & classifies
each company's candidates into signals, which are deduped and written to Supabase.

Run: python3 src/monitor.py [company_limit]
Env: ANTHROPIC_API_KEY (required), COURTLISTENER_TOKEN (optional).
Python 3.9 compatible.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402  (loads .env)
import anthropic  # noqa: E402

MODEL = "claude-haiku-4-5-20251001"
NEWS_PER_QUERY = 6
MAX_CANDIDATES = 30
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Activator/1.0"


def fetch(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def google_news(phrase, extra=""):
    raw = f'"{phrase}" {extra}'.strip()
    q = urllib.parse.quote(raw)
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    out = []
    try:
        root = ET.fromstring(fetch(url))
    except Exception as e:
        sys.stderr.write(f"  news fetch failed [{phrase}]: {str(e)[:80]}\n")
        return out
    for item in list(root.iterfind(".//item"))[:NEWS_PER_QUERY]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = item.findtext("pubDate")
        src = item.find("{*}source")
        when = None
        if pub:
            try:
                when = parsedate_to_datetime(pub).date().isoformat()
            except Exception:
                when = None
        out.append({"kind": "news", "title": title, "url": link, "date": when,
                    "source": (src.text if src is not None else "Google News"), "query": phrase})
    return out


# Minimal state -> CourtListener court IDs (federal districts + state high/appellate).
STATE_COURTS = {
    "california": "cand,cacd,casd,caed,cal,calctapp",
    "new york": "nysd,nyed,nynd,nywd,ny,nyappdiv",
    "texas": "txsd,txnd,txed,txwd,tex,texapp",
}


def courtlistener(phrase, token, jurisdiction=None):
    if not token:
        return []
    q = urllib.parse.quote(f'"{phrase}"')
    url = f"https://www.courtlistener.com/api/rest/v4/search/?q={q}&type=r&order_by=dateFiled+desc"
    courts = STATE_COURTS.get((jurisdiction or "").strip().lower())
    if courts:
        url += "&court=" + urllib.parse.quote(courts)
    hdr = {"User-Agent": UA, "Authorization": f"Token {token}"}
    out = []
    for attempt in range(2):
        try:
            data = json.loads(fetch(url, headers=hdr))
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                time.sleep(3)
                continue
            sys.stderr.write(f"  courtlistener failed [{phrase}]: HTTP {e.code}\n")
            return out
        except Exception as e:
            sys.stderr.write(f"  courtlistener failed [{phrase}]: {str(e)[:80]}\n")
            return out
    else:
        return out
    for r in (data.get("results") or [])[:5]:
        out.append({
            "kind": "court",
            "title": r.get("caseName") or r.get("docketNumber") or "case",
            "url": "https://www.courtlistener.com" + (r.get("absolute_url") or ""),
            "date": (r.get("dateFiled") or None),
            "source": r.get("court") or "CourtListener", "query": phrase,
        })
    return out


def build_queries(company, entities, contacts):
    qs = []  # (query, entity_id, relationship, proximity)
    primary = company.get("segment_focus") or company["name"]  # narrow big parents
    qs.append((primary, None, "primary", 1.0))
    for e in entities:
        if e.get("enabled", True):
            qs.append((e["name"], e["id"], e.get("type") or "entity", e.get("proximity_weight") or 0.8))
    for t in (company.get("watch_terms") or []):
        qs.append((t, None, "watch-term", 0.7))
    for ind in (company.get("industries") or []):
        qs.append((ind, None, "industry", 0.4))
    seen_int = set()
    for c in contacts:
        for i in (c.get("interests") or []):
            if i.lower() not in seen_int:
                seen_int.add(i.lower())
                qs.append((i, None, "interest", 0.5))
    # de-dup queries, cap
    uniq, out = set(), []
    for q in qs:
        if q[0] and q[0].lower() not in uniq:
            uniq.add(q[0].lower())
            out.append(q)
    return out[:12]


def classify(client, company, entities, candidates):
    ent_list = ", ".join(e["name"] for e in entities) or "(none recorded)"
    segment = company.get("segment_focus") or company["name"]
    juris = company.get("jurisdiction_focus")
    focus = (f"FOCUS NARROWLY on the business segment: \"{segment}\". For a large parent "
             f"(e.g. Amazon), DROP items about the broader company that do not concern this "
             f"segment (e.g. keep Amazon Studios items, drop AWS/retail items).\n")
    if juris:
        focus += (f"JURISDICTION FOCUS: only keep items in or clearly relevant to "
                  f"{juris} (e.g. {juris} litigation/regulators).\n")
    lines = []
    for i, c in enumerate(candidates):
        lines.append(f'{i}. [{c["kind"]}] "{c["title"]}" ({c.get("date") or "n/d"}; '
                     f'src={c.get("source")}; matched query="{c["query"]}")')
    prompt = (
        f"You filter news/court hits for a law firm's business development. The firm is "
        f"Steptoe LLP; the attorney does litigation/appeals.\n\n"
        f"Target company: {company['name']}\n"
        f"Known related entities (corporate family/peers/industry): {ent_list}\n"
        f"{focus}\n"
        f"Below are candidate items found by keyword search. Many will be NOISE "
        f"(wrong entity, generic word match, wrong segment, wrong jurisdiction, unrelated). "
        f"Keep ONLY items genuinely about the focus segment/entities above that a BD-minded "
        f"attorney would find relevant (litigation, regulatory action, M&A/funding, "
        f"leadership change, major business news, industry development).\n\n"
        f"CANDIDATES:\n" + "\n".join(lines) + "\n\n"
        f"Return ONLY a JSON array. For each KEPT item: "
        f'{{"index": int, "type": one of '
        f'["litigation","regulatory","corporate","leadership","news","industry-trend","commentary"], '
        f'(use "commentary" for analysis/opinion/client-alert/thought-leadership pieces) '
        f'"summary": "one sentence", "score_impact": 0.0-1.0 (BD relevance), '
        f'"entity": "which entity/company it concerns"}}. '
        f"If none are relevant, return []. No prose, JSON only."
    )
    msg = client.messages.create(model=MODEL, max_tokens=1500,
                                 messages=[{"role": "user", "content": prompt}])
    text = msg.content[0].text.strip()
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[-1]
    try:
        return json.loads(text)
    except Exception:
        try:
            start, end = text.find("["), text.rfind("]")
            return json.loads(text[start:end + 1]) if start >= 0 and end > start else []
        except Exception as e:
            sys.stderr.write(f"  classify parse failed: {str(e)[:80]}\n")
            return []


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    token = os.environ.get("COURTLISTENER_TOKEN", "").strip()
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    companies = db.get_companies_full()
    all_entities = db.get_entities()
    contacts = db.get("contacts?select=name,company_id,interests")
    ent_by_co, con_by_co = {}, {}
    for e in all_entities:
        ent_by_co.setdefault(e.get("related_company_id"), []).append(e)
    for c in contacts:
        con_by_co.setdefault(c.get("company_id"), []).append(c)

    if limit:
        companies = companies[:limit]

    total_new = 0
    for company in companies:
        ents = ent_by_co.get(company["id"], [])
        cons = con_by_co.get(company["id"], [])
        juris = company.get("jurisdiction_focus")
        queries = build_queries(company, ents, cons)
        named = {"primary", "parent", "subsidiary", "affiliate",
                 "peer-competitor", "customer-supplier", "co-defendant"}
        candidates = []
        for (qtext, ent_id, rel, prox) in queries:
            for hit in google_news(qtext, juris or ""):
                hit.update({"entity_id": ent_id, "relationship": rel, "proximity": prox})
                candidates.append(hit)
            # For topics (interests/industries), also pull legal/industry COMMENTARY & analysis.
            if rel in ("interest", "industry"):
                for hit in google_news(qtext, 'commentary OR analysis OR "client alert" OR "legal update"'):
                    hit["kind"] = "commentary"
                    hit.update({"entity_id": ent_id, "relationship": rel, "proximity": prox})
                    candidates.append(hit)
            # Only run court searches on named entities (not topic/industry/interest terms).
            if rel in named:
                for hit in courtlistener(qtext, token, juris):
                    hit.update({"entity_id": ent_id, "relationship": rel, "proximity": prox})
                    candidates.append(hit)
                time.sleep(0.8)  # be gentle with CourtListener's rate limit
            if len(candidates) >= MAX_CANDIDATES:
                break
        if not candidates:
            print(f"- {company['name']}: 0 candidates")
            continue

        kept = classify(client, company, ents, candidates)
        rows = []
        for k in kept:
            idx = k.get("index")
            if not isinstance(idx, int) or idx < 0 or idx >= len(candidates):
                continue
            cand = candidates[idx]
            if db.signal_exists(company["id"], cand.get("url"), cand.get("title")):
                continue
            kind_to_type = {"court": "litigation"}
            rows.append({
                "company_id": company["id"],
                "entity_id": cand.get("entity_id"),
                "type": k.get("type") or kind_to_type.get(cand["kind"], "news"),
                "relationship": cand.get("relationship"),
                "proximity_weight": cand.get("proximity") or 1.0,
                "event_date": cand.get("date"),
                "source": cand.get("source"),
                "url": cand.get("url"),
                "title": cand.get("title"),
                "summary": k.get("summary"),
                "score_impact": float(k.get("score_impact") or 0.0),
            })
        db.insert_signals(rows)
        total_new += len(rows)
        print(f"- {company['name']}: {len(candidates)} candidates -> {len(rows)} signals")

    print(f"\nDone. {total_new} new signals across {len(companies)} companies.")


if __name__ == "__main__":
    main()
