#!/usr/bin/env python3
"""Push the engine's queue into Sleeper's draft room, and keep it current.

    python3 engine/queue_sync.py --show              what Sleeper holds now
    python3 engine/queue_sync.py --dry-run           what we would set it to
    python3 engine/queue_sync.py --apply             set it
    python3 engine/queue_sync.py --autopick on|off   the server-side backstop

Sleeper's autodraft picks the highest-ranked *available* player from your queue,
so a queue already skips players other teams have taken. What it cannot do is
re-rank: if a run on running backs breaks out, a frozen list does not notice.

That is what this fixes. `safety_queue.build()` re-reads the live draft every
time, so re-running this between our picks produces a queue that reflects what
has actually happened. The live watcher calls it automatically; the fallback
then stays roughly as smart as the engine instead of decaying all draft long.

Everything here is a browser action - Sleeper has no write API - so every change
is read back and compared before it is reported as done.
"""
import sys, os, json, time, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cdp
import safety
import safety_queue as SQ
import sync as SY
from value import build_board, load_config

SETTLE = 1.1          # Sleeper debounces the player search
CLEAR_SETTLE = 2.5    # React keeps flushing removals after the list reads empty
CLICK_GAP = 0.45      # be polite to a React list that re-renders on every add


class QueueError(Exception):
    pass


JS_READ = r"""
(() => {
  const panel = document.querySelector('.sortable-queue-list');
  if (!panel) return JSON.stringify({ok: false, why: 'no queue panel'});
  // Queue entries are NOT built like player-list rows: they are
  // .item-container > .draft-queue-player-item-player > .meta-container > .name,
  // and carry no .name-wrapper at all. Assuming otherwise made the reader
  // silently report an empty queue that in fact held three players.
  const names = [...panel.querySelectorAll('.item-container .name')]
      .map(n => (n.textContent || '').trim())
      .filter(Boolean);
  return JSON.stringify({ok: true, names,
                         empty: !!document.querySelector('.empty-queue-state')});
})()
"""

# One click per call, driven from Python. A JS loop cannot work here: React
# re-renders the list asynchronously, so a synchronous loop re-finds the same
# node and "removes" the first entry eighty times while nothing changes. The
# earlier version reported clearing 80 entries from a queue of 3.
JS_DELETE_ONE = r"""
(() => {
  const panel = document.querySelector('.sortable-queue-list');
  if (!panel) return JSON.stringify({left: 0});
  const btn = panel.querySelector('.item-container .delete-button');
  if (btn) btn.click();
  return JSON.stringify({
    clicked: !!btn,
    left: panel.querySelectorAll('.item-container').length
  });
})()
"""

JS_SET_SEARCH = r"""
(() => {
  const i = document.querySelector('.player-search input');
  if (!i) return JSON.stringify({ok: false});
  const s = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype, 'value').set;
  s.call(i, %s);
  i.dispatchEvent(new Event('input', {bubbles: true}));
  return JSON.stringify({ok: true});
})()
"""

JS_ADD = r"""
(() => {
  const want = %s;
  const norm = s => (s || '').toLowerCase().replace(/[^a-z]/g, '');
  const rows = [...document.querySelectorAll('div.player-rank-item2')];
  const hits = rows.filter(r => {
    const n = r.querySelector('.name-wrapper')?.childNodes[0]?.nodeValue || '';
    const parts = ((r.querySelector('.name-wrapper .position') || {}).innerText || '')
        .split('\n').map(s => s.trim());
    const nameOk = want.pos === 'DEF' ? true : norm(n) === norm(want.name);
    return nameOk && parts[0] === want.pos && (!want.team || parts[1] === want.team);
  });
  if (hits.length !== 1) return JSON.stringify({ok: false, n: hits.length});
  const q = hits[0].querySelector('.queue-action');
  if (!q) return JSON.stringify({ok: false, why: 'no queue button'});
  q.click();
  return JSON.stringify({ok: true});
})()
"""

# The checkbox is hidden AND marked readonly, so clicking it does nothing; the
# visible control is the .slider span. Clicking the wrapper, the .switch, or the
# input itself all silently fail while still reporting success.
JS_AUTOPICK = r"""
(() => {
  const c = document.querySelector('.autopick-toggle input[type=checkbox]');
  const s = document.querySelector('.autopick-toggle .slider');
  if (!c || !s) return JSON.stringify({ok: false, why: 'no autopick toggle'});
  const want = %s;
  if (c.checked !== want) s.click();
  return JSON.stringify({ok: true, checked: c.checked, wanted: want});
})()
"""


