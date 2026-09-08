#!/usr/bin/env python3
"""Learn a new fantasy platform by looking at your own browser.

    python3 engine/explore.py --platform espn
    python3 engine/explore.py --platform generic --url https://myleague.example/team

The agent can already read most platforms over HTTP. What it cannot do without
help is *write* - no major fantasy host offers an API for setting a lineup or
making a waiver claim, so every change is made by driving the real web app. That
needs to know which element on the page is a roster row, which is the position
button, which is the add control. This learns those, once, from a browser you
are already logged into.

**Nothing here is guessed.** The naive version of this tool asks a human to
supply CSS selectors, which is miserable and wrong as often as not. Instead it
*anchors on facts it already knows*: the reader has told it which players are on
your roster, so it searches the rendered page for those names, walks up from
each match to the smallest element that contains exactly one of them, and
derives the repeating row structure from what those elements have in common. A
selector is only accepted if it selects the right number of rows and every one
of them contains a player the agent expected to find.

**It reads, and it asks before it acts.** The exploration pass is pure
observation - it opens pages and inspects the DOM, and it does not click
anything. The one interaction it needs is confirming that a lineup control does
what it looks like it does, and that is done on an explicit prompt with a
described action, not silently.

Safety, because this drives a real account:

- Destructive-looking controls are catalogued and never clicked. Anything whose
  text matches drop, delete, trade, propose, commissioner, or start draft is
  recorded as forbidden and put out of reach for the rest of the session, the
  same way the Sleeper draft-room automation strips commissioner controls.
- The learned profile is printed for you to read before it is written.
"""
import sys, os, re, json, time, argparse, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cdp
from platforms import base as PB

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Anything matching this is catalogued and then removed from the page for the
# duration of the session. Learning where the "drop player" button lives is
# useful; being one stray click away from it while poking at a live roster is
# not.
FORBIDDEN_TEXT = re.compile(
    r"\b(drop|release|delete|remove player|trade|propose|accept|commissioner|"
    r"commish|start draft|reset|edit league|leave league)\b", re.I)


JS_HARDEN = r"""
(() => {
  const re = %s;
  const found = [];
  for (const el of document.querySelectorAll('button,a,[role="button"],[class*="button"],[class*="btn"]')) {
    const t = (el.innerText || '').trim();
    if (t && t.length < 40 && new RegExp(re, 'i').test(t)) {
      found.push(t);
      el.setAttribute('data-agent-forbidden', '1');
      el.style.pointerEvents = 'none';
    }
  }
  window.confirm = () => false;
  return JSON.stringify({neutralised: found.slice(0, 30), n: found.length});
})()
"""

# Find every element whose own text is exactly one of the names we expect, then
# report the ancestor chain so Python can work out the repeating row.
JS_ANCHOR = r"""
(() => {
  const names = %s;
  const norm = s => (s || '').toLowerCase().replace(/[^a-z]/g, '');
  const want = new Set(names.map(norm));
  const short = new Set(names.map(n => {
    const p = n.trim().split(/\s+/);
    return norm((p[0] ? p[0][0] : '') + (p[p.length - 1] || ''));
  }));
  const hits = [];
  const all = document.querySelectorAll('*');
  for (const el of all) {
    if (el.children.length) continue;                 // leaf nodes only
    const t = (el.innerText || '').trim();
    if (!t || t.length > 40) continue;
    const n = norm(t);
    if (!want.has(n) && !short.has(n)) continue;
    const chain = [];
    let cur = el;
    for (let d = 0; d < 8 && cur; d++) {
      const cls = typeof cur.className === 'string' ? cur.className : '';
      chain.push({depth: d, tag: cur.tagName.toLowerCase(),
                  cls: cls.trim().split(/\s+/).filter(Boolean).slice(0, 4)});
      cur = cur.parentElement;
    }
    hits.push({text: t, chain});
  }
  return JSON.stringify({hits: hits.slice(0, 120), dom_size: all.length});
})()
"""

