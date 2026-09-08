#!/usr/bin/env python3
"""Generate the Sleeper draft queue for the configured team.

Sleeper autodrafts the highest player in your queue who is still available.
A single "best pick" is useless if he is gone; a well-ordered queue is
contingency-proof. We build it by repeatedly asking the engine what it would
take, assuming each prior queue entry was already taken by someone else --
so the ordering stays correct however the board falls.

  python3 engine/queue.py [depth]
"""
import sys, os, copy
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from value import build_board, load_config
import draft as D
import sync as SY


def build_queue(depth=25):
    cfg = load_config()
    board, _, _ = build_board(cfg)
    st = D.DraftState(cfg, board)
    st.my_slot = cfg.get("draft_slot")

    picks = SY.picks_made(cfg["draft_id"]) if cfg.get("draft_id") else []
    for pk in picks:
        pid = pk.get("player_id")
        if not pid:
            continue
        st.drafted[pid] = pk.get("draft_slot")
        if pk.get("draft_slot") == st.my_slot and pid in st.by_pid:
            st.my_roster.append(st.by_pid[pid])

    n = len(picks)
    cur, _ = st.next_two_picks(n)
    out = []
    sim_drafted = dict(st.drafted)
    for i in range(depth):
        probe = D.DraftState(cfg, board)
        probe.my_slot = st.my_slot
        probe.drafted = dict(sim_drafted)
        probe.my_roster = list(st.my_roster)
        cands, meta = D.recommend(probe, n, top_n=1)
        if not cands:
            break
        p = cands[0]["player"]
        out.append((p, cands[0], meta))
        # assume he's gone -- next entry is our answer if he is
        sim_drafted[p["pid"]] = "sim"
    return cfg, st, cur, n, out


def main():
    depth = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    cfg, st, cur, n, out = build_queue(depth)
    have = st.roster_counts()
    print(f"DRAFT QUEUE  (slot {st.my_slot})")
    print(f"picks made: {n}   our pick: {cur}   roster: {dict(sorted(have.items())) or 'empty'}")
    need = {k: v for k, v in st.unfilled_starters().items() if v}
    print(f"still need: {need}")
    print("\nEnter these into Sleeper's queue IN THIS ORDER:\n")
    for i, (p, c, meta) in enumerate(out, 1):
        adp = f"ADP {p['adp']:.0f}" if p.get("adp") else "no ADP"
        inj = f"  [{p['injury']}]" if p.get("injury") else ""
        print(f"  {i:>2}. {p['name']:<24}{p['pos']:<4}{str(p.get('team') or 'FA'):<4}"
              f"  proj {p['proj']:>6.1f}  vorp {p['vorp']:>6.1f}  {adp}{inj}")
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "logs", "queue.txt")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for i, (p, c, m) in enumerate(out, 1):
            f.write(f"{i}. {p['name']} ({p['pos']}-{p.get('team') or 'FA'})\n")
    print(f"\nplain list written to {path}")


if __name__ == "__main__":
    main()