def live_queue(cfg, board, depth=30):
    """The fallback queue, built from the roster we ACTUALLY have.

    safety_queue.build() runs whole simulated drafts from an empty roster, so it
    has no idea what we already hold. With a quarterback, two backs and four
    receivers on our books it still led the autopick fallback with Brock Purdy -
    a second QB - while tight end, kicker and defence sat unfilled. If the
    watcher had died and a clock expired, Sleeper would have drafted exactly
    that.

    Instead, walk the live engine forward: take its top pick, pretend we got
    him, re-rank, repeat. Every entry is then evaluated against the roster as it
    would actually stand, so need, bye collisions and positional caps all apply.
    """
    import draft as D
    st = D.DraftState(cfg, board)
    st.my_slot = cfg.get("draft_slot")
    picks = SY.picks_made(cfg["draft_id"]) or []
    for pk in picks:
        pid = pk.get("player_id")
        if not pid:
            continue
        st.drafted[pid] = pk.get("draft_slot")
        if pk.get("draft_slot") == st.my_slot and pid in st.by_pid:
            st.my_roster.append(st.by_pid[pid])
    n = len(picks)
    out = []
    for i in range(depth):
        cands, _ = D.recommend(st, n + i, top_n=1)
        if not cands:
            break
        p = cands[0]["player"]
        out.append(p)
        st.drafted[p["pid"]] = st.my_slot
        st.my_roster.append(p)
    return out, len(picks)


def _display(p):
    """Sleeper lists defenses by city, not as 'LAR DEF'."""
    if p["pos"] == "DEF":
        import submit as SUB
        return SUB.def_display_name(p.get("team")) or p["name"]
    return p["name"]


def _queue_len(page):
    r = page.evaluate(
        "JSON.stringify(document.querySelectorAll("
        "'.sortable-queue-list .item-container').length)")
    return int(r) if isinstance(r, (int, float, str)) and str(r).isdigit() else 0


def read_queue(page):
    r = page.evaluate(JS_READ)
    if not (isinstance(r, dict) and r.get("ok")):
        raise QueueError(f"cannot read the queue: {r}")
    return r["names"]


def clear_queue(page, cap=80):
    """Remove every queued player, one click at a time.

    `.queue-action` on a player row is a TOGGLE, not an add - clicking it for
    someone already queued silently removes them. So the queue must genuinely be
    empty before loading a new order, or entries vanish mid-write. That is what
    happened the first time: two players survived the clear, and "adding" them
    took them straight back out.
    """
    removed = 0
    for _ in range(cap):
        r = page.evaluate(JS_DELETE_ONE)
        if not isinstance(r, dict) or not r.get("clicked"):
            break
        removed += 1
        time.sleep(0.35)                 # let React re-render before re-reading
        if r.get("left", 0) <= 0:
            break
    left = len(read_queue(page))
    if left:
        raise QueueError(f"could not clear the queue: {left} entries remain")
    return removed


def set_queue(page, players, verbose=True):
    """Replace the queue with `players`, in order. Returns (added, missing).

    JS_ADD reports success on the click, not on the player actually landing in
    the queue. Combined with React still flushing the clear, the first six adds
    of a 30-player write silently evaporated - the log said "added 30, missing
    0" while Sleeper held 24, all of them the tail. So the clear is given time
    to settle, and each add is confirmed against the queue length rather than
    trusted.
    """
    removed = clear_queue(page)
    time.sleep(CLEAR_SETTLE)
    if verbose:
        print(f"  cleared {removed} existing entr{'y' if removed == 1 else 'ies'}")
    added, missing = 0, []
    for p in players:
        name = _display(p)
        page.evaluate(JS_SET_SEARCH % json.dumps(name))
        time.sleep(SETTLE)
        want = {"name": name, "pos": p["pos"], "team": p.get("team") or ""}
        r = page.evaluate(JS_ADD % json.dumps(want))
        time.sleep(CLICK_GAP)
        if isinstance(r, dict) and r.get("ok"):
            # Confirm it actually landed. One retry, because the common failure
            # is a click into a list that was still re-rendering.
            if _queue_len(page) > added:
                added += 1
            else:
                page.evaluate(JS_ADD % json.dumps(want))
                time.sleep(CLICK_GAP)
                if _queue_len(page) > added:
                    added += 1
                else:
                    missing.append((p["name"], "click did not register"))
        else:
            missing.append((p["name"], r))
    page.evaluate(JS_SET_SEARCH % json.dumps(""))
    time.sleep(SETTLE)
    return added, missing