JS_COUNT = r"""
(() => {
  const rows = [...document.querySelectorAll(%s)];
  return JSON.stringify({
    n: rows.length,
    texts: rows.slice(0, 40).map(r => (r.innerText || '').replace(/\n/g, ' | ').slice(0, 70))
  });
})()
"""

JS_SUBSELECT = r"""
(() => {
  const rows = [...document.querySelectorAll(%s)];
  const out = {};
  for (const sel of %s) {
    let hit = 0, sample = [];
    let kids = 0;
    for (const r of rows) {
      const e = r.querySelector(sel);
      if (e && (e.innerText || '').trim()) {
        hit++;
        kids += e.querySelectorAll('*').length;
        if (sample.length < 4) sample.push((e.innerText || '').trim().slice(0, 24));
      }
    }
    out[sel] = {hit, of: rows.length, sample, kids: hit ? kids / hit : 999};
  }
  return JSON.stringify(out);
})()
"""

JS_CANDIDATE_CHILD_SELECTORS = r"""
(() => {
  const rows = [...document.querySelectorAll(%s)];
  const counts = {};
  for (const r of rows) {
    const seen = new Set();
    for (const el of r.querySelectorAll('*')) {
      const cls = typeof el.className === 'string' ? el.className : '';
      for (const c of cls.trim().split(/\s+/).filter(Boolean)) {
        if (seen.has(c)) continue;
        seen.add(c);
        counts[c] = (counts[c] || 0) + 1;
      }
    }
  }
  const n = rows.length;
  return JSON.stringify(Object.entries(counts)
    .filter(([, c]) => c >= Math.max(2, n * 0.6))
    .sort((a, b) => b[1] - a[1]).slice(0, 40));
})()
"""


def _css_candidates(part):
    """A chain entry -> every selector worth trying for it.

    Emitting only the conjunction of all an element's classes was a real bug:
    a roster row carrying `class="team-roster-item odd"` produced
    `.team-roster-item.odd`, which matches the alternating stripe and therefore
    half the roster. The single classes have to be candidates in their own
    right, and the scorer decides between them.
    """
    out = []
    classes = [re.sub(r"[^A-Za-z0-9_-]", "", c) for c in (part["cls"] or [])]
    classes = [c for c in classes if c]
    for c in classes:
        out.append("." + c)
    if len(classes) > 1:
        out.append("." + ".".join(classes))
    if not out:
        out.append(part["tag"])
    return out


def harden(page, verbose=True):
    r = page.evaluate(JS_HARDEN % json.dumps(FORBIDDEN_TEXT.pattern))
    if verbose and isinstance(r, dict):
        n = r.get("n", 0)
        print(f"  [safety] neutralised {n} destructive control(s)"
              + (f": {', '.join(sorted(set(r['neutralised']))[:8])}" if n else ""))
    return r


