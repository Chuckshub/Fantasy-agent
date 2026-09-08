#!/usr/bin/env python3
"""Draft-day runtime.

Polls the live Sleeper draft, rebuilds our roster from picks already made,
and computes the pick whenever we are on the clock.

  python3 engine/run.py --now      one-shot: what should we take right now
  python3 engine/run.py --watch    continuous: poll and alert when on the clock
  python3 engine/run.py --board    top of the current draft board

  --watch --auto        also submit the pick through Chrome (engine/submit.py)
  --watch --auto --dry  run every submit check but never click - use this first

--auto is OFF unless asked for. It needs Chrome started with a debugging port
(see engine/submit.py) and the draft room open; if the browser is unreachable
or anything looks wrong, it aborts loudly and leaves the pick to Sleeper's own
autodraft queue rather than guessing.
"""
import sys, os, json, time, subprocess, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from value import build_board, load_config
import draft as D
import sync as SY
import submit as SUB
try:
    import queue_sync as QS
except Exception:
    QS = None
try:
    import notify_discord as ND
except Exception:
    ND = None

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(HERE, "logs", "draft_log.txt")
POLL_SEC = 20


def notify(title, msg):
    """Best-effort desktop alert; never let a missing notifier break the loop."""
    for cmd in (["notify-send", "-u", "critical", title, msg],
                ["zenity", "--info", "--text", f"{title}\n{msg}"]):
        try:
            subprocess.run(cmd, timeout=5,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except Exception:
            continue


def log(line):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    with open(LOG, "a") as f:
        f.write(f"[{stamp}] {line}\n")


def load_state():
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
    return cfg, st, picks


def on_the_clock(st, n_picks):
    cur, _ = st.next_two_picks(n_picks)
    return cur is not None and cur == n_picks + 1, cur


def render(st, n_picks, top_n=7):
    cands, meta = D.recommend(st, n_picks, top_n=top_n)
    if not cands:
        return None, meta, "no legal candidates"
    lines = []
    rnd = (meta["pick"] - 1) // st.teams + 1 if meta["pick"] else "?"
    lines.append(f"PICK {meta['pick']}  (round {rnd})   next pick: {meta['next_pick']}")
    have = st.roster_counts()
    lines.append(f"roster: {dict(sorted(have.items()))}   need: "
                 f"{ {k:v for k,v in st.unfilled_starters().items() if v} }")
    lines.append("")
    best = cands[0]
    p = best["player"]
    lines.append(f">>> TAKE: {p['name']}  ({p['pos']} - {p.get('team') or 'FA'})")
    lines.append(f"    {D.explain(best, meta)}")
    lines.append("")
    lines.append("    alternatives:")
    for c in cands[1:]:
        q = c["player"]
        lines.append(f"      {q['name']:<24}{q['pos']:<4} score {c['score']:6.1f}  {D.explain(c, meta)}")
    return best, meta, "\n".join(lines)


def cmd_now():
    cfg, st, picks = load_state()
    n = len(picks)
    mine, cur = on_the_clock(st, n)
    print(f"draft {cfg['draft_id']}  picks made: {n}  our slot: {st.my_slot}")
    best, meta, text = render(st, n)
    print()
    print(text)
    if not mine:
        print(f"\n(not on the clock - this is our upcoming pick {cur})")


def cmd_board():
    cfg, st, picks = load_state()
    avail = st.available()
    print(f"{'#':>3} {'PLAYER':<24}{'POS':<5}{'TM':<4}{'PROJ':>7}{'VORP':>8}{'TIER':>5}{'ADP':>7}")
    for p in avail[:30]:
        adp = f"{p['adp']:.1f}" if p["adp"] else "-"
        print(f"{p['overall_rank']:>3} {p['name']:<24}{p['pos']:<5}{str(p['team'] or '-'):<4}"
              f"{p['proj']:>7.1f}{p['vorp']:>8.1f}{p.get('tier',0):>5}{adp:>7}")


def auto_submit(cfg, player, pick_no, dry, port, attempts=2):
    """Hand the pick to Chrome. Never let a failure kill the watch loop.

    Retrying is safe: submit() re-checks the live pick count immediately
    before clicking, so a pick that actually landed on the first try makes the
    second attempt abort instead of drafting twice.
    """
    for i in range(1, attempts + 1):
        try:
            res = SUB.submit(player, pick_no, cfg=cfg, dry_run=dry, port=port)
        except Exception as e:                      # unreachable browser, etc.
            res = {"ok": False, "detail": f"{type(e).__name__}: {e}"}
        tag = "DRY RUN" if dry else "AUTO"
        print(f"  [{tag} attempt {i}] "
              f"{'OK' if res.get('ok') else 'FAILED'}: {res.get('detail')}")
        log(f"{tag} attempt {i} pick {pick_no} {player['name']}: "
            f"ok={res.get('ok')} {res.get('detail')}")
        if res.get("ok"):
            return True
        if res.get("clicked"):
            break                                   # clicked but unconfirmed: stop
        time.sleep(3)
    notify("DRAFT: AUTO-SUBMIT FAILED",
           f"pick {pick_no} {player['name']} - PICK MANUALLY")
    return False


DELIBERATE_SIMS = 4000        # the fast path uses 300


def deliberate(cfg, st, n, best, meta):
    """Re-decide the pick carefully, now that the clock is ours.

    Two things change versus the fast opinion the loop forms every 20 seconds:
    the injury and projection data is refreshed first, because a designation
    posted this morning can move a first-round call; and the survival
    Monte Carlo runs at DELIBERATE_SIMS rather than the default, because the
    candidates are often within a point of each other and 300 samples is not
    enough to separate them honestly.

    Returns the possibly-revised pick plus a note describing what changed, so
    the Discord post can say whether deliberation mattered or merely confirmed.
    """
    notes = []
    first = best["player"]["name"]
    try:
        import refresh as RF
        RF.refresh(verbose=False)
        notes.append("refreshed projections and injury designations")
    except Exception as e:
        notes.append(f"data refresh failed, used cached ({type(e).__name__})")
    try:
        cfg2 = load_config()
        board, _, _ = build_board(cfg2)
        st2 = D.DraftState(cfg2, board)
        st2.my_slot = cfg2.get("draft_slot")
        picks = SY.picks_made(cfg2["draft_id"]) or []
        for pk in picks:
            pid = pk.get("player_id")
            if not pid:
                continue
            st2.drafted[pid] = pk.get("draft_slot")
            if pk.get("draft_slot") == st2.my_slot and pid in st2.by_pid:
                st2.my_roster.append(st2.by_pid[pid])
        cands, meta2 = D.recommend(st2, len(picks), top_n=6, sims=DELIBERATE_SIMS)
        notes.append(f"re-ran at {DELIBERATE_SIMS} sims")
        if cands:
            if cands[0]["player"]["name"] != first:
                notes.append(f"**changed** {first} -> {cands[0]['player']['name']}")
            else:
                notes.append("confirmed the same player")
            st.__dict__.update(st2.__dict__)
            return cands[0], meta2, "", " · ".join(notes)
    except Exception as e:
        notes.append(f"re-decision failed, keeping fast pick ({type(e).__name__}: {e})")
    return best, meta, "", " · ".join(notes)


def cmd_watch(auto=False, dry=True, port=9222, refresh_queue=False,
              queue_every=8, queue_len=30):
    mode = "alert only" if not auto else ("auto-submit DRY RUN" if dry else "AUTO-SUBMIT ARMED")
    print(f"watching draft... [{mode}] ctrl-c to stop")
    if auto and not dry:
        print("  auto-submit is armed: picks will be made in Chrome automatically.")
    last_n, alerted_for = -1, None
    last_queue_n = -999
    while True:
        try:
            cfg, st, picks = load_state()
            n = len(picks)
            if n != last_n:
                new_picks = picks[last_n:] if last_n >= 0 else picks
                last_n = n
                log(f"picks={n} roster={st.roster_counts()}")
                print(f"\n--- {n} picks made ---")
                # Announce every pick, not just ours. Silence between our own
                # turns is indistinguishable from the automation being dead.
                if ND and new_picks:
                    try:
                        cur2, _ = st.next_two_picks(n)
                        avail = st.available()[:5]
                        ND.post(embeds=[ND.pick_feed_embed(
                            new_picks, f"#{cur2}" if cur2 else "-", cfg, avail)])
                    except Exception as e:
                        log(f"discord feed failed: {e}")
                # Keep the server-side fallback as current as the live engine.
                # Sleeper's autodraft skips players already taken, but it cannot
                # re-rank - so after a run at a position a frozen queue is stale.
                # Rebuilding it from the live board every few picks means that if
                # this process dies, the backstop still reflects the real draft.
                if QS and refresh_queue and n - last_queue_n >= queue_every:
                    last_queue_n = n
                    try:
                        r = QS.sync(cfg, trials=20, port=port, apply=True,
                                    limit=queue_len, verbose=False)
                        log(f"queue refreshed: {r['added']} added, "
                            f"order_ok={r['order_ok']}")
                        print(f"  [queue] refreshed to {r['added']} players, "
                              f"order_ok={r['order_ok']}")
                    except Exception as e:
                        log(f"queue refresh failed: {e}")
            mine, cur = on_the_clock(st, n)
            if mine and alerted_for != cur:
                alerted_for = cur
                best, meta, text = render(st, n)
                print("\n" + "=" * 64)
                print(text)
                print("=" * 64)
                if best:
                    p = best["player"]
                    log(f"ON CLOCK pick {cur} -> RECOMMEND {p['name']} ({p['pos']})")
                    notify(f"DRAFT: pick {cur}", f"TAKE {p['name']} ({p['pos']})")

                    # We get two hours per pick and were spending twenty
                    # seconds. Deliberate instead: pull fresh injury and
                    # projection data, then re-decide at much higher Monte
                    # Carlo precision. The first pass is a fast opinion; this
                    # is the one that gets submitted.
                    best, meta, text, delib = deliberate(cfg, st, n, best, meta)
                    p = best["player"]
                    # Log it too. Sending the record only to Discord means a
                    # failed post loses the only account of how a pick was made.
                    log(f"DELIBERATED pick {cur}: {p['name']} | {delib}")
                    cands_full, _ = D.recommend(st, n, top_n=6)
                    if ND:   # reporting must never take the draft loop down
                        try:
                            ND.post(embeds=[ND.pick_analysis_embed(
                                best, cands_full, meta, st, cfg, delib)])
                        except Exception as e:
                            log(f"discord post failed: {e}")
                    if auto:
                        auto_submit(cfg, p, cur, dry, port)
            time.sleep(POLL_SEC)
        except KeyboardInterrupt:
            print("\nstopped."); return
        except Exception as e:
            log(f"error: {e}")
            time.sleep(POLL_SEC)


if __name__ == "__main__":
    args = sys.argv[1:]
    mode = next((a for a in args if a in ("--now", "--watch", "--board")), "--now")
    if mode == "--watch":
        port = 9222
        if "--port" in args:
            port = int(args[args.index("--port") + 1])
        # --draft-id lets the whole watch loop be rehearsed against a mock draft
        # without editing config.json, which is the only safe way to exercise the
        # real draft-day command before the real draft.
        if "--draft-id" in args:
            # Overriding only the id is not enough: the engine also reads team
            # count and our slot from config, and a mock has neither of ours. A
            # rehearsal that quietly thinks it is slot 7 of 14 in a 10-team mock
            # never reaches its own pick and silently proves nothing.
            did = args[args.index("--draft-id") + 1]
            # sync() rewrites config.json in place. If this process dies before
            # it is put back, the real draft is left pointing at a mock - which
            # on draft morning would be catastrophic and silent. Snapshot first
            # and restore no matter how we exit.
            # atexit alone is not enough: it does not run on SIGTERM, which is
            # exactly how a timed-out or killed rehearsal ends. Tested - the
            # first version left the real config pointing at a mock.
            import shutil, atexit, signal
            cp = os.path.join(HERE, "config.json")
            bak = cp + ".prelive"
            shutil.copy2(cp, bak)

            def _restore(*_a):
                try:
                    shutil.copy2(bak, cp)
                    os.remove(bak)
                except Exception:
                    pass

            atexit.register(_restore)
            for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                signal.signal(_sig, lambda s, f: (_restore(), sys.exit(143)))
            SY.sync(did, "coderchuck")
            _c = load_config()
            print(f"[rehearsal] draft {did}  teams={_c['teams']} "
                  f"slot={_c.get('draft_slot')}  (config auto-restores on exit)")
        try:
            cmd_watch(auto="--auto" in args, dry="--dry" in args, port=port,
                   refresh_queue="--queue" in args)
        finally:
            pass
    else:
        {"--now": cmd_now, "--board": cmd_board}[mode]()
