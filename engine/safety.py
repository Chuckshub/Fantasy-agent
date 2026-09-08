#!/usr/bin/env python3
"""Make commissioner-only controls unreachable before automating a draft room.

If you are a league co-commissioner, the real draft room renders controls an
ordinary manager never sees - START DRAFT above all. Any script running in that
session can reach them, and clicking START DRAFT would begin the league's real
draft early and irreversibly.

Care is not a safeguard. This physically removes those elements from the DOM
before any interaction, so a mistyped selector, a fuzzy text match or a stray
coordinate click cannot land on one. It is re-applied before every action
because React re-renders and will put them back.

One specific trap this closes: START DRAFT raises a native confirm() dialog.
Earlier work here overrode window.confirm to return true so that a *mock* draft
could be started programmatically. If that override were ever applied to the real
room, a single mis-click would auto-confirm starting the league's draft. So this
does the opposite - it forces confirm() to return false.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Everything a commissioner can press that we must never reach.
FORBIDDEN = [
    ".start-draft-button",
    ".start-draft-text",
    ".draft-settings-text",
    ".share-link",
    "[class*='commish']",
    "[class*='commissioner']",
]

JS_HARDEN = r"""
(() => {
  const sels = %s;
  let removed = 0;
  const seen = [];
  for (const sel of sels) {
    for (const el of document.querySelectorAll(sel)) {
      seen.push(sel);
      el.remove();
      removed++;
    }
  }
  // Also neutralise anything whose visible text is a start action, in case the
  // class names change under us.
  for (const el of document.querySelectorAll('div,button,span,a')) {
    if (el.children.length === 0 &&
        /^\s*(START DRAFT|START|PAUSE DRAFT|RESET DRAFT)\s*$/i.test(el.textContent || '')) {
      const box = el.closest('button,[class*="button"]') || el;
      box.remove();
      removed++;
      seen.push('text:' + el.textContent.trim());
    }
  }
  // A confirm() that answers itself is how an accidental commissioner action
  // would get committed. Refuse everything instead.
  window.confirm = () => false;
  window.__statking_hardened = true;
  return JSON.stringify({removed, matched: [...new Set(seen)].slice(0, 8)});
})()
""" % __import__("json").dumps(FORBIDDEN)

JS_AUDIT = r"""
(() => {
  const sels = %s;
  const present = [];
  for (const sel of sels) {
    if (document.querySelector(sel)) present.push(sel);
  }
  for (const el of document.querySelectorAll('div,button,span,a')) {
    if (el.children.length === 0 &&
        /^\s*START DRAFT\s*$/i.test(el.textContent || '')) present.push('text:START DRAFT');
  }
  return JSON.stringify({
    hardened: !!window.__statking_hardened,
    still_present: [...new Set(present)],
    confirm_refuses: window.confirm() === false
  });
})()
""" % __import__("json").dumps(FORBIDDEN)


class UnsafePage(Exception):
    pass


def harden(page, verbose=False):
    """Strip commissioner controls out of the page. Returns what it removed."""
    r = page.evaluate(JS_HARDEN)
    if verbose and isinstance(r, dict):
        print(f"  [safety] removed {r.get('removed', 0)} commissioner control(s) "
              f"{r.get('matched') or ''}")
    return r


def audit(page):
    """Confirm nothing dangerous is reachable. Raises if it is."""
    r = page.evaluate(JS_AUDIT)
    if not isinstance(r, dict):
        raise UnsafePage(f"could not audit the page: {r}")
    if r.get("still_present"):
        raise UnsafePage(
            f"commissioner controls still reachable: {r['still_present']}")
    if not r.get("confirm_refuses"):
        raise UnsafePage("window.confirm does not refuse - a dialog could commit")
    return r


def guard(page, verbose=False):
    """Harden then verify. Use before any interaction with the real draft room."""
    harden(page, verbose)
    return audit(page)