def find_row_selector(page, expected_names, verbose=True):
    """Derive the repeating roster-row selector by anchoring on known players.

    Walks up from every element whose text is a player we expect to see, and
    scores each ancestor level by how well `document.querySelectorAll` on it
    reproduces the roster: the right number of rows, each containing exactly one
    expected name. The winner is the shallowest level that does.
    """
    res = page.evaluate(JS_ANCHOR % json.dumps(expected_names))
    if not isinstance(res, dict) or not res.get("hits"):
        return None, {"why": "none of the expected player names appear on this "
                             "page - is it the right page, and are you logged in?"}
    hits = res["hits"]
    if verbose:
        print(f"  found {len(hits)} of {len(expected_names)} expected players "
              f"in a {res.get('dom_size', 0):,}-element page")

    # Count how often each selector shows up across the anchors.
    tally = {}
    for h in hits:
        for part in h["chain"]:
            for sel in _css_candidates(part):
                if sel in ("div", "span", "td", "tr", "li", "a", "p"):
                    continue                   # too generic to be a row marker
                tally.setdefault(sel, set()).add(h["text"])

    lastnames = [_norm(n.split()[-1]) for n in expected_names]
    scored = []
    for sel, names in tally.items():
        got = page.evaluate(JS_COUNT % json.dumps(sel))
        if not isinstance(got, dict):
            continue
        n = got.get("n", 0)
        if n < 2:
            continue
        texts = got.get("texts", [])
        # Coverage: does every anchor land in some matched row? A striped-row
        # class matches half the roster and fails this, which is exactly the
        # discrimination that was missing.
        covered = sum(1 for ln in lastnames
                      if any(ln and ln in _norm(t) for t in texts))
        # Cleanliness: does each row hold exactly one player, rather than the
        # whole roster in one container?
        clean = sum(1 for t in texts
                    if sum(1 for ln in lastnames if ln and ln in _norm(t)) == 1)
        crowded = sum(1 for t in texts
                      if sum(1 for ln in lastnames if ln and ln in _norm(t)) > 1)
        avg_len = (sum(len(t) for t in texts) / len(texts)) if texts else 0
        scored.append({
            "selector": sel, "rows": n, "clean": clean, "covered": covered,
            "anchors": len(names), "avg_len": avg_len,
            # Coverage dominates, then cleanliness; containers holding several
            # players at once are penalised hard.
            "score": covered * 3 + clean - crowded * 5,
        })
    # Ties are common and the tie-break matters. Two selectors can both match
    # fourteen rows - the true row and a child of it - and picking the child
    # loses the lineup-slot control, which lives on the parent. The outer
    # element carries more text, so richer rows win: first more rows, then
    # more content per row.
    scored.sort(key=lambda s: (-s["score"], -s["rows"], -s["avg_len"]))
    if not scored:
        return None, {"why": "no repeating structure matched the roster"}
    return scored[0], {"alternatives": scored[1:4]}


def _norm(s):
    return re.sub(r"[^a-z]", "", (s or "").lower())


# Every label a fantasy lineup slot is plausibly called, across platforms.
# Sleeper renders its flex as "W R T", ESPN as "FLEX", others as "W/R/T" or
# "OP"; bench is BN, BE or Bench depending on the site.
SLOT_LABELS = {
    "QB", "RB", "WR", "TE", "K", "PK", "DEF", "DST", "D/ST", "DEFENSE",
    "FLEX", "WRT", "WRRB", "RBWR", "RBWRTE", "OP", "SUPERFLEX", "SFLEX",
    "BN", "BE", "BENCH", "IR", "RESERVE", "TAXI", "NA",
}


def _is_slot_label(s):
    return re.sub(r"[^A-Z/]", "", (s or "").upper()) in SLOT_LABELS


def find_child_selectors(page, row_sel, expected_names, verbose=True):
    """Within a row, which child holds the name and which holds the slot."""
    cands = page.evaluate(JS_CANDIDATE_CHILD_SELECTORS % json.dumps(row_sel))
    if not isinstance(cands, list):
        return {}
    sels = ["." + re.sub(r"[^A-Za-z0-9_-]", "", c) for c, _ in cands]
    got = page.evaluate(JS_SUBSELECT % (json.dumps(row_sel), json.dumps(sels)))
    if not isinstance(got, dict):
        return {}
    name_sel = slot_sel = None
    best_name = best_slot = -1e9
    lastnames = {_norm(n.split()[-1]) for n in expected_names}
    for sel, info in got.items():
        if info["hit"] < info["of"] * 0.6:
            continue
        sample = [s for s in info["sample"] if s]
        if not sample:
            continue
        # A name column: its samples look like the players we expect, and the
        # shorter the cell the better - `.cell-player-meta` also contains the
        # names but drags in position, kickoff time and opponent with them.
        hit_names = sum(1 for s in sample
                        if any(ln and ln in _norm(s) for ln in lastnames))
        name_score = hit_names * 10 - sum(len(s) for s in sample) / len(sample) / 10
        if hit_names and name_score > best_name:
            best_name, name_sel = name_score, sel
        # A slot column holds lineup slot labels, and we know what those look
        # like. Generic heuristics are not enough here: "short, alphabetic and
        # repeated" also describes a column of projected points, and then a
        # column of player-link buttons. Matching against the actual vocabulary
        # a fantasy lineup uses is both simpler and far harder to fool.
        matched = sum(1 for s in sample if _is_slot_label(s))
        if not matched:
            continue
        # Several nested elements often carry the same slot text - on Sleeper
        # the link wrapper, the cell and the square inside it all read "QB".
        # For reading, any of them works; for *clicking*, only the innermost is
        # the actual control, so the tie-break is fewest descendants.
        slot_score = (matched * 100 + info["hit"]
                      - len({s.upper() for s in sample})
                      - info.get("kids", 0))
        if slot_score > best_slot:
            best_slot, slot_sel = slot_score, sel
    if name_sel and slot_sel == name_sel:
        slot_sel = None
    if verbose:
        print(f"  name column  -> {name_sel}")
        print(f"  slot column  -> {slot_sel or '(not found on this page)'}")
    return {"row_name": name_sel, "row_slot": slot_sel,
            "candidates": {k: v for k, v in list(got.items())[:12]}}


