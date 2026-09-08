#!/usr/bin/env python3
"""Autonomous feature builder, driven from Discord by !f-lenovo.

    python3 engine/feature.py --add "make !board show bye weeks"
    python3 engine/feature.py --work        build the next queued request
    python3 engine/feature.py --list

A request is built by a headless Claude Code run inside an isolated **git
worktree**, never in the live tree. That isolation is the whole design: while a
draft is live there is a watcher polling every 20 seconds and a bot answering
commands out of these same files, and editing them underneath a running process
is how you lose a pick.

Three rules the worker enforces:

1. **Worktree only.** The live checkout is never touched, so a half-finished
   edit cannot reach the running watcher.
2. **It must still parse and import.** Every engine module is compiled and the
   package imported before the work is accepted. A feature that breaks the
   engine is a failed build, not a feature.
3. **No merge while a draft is live.** The branch is committed and reported, and
   merging waits until the draft is complete. Hot-patching the system that is
   actively drafting for us is not a risk worth taking for a convenience
   feature.
"""
import sys, os, json, time, argparse, subprocess, shutil
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as DB
from value import load_config

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKROOT = os.path.expanduser("~/.statking-features")
BUILD_TIMEOUT = 1800

try:
    import notify_discord as ND
except Exception:
    ND = None

# Files that drive a live draft. The builder is told not to touch them, and the
# validator refuses the build if it did anyway - an instruction is a request, a
# check is a guarantee.
PROTECTED = ("engine/run.py", "engine/submit.py", "engine/queue_sync.py",
             "engine/safety.py", "engine/cdp.py")


def add(request, requester=None):
    con = DB.connect()
    cur = con.execute(
        "INSERT INTO feature_request (ts, requester, request) VALUES (?,?,?)",
        (int(time.time()), requester, request.strip()))
    con.commit()
    return cur.lastrowid


def listing(limit=15):
    con = DB.connect()
    return con.execute(
        "SELECT * FROM feature_request ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def _set(fid, **kw):
    con = DB.connect()
    cols = ", ".join(f"{k}=?" for k in kw)
    con.execute(f"UPDATE feature_request SET {cols} WHERE id=?",
                (*kw.values(), fid))
    con.commit()


def draft_is_live():
    try:
        import sync as SY
        cfg = load_config()
        d = SY.get(f"{SY.API}/draft/{cfg['draft_id']}") or {}
        return d.get("status") == "drafting"
    except Exception:
        return True          # unknown means assume live, and hold


def validate(tree):
    """The build must compile, import, and leave the live-draft files alone."""
    touched = subprocess.run(["git", "diff", "--name-only", "HEAD"], cwd=tree,
                             capture_output=True).stdout.decode().split()
    hit = [f for f in touched if f in PROTECTED]
    if hit:
        return False, f"refused: modified live-draft files {hit}"

    eng = os.path.join(tree, "engine")
    bad = []
    for f in sorted(os.listdir(eng)):
        if not f.endswith(".py"):
            continue
        r = subprocess.run([sys.executable, "-m", "py_compile",
                            os.path.join(eng, f)], capture_output=True)
        if r.returncode:
            bad.append(f"{f}: {r.stderr.decode()[:160]}")
    if bad:
        return False, "compile failures -> " + " | ".join(bad[:3])

    r = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0,'engine');"
         "import value, draft, lineup, model, bot, notify_discord, queue_sync"],
        cwd=tree, capture_output=True, timeout=180)
    if r.returncode:
        return False, "import failed -> " + r.stderr.decode()[-200:]
    return True, "compiles, imports, and left the live-draft files untouched"


PROMPT = """You are extending StatKing, a pure-stdlib Python fantasy football engine.

Requested feature:
{request}

Rules, which matter more than the feature itself:
- Python 3 standard library ONLY. There is no pip on this machine.
- Do NOT modify run.py, submit.py, queue_sync.py, safety.py or cdp.py. Those
  drive a LIVE draft. A change to them fails the build.
- New Discord commands belong in engine/bot.py using the existing @cmd decorator.
- Match the surrounding style. Comments explain WHY, not what.
- Every file must still compile and the engine must still import.

Make the change, then briefly state what you changed and why."""