def verify(page, players):
    """Compare what Sleeper holds against what we meant to set."""
    import submit as SUB
    got = read_queue(page)
    want = [_display(p) for p in players]
    n = min(len(got), len(want))
    mismatch = [(i, want[i], got[i]) for i in range(n)
                if SUB.norm(want[i]) != SUB.norm(got[i])]
    return {"in_sleeper": len(got), "intended": len(want),
            "order_ok": not mismatch and len(got) == len(want),
            "mismatch": mismatch[:5]}


def set_autopick(page, on):
    """Flip the toggle and confirm it actually moved."""
    want = "true" if on else "false"
    r = page.evaluate(JS_AUTOPICK % want)
    if not (isinstance(r, dict) and r.get("ok")):
        raise QueueError(f"cannot set autopick: {r}")
    time.sleep(0.9)
    after = page.evaluate(JS_AUTOPICK % want)
    if bool(after.get("checked")) is not bool(on):
        raise QueueError(
            f"autopick did not change: wanted {on}, still {after.get('checked')}")
    return after


def sync(cfg=None, trials=30, port=cdp.DEFAULT_PORT, apply=False,
         limit=None, verbose=True):
    cfg = cfg or load_config()
    board, _, _ = build_board(cfg)
    q, gone = live_queue(cfg, board, depth=trials and 30 or 30)
    players = list(q)

    # NO re-sorting. An earlier version prepended the top-12 board players and
    # then sorted the first ten entries by VORP, which was right when the queue
    # came from fresh-draft simulations and arrived in no useful order. Against
    # live_queue it is destructive: that ladder is already ordered by what we
    # need next, and re-sorting by raw VORP threw Houston's defence (19) to the
    # top and Mark Andrews (8) down to eighth - inverting the exact reasoning
    # that put a tight end first. The elite-prepend is obsolete too; those
    # players were drafted in round one.
    players = players[:limit or len(players)]
    if verbose:
        print(f"queue: {len(players)} players ({gone} already drafted)")
    if not apply:
        return {"players": players, "applied": False}
    page, tab = cdp.attach(f"/draft/nfl/{cfg['draft_id']}", port)
    try:
        # Strip commissioner controls before touching anything. Chuck is a
        # co-commissioner, so START DRAFT is live in this DOM.
        safety.guard(page, verbose=verbose)
        added, missing = set_queue(page, players, verbose)
        res = verify(page, players)
        res.update(added=added, missing=missing, applied=True, players=players)
        return res
    finally:
        page.close()


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--show", action="store_true")
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    g.add_argument("--autopick", choices=("on", "off"))
    ap.add_argument("--draft-id")
    ap.add_argument("--trials", type=int, default=30)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--port", type=int, default=cdp.DEFAULT_PORT)
    a = ap.parse_args()
    cfg = load_config()
    if a.draft_id:
        cfg["draft_id"] = a.draft_id

    if a.show or a.autopick:
        page, tab = cdp.attach(f"/draft/nfl/{cfg['draft_id']}", a.port)
        try:
            safety.guard(page, verbose=True)
            if a.autopick:
                r = set_autopick(page, a.autopick == "on")
                print(f"autopick is now: {'ON' if r.get('checked') else 'OFF'}")
            else:
                names = read_queue(page)
                print(f"Sleeper queue holds {len(names)}:")
                for i, n in enumerate(names, 1):
                    print(f"  {i:>3}. {n}")
        finally:
            page.close()
        return

    res = sync(cfg, a.trials, a.port, apply=a.apply, limit=a.limit)
    if not res.get("applied"):
        print("would set, in order:")
        for i, p in enumerate(res["players"], 1):
            print(f"  {i:>3}. {_display(p):<26}{p['pos']:<5}{p.get('team') or '-'}")
        return
    print(f"  added {res['added']}, missing {len(res['missing'])}")
    for nm, r in res["missing"]:
        print(f"    ! {nm}: {r}")
    print(f"  read-back: {res['in_sleeper']} in Sleeper vs {res['intended']} intended"
          f"  order_ok={res['order_ok']}")
    for i, w, g_ in res["mismatch"]:
        print(f"    position {i + 1}: wanted {w}, found {g_}")
    sys.exit(0 if res["order_ok"] else 1)


if __name__ == "__main__":
    main()
