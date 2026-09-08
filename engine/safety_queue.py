#!/usr/bin/env python3
"""Zero-touch safety queue covering the WHOLE draft.

If nobody is at the keyboard, Sleeper autodrafts the top available player in
your queue. That queue must survive every round, so a plain best-available list
fails -- it would happily hand us six WRs and no QB. Instead we simulate the
draft forward many times, let the engine pick with full roster-need logic, and
record what it actually wanted each round. Alternates per round are interleaved
so a run on one position cannot derail us.

  python3 engine/safety_queue.py [trials]
"""
import sys, os, random
from collections import defaultdict, Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from value import build_board, load_config
import simulate as S
import sync as SY

ALT_PER_ROUND = 3     # depth of cover against positional runs


def build(cfg=None, board=None, trials=30):
    """The queue as data: [(round, player, confidence)], drafted players removed.

    Split out of main() so the live watcher can rebuild it mid-draft. Re-running
    this after other teams pick is what makes the fallback adaptive rather than a
    frozen list - `gone` is read from the live draft every time.
    """
    cfg = cfg or load_config()
    if board is None:
        board, _, _ = build_board(cfg)
    my_slot = cfg["draft_slot"]
    picks = SY.picks_made(cfg["draft_id"]) if cfg.get("draft_id") else []
    gone = {pk["player_id"] for pk in picks if pk.get("player_id")}

    per_round = defaultdict(Counter)
    for t in range(trials):
        rng = random.Random(9000 + t * 7)
        rosters, _, _ = S.run_mock(my_slot, cfg, board, rng)
        for rnd, p in enumerate(rosters[my_slot], 1):
            per_round[rnd][p["pid"]] += 1

    by_pid = {p["pid"]: p for p in board}
    queue, seen = [], set(gone)
    for rnd in sorted(per_round):
        added = 0
        for pid, hits in per_round[rnd].most_common(10):
            if pid in seen or pid not in by_pid:
                continue
            queue.append((rnd, by_pid[pid], hits / trials))
            seen.add(pid)
            added += 1
            if added >= ALT_PER_ROUND:
                break
    return queue, len(gone)


def main():
    trials = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    cfg = load_config()
    board, _, _ = build_board(cfg)
    my_slot = cfg["draft_slot"]

    picks = SY.picks_made(cfg["draft_id"]) if cfg.get("draft_id") else []
    gone = {pk["player_id"] for pk in picks if pk.get("player_id")}

    per_round = defaultdict(Counter)
    for t in range(trials):
        rng = random.Random(9000 + t * 7)
        rosters, _, _ = S.run_mock(my_slot, cfg, board, rng)
        for rnd, p in enumerate(rosters[my_slot], 1):
            per_round[rnd][p["pid"]] += 1

    by_pid = {p["pid"]: p for p in board}
    queue, seen = [], set(gone)
    for rnd in sorted(per_round):
        added = 0
        for pid, hits in per_round[rnd].most_common(10):
            if pid in seen or pid not in by_pid:
                continue
            queue.append((rnd, by_pid[pid], hits / trials))
            seen.add(pid)
            added += 1
            if added >= ALT_PER_ROUND:
                break

    print(f"ZERO-TOUCH SAFETY QUEUE  |  slot {my_slot}/{cfg['teams']}"
          f"  |  {cfg['scoring']}  {cfg['rounds']} rounds")
    print(f"built from {trials} simulated drafts | {len(gone)} players already drafted\n")
    print(f"{'#':>3}  {'PLAYER':<24}{'POS':<5}{'TM':<4}{'BYE':>4}{'PROJ':>7}{'ADP':>6}{'RD':>4}{'CONF':>6}")
    for i, (rnd, p, conf) in enumerate(queue, 1):
        adp = f"{p['adp']:.0f}" if p.get("adp") else "-"
        print(f"{i:>3}. {p['name']:<24}{p['pos']:<5}{str(p.get('team') or 'FA'):<4}"
              f"{str(p.get('bye') or '-'):>4}{p['proj']:>7.1f}{adp:>6}{rnd:>4}{conf*100:>5.0f}%")

    print(f"\nposition mix: {dict(Counter(p['pos'] for _, p, _ in queue))}")
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "logs", "safety_queue.txt")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        for i, (rnd, p, c) in enumerate(queue, 1):
            f.write(f"{i}. {p['name']} ({p['pos']}-{p.get('team') or 'FA'})\n")
    print(f"paste-ready list -> {out}")


if __name__ == "__main__":
    main()
