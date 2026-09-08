#!/usr/bin/env python3
"""Report the outcome of the pre-draft setup to Discord."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cdp, safety, queue_sync as QS
import notify_discord as ND
from value import load_config

qrc = int(sys.argv[1]) if len(sys.argv) > 1 else 1
arc = int(sys.argv[2]) if len(sys.argv) > 2 else 1
cfg = load_config()
names, autopick, err = [], None, None
try:
    page, tab = cdp.attach(f"/draft/nfl/{cfg['draft_id']}", cdp.DEFAULT_PORT)
    try:
        safety.guard(page)
        names = QS.read_queue(page)
        autopick = page.evaluate(
            "JSON.stringify(!!document.querySelector"
            "('.autopick-toggle input[type=checkbox]').checked)")
    finally:
        page.close()
except Exception as e:
    err = f"{type(e).__name__}: {e}"

ok = qrc == 0 and arc == 0 and not err
preview = "\n".join(f"{i}. {n}" for i, n in enumerate(names[:12], 1))
fields = [("Queue in Sleeper", f"**{len(names)}** players\n{preview}"
           + ("\n..." if len(names) > 12 else ""), 0),
          ("Autopick", "**ON**" if autopick else "**OFF**", 1),
          ("Not touched", "draft not started, no settings changed, no picks made", 1)]
if err:
    fields.append(("Error", f"`{err}`", 0))
ND.post(embeds=[ND.embed(
    "Pre-draft setup complete" if ok else "Pre-draft setup NEEDS ATTENTION",
    "Queue loaded and autopick enabled. Draft at 9:00am MDT."
    if ok else "Something did not verify - check before the draft starts.",
    ND.GREEN if ok else ND.RED, fields,
    "commissioner controls were stripped from the page before any interaction")])
print("posted")
