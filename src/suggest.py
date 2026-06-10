"""Anticipate & suggest engine for Project Activator (Phase 8).

Proactively proposes additions/actions into the `suggestions` inbox:
  - priority-flag : under-investing (high opportunity, low manual priority) or a
                    relationship flagged as drifting-and-not-ok -> re-engage.
  - new-contact   : a newly-named exec/GC in recent news, not already a contact.
  - new-entity    : a subsidiary/affiliate/brand mentioned in news, not yet tracked.
  - data-update   : an existing contact who appears to have changed roles/companies.
Rule-based items are deterministic; the new-contact/new-entity/data-update items come
from a Claude pass over recent leadership/corporate signals. Deduped against existing
suggestions (any status) so nothing is re-proposed.

Run: python3 src/suggest.py
Python 3.9 compatible.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import anthropic  # noqa: E402

MODEL = "claude-haiku-4-5-20251001"
TODAY = dt.date.today()


def main():
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    contacts = db.get("contacts?select=*")
    companies = {c["id"]: c for c in db.get("companies?select=*")}
    ent_by_co, con_by_co = {}, {}
    for e in db.get("entities?select=name,related_company_id"):
        ent_by_co.setdefault(e.get("related_company_id"), []).append((e.get("name") or "").lower())
    for c in contacts:
        con_by_co.setdefault(c.get("company_id"), []).append(c)

    proposed = []  # dicts: kind, target_ref, body

    # --- rule-based flags ---
    for c in contacts:
        if c.get("outreach_paused"):
            continue  # paused: don't nudge to re-engage (pause silences routine prompts)
        opp = c.get("opportunity_score") or 0
        pr = c.get("manual_priority") or 3
        if opp >= 60 and pr <= 2:
            proposed.append({"kind": "priority-flag", "target_ref": c["id"],
                             "body": f"Under-investing: {c['name']} scores {opp}/100 opportunity "
                                     f"but is priority {pr}/5 — consider raising priority."})
        if c.get("trajectory") == "apart" and c.get("trajectory_ok") is False:
            proposed.append({"kind": "priority-flag", "target_ref": c["id"],
                             "body": f"Drifting: {c['name']} is growing apart and you flagged "
                                     f"you're not OK with it — re-engage soon."})

    # --- Claude pass over recent leadership/corporate signals ---
    sigs = db.get("signals?select=company_id,type,title,summary&dismissed=eq.false"
                  "&type=in.(leadership,corporate)&order=event_date.desc.nullslast&limit=80")
    by_co = {}
    for s in sigs:
        by_co.setdefault(s.get("company_id"), []).append(s)
    for cid, slist in by_co.items():
        co = companies.get(cid)
        if not co:
            continue
        people = [c["name"] for c in con_by_co.get(cid, [])]
        ents = ent_by_co.get(cid, [])
        items = "\n".join(f"- [{s.get('type')}] {s.get('title','')}: {s.get('summary','')}"
                          for s in slist[:8])
        prompt = (
            f"Company: {co['name']}.\nExisting contacts: {people}.\n"
            f"Existing tracked entities: {ents}.\nRecent leadership/corporate news:\n{items}\n\n"
            f"Identify genuinely NEW, actionable BD additions. Return ONLY a JSON array of "
            f'{{"kind": "new-contact"|"new-entity"|"data-update", "name": "...", "detail": "one sentence"}}.\n'
            f"new-contact = a newly named executive/GC at this company NOT already a contact.\n"
            f"new-entity = a subsidiary/affiliate/brand mentioned NOT already tracked.\n"
            f"data-update = an existing contact who appears to have changed role/company.\n"
            f"Only include real, useful items. If none, return [].")
        try:
            msg = client.messages.create(model=MODEL, max_tokens=500,
                                         messages=[{"role": "user", "content": prompt}])
            text = msg.content[0].text.strip().strip("`")
            if text.startswith("json"):
                text = text[4:]
            arr = json.loads(text[text.find("["):text.rfind("]") + 1])
        except Exception as e:
            sys.stderr.write(f"  suggest failed for {co['name']}: {str(e)[:80]}\n")
            arr = []
        for it in arr:
            kind, name = it.get("kind"), str(it.get("name", "")).strip()
            if kind not in ("new-contact", "new-entity", "data-update") or not name:
                continue
            proposed.append({"kind": kind, "target_ref": f"{co['name']}: {name}",
                             "body": f"{co['name']} — {name}: {it.get('detail', '')}"})

    # --- dedupe against ALL existing suggestions (any status) + insert ---
    keys = {(r.get("kind"), (r.get("target_ref") or r.get("body") or "")[:120])
            for r in db.get("suggestions?select=kind,target_ref,body")}
    new_rows = []
    for s in proposed:
        k = (s["kind"], (s["target_ref"] or s["body"])[:120])
        if k in keys:
            continue
        keys.add(k)
        new_rows.append(s)
    if new_rows:
        db.post("suggestions", new_rows, prefer="return=minimal")
    print(f"Generated {len(new_rows)} new suggestions ({len(proposed)} proposed, rest deduped).")


if __name__ == "__main__":
    main()