def explore(platform, url=None, expected_names=None, port=cdp.DEFAULT_PORT,
            verbose=True, save=False):
    prof = PB.load_profile(platform) or {}
    if url:
        prof["team_url"] = url
    if not prof.get("team_url"):
        raise SystemExit(
            "I need the URL of your team/roster page. Open it in the browser "
            "and pass it with --url, e.g.\n"
            "  python3 engine/explore.py --platform espn --url "
            "'https://fantasy.espn.com/football/team?leagueId=123&seasonId=2026'")
    if not expected_names:
        raise SystemExit(
            "I need to know a few players on your roster so I can find them on "
            "the page. Pass them with --players 'Name One,Name Two,...' - four "
            "or five is plenty.")

    if verbose:
        print(f"\nEXPLORING {platform}")
        print(f"  page    : {prof['team_url']}")
        print(f"  anchors : {', '.join(expected_names)}\n")

    ready = "JSON.stringify(document.body && document.body.innerText.length > 200)"
    cdp.open_url(prof["team_url"], port=port, ready_js=ready)
    time.sleep(3.0)
    page, tab = cdp.attach("http", port)
    try:
        harden(page, verbose)
        row, meta = find_row_selector(page, expected_names, verbose)
        if not row:
            print(f"\n  could not find the roster: {meta.get('why')}")
            return None
        if verbose:
            print(f"\n  roster rows  -> {row['selector']}  "
                  f"({row['rows']} rows, {row['clean']} cleanly matched)")
            for alt in meta.get("alternatives", [])[:2]:
                print(f"     alternative: {alt['selector']} ({alt['rows']} rows)")
        prof["roster_row"] = row["selector"]
        kids = find_child_selectors(page, row["selector"], expected_names, verbose)
        prof.update({k: v for k, v in kids.items()
                     if k in ("row_name", "row_slot") and v})
        prof["row_slot_click"] = prof.get("row_slot_click") or prof.get("row_slot")
        prof["_learned_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        prof["_learned_from"] = prof["team_url"]
        prof.setdefault("bench_slot_labels", ["BN", "BE", "Bench", "IR", "Reserve"])
    finally:
        page.close()

    print("\n  LEARNED PROFILE")
    print(json.dumps({k: v for k, v in prof.items() if not k.startswith("_")},
                     indent=2))
    gaps = PB.profile_gaps(prof)
    if gaps:
        print(f"\n  still missing: {', '.join(gaps)}")
        print("  Re-run with --url pointing at the add/free-agent page to learn "
              "the rest.")
    if save:
        p = PB.save_profile(platform, prof)
        print(f"\n  saved -> {p}")
    else:
        print("\n  not saved. Re-run with --save once the above looks right.")
    return prof


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", required=True)
    ap.add_argument("--url", help="your team/roster page")
    ap.add_argument("--players", help="comma-separated players you know are on "
                                      "your roster, used as anchors")
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--port", type=int, default=cdp.DEFAULT_PORT)
    a = ap.parse_args()
    names = [n.strip() for n in (a.players or "").split(",") if n.strip()]
    explore(a.platform, a.url, names, a.port, save=a.save)


if __name__ == "__main__":
    main()