def work(fid=None, verbose=True):
    con = DB.connect()
    row = (con.execute("SELECT * FROM feature_request WHERE id=?", (fid,)).fetchone()
           if fid else
           con.execute("SELECT * FROM feature_request WHERE status='queued' "
                       "ORDER BY id LIMIT 1").fetchone())
    if not row:
        return {"ok": False, "detail": "nothing queued"}
    fid = row["id"]
    branch = f"feat/{fid}"
    tree = os.path.join(WORKROOT, f"f{fid}")
    _set(fid, status="building")
    if verbose:
        print(f"  building #{fid}: {row['request'][:70]}")

    shutil.rmtree(tree, ignore_errors=True)
    os.makedirs(WORKROOT, exist_ok=True)
    subprocess.run(["git", "worktree", "prune"], cwd=HERE, capture_output=True)
    subprocess.run(["git", "branch", "-D", branch], cwd=HERE, capture_output=True)
    r = subprocess.run(["git", "worktree", "add", "-b", branch, tree, "HEAD"],
                       cwd=HERE, capture_output=True)
    if r.returncode:
        _set(fid, status="failed", summary=r.stderr.decode()[:300],
             finished=int(time.time()))
        return {"ok": False, "detail": r.stderr.decode()[:200]}

    try:
        proc = subprocess.run(
            ["claude", "-p", PROMPT.format(request=row["request"]),
             "--permission-mode", "acceptEdits"],
            cwd=tree, capture_output=True, timeout=BUILD_TIMEOUT)
        out = (proc.stdout or b"").decode()[-1500:]
    except subprocess.TimeoutExpired:
        _set(fid, status="failed", summary=f"timed out after {BUILD_TIMEOUT}s",
             finished=int(time.time()))
        return {"ok": False, "detail": "build timed out"}
    except FileNotFoundError:
        _set(fid, status="failed", summary="claude CLI not found",
             finished=int(time.time()))
        return {"ok": False, "detail": "claude CLI not found on PATH"}

    ok, detail = validate(tree)
    changed = subprocess.run(["git", "status", "--porcelain"], cwd=tree,
                             capture_output=True).stdout.decode().strip()
    if ok and not changed:
        ok, detail = False, "the build made no changes"

    if ok:
        subprocess.run(["git", "add", "-A"], cwd=tree, capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", f"feature #{fid}: {row['request'][:60]}",
             "-m", out[-400:],
             "-m", "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"],
            cwd=tree, capture_output=True)
        held = draft_is_live()
        _set(fid, status="held" if held else "built", branch=branch,
             summary=(out[-600:] or detail), finished=int(time.time()))
        result = {"ok": True, "id": fid, "branch": branch, "held": held,
                  "detail": detail, "output": out[-600:]}
    else:
        _set(fid, status="failed", branch=branch, summary=detail,
             finished=int(time.time()))
        result = {"ok": False, "id": fid, "branch": branch, "detail": detail}

    if ND:
        try:
            colour = ND.GREEN if result["ok"] else ND.RED
            fields = [("Request", row["request"][:900], 0),
                      ("Validation", detail, 0)]
            if result.get("held"):
                fields.append((
                    "Held, not merged",
                    f"Built on branch `{branch}`. A draft is live, and "
                    "hot-patching the engine that is currently drafting for us "
                    "is not worth a convenience feature. Merges once the draft "
                    "completes.", 0))
            ND.post(embeds=[ND.embed(
                f"Feature #{fid} " + ("built" if result["ok"] else "FAILED"),
                (result.get("output") or detail)[:1500], colour, fields,
                "built by a headless Claude run in an isolated git worktree")])
        except Exception as e:
            print(f"  discord report failed: {e}")
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--add")
    g.add_argument("--work", action="store_true")
    g.add_argument("--list", action="store_true")
    ap.add_argument("--id", type=int)
    a = ap.parse_args()
    if a.add:
        print("queued as #%d" % add(a.add))
    elif a.list:
        for r in listing():
            print(f"  #{r['id']:<4}{r['status']:<10}{(r['branch'] or '-'):<12}"
                  f"{r['request'][:60]}")
    else:
        print(json.dumps(work(a.id), indent=1)[:900])
