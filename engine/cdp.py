#!/usr/bin/env python3
"""Minimal Chrome DevTools Protocol client - Python 3 stdlib only.

There is no websocket library on this machine and no way to install one, so
this speaks RFC 6455 directly over a socket. It implements exactly the subset
CDP needs: a text-frame client that masks what it sends, reassembles what it
receives, answers pings, and matches replies to request ids.

Chrome must be running with a remote debugging port:

    google-chrome --remote-debugging-port=9222 \
                  --user-data-dir=$HOME/.statking-chrome

Chrome 136+ refuses --remote-debugging-port on the default profile directory,
which is why --user-data-dir is mandatory rather than optional.
"""
import base64, json, os, socket, struct, time, urllib.request

DEFAULT_PORT = 9222


class CDPError(Exception):
    pass


# ---------------------------------------------------------------- discovery
def http_json(path, port=DEFAULT_PORT, host="127.0.0.1", timeout=5):
    url = f"http://{host}:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        raise CDPError(
            f"cannot reach Chrome devtools at {host}:{port} ({e}). "
            f"Start Chrome with --remote-debugging-port={port} "
            f"--user-data-dir=$HOME/.statking-chrome"
        )


def find_tab(url_substring, port=DEFAULT_PORT, host="127.0.0.1"):
    """First page target whose url contains url_substring."""
    for t in http_json("/json/list", port, host):
        if t.get("type") == "page" and url_substring in (t.get("url") or ""):
            return t
    raise CDPError(f"no open Chrome tab matching {url_substring!r}")


# ---------------------------------------------------------------- websocket
class _WS:
    """Client-side RFC 6455 text framing. Only what CDP needs."""

    def __init__(self, ws_url, timeout=20):
        if not ws_url.startswith("ws://"):
            raise CDPError(f"unexpected devtools url scheme: {ws_url[:12]}")
        rest = ws_url[5:]
        netloc, _, path = rest.partition("/")
        host, _, port = netloc.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {netloc}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        self._buf = b""
        head = self._read_until(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise CDPError(f"websocket upgrade refused: {head.split(chr(13).encode())[0]!r}")

    # -- raw io ------------------------------------------------------------
    def _read_until(self, sentinel):
        while sentinel not in self._buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise CDPError("devtools socket closed during handshake")
            self._buf += chunk
        head, _, self._buf = self._buf.partition(sentinel)
        return head + sentinel

    def _read_exact(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise CDPError("devtools socket closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    # -- frames ------------------------------------------------------------
    def send_text(self, text):
        payload = text.encode()
        n = len(payload)
        hdr = bytearray([0x81])                      # FIN + text
        if n < 126:
            hdr.append(0x80 | n)
        elif n < (1 << 16):
            hdr.append(0x80 | 126); hdr += struct.pack("!H", n)
        else:
            hdr.append(0x80 | 127); hdr += struct.pack("!Q", n)
        mask = os.urandom(4)
        hdr += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(hdr) + masked)

    def _recv_frame(self):
        b0, b1 = self._read_exact(2)
        fin, opcode = b0 & 0x80, b0 & 0x0F
        ln = b1 & 0x7F
        if ln == 126:
            ln = struct.unpack("!H", self._read_exact(2))[0]
        elif ln == 127:
            ln = struct.unpack("!Q", self._read_exact(8))[0]
        if b1 & 0x80:                                 # server must not mask
            raise CDPError("masked frame from server")
        return fin, opcode, self._read_exact(ln)

    def recv_text(self):
        """Next complete text message, transparently handling ping/continuation."""
        parts, op = [], None
        while True:
            fin, opcode, data = self._recv_frame()
            if opcode == 0x9:                         # ping -> pong
                mask = os.urandom(4)
                masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
                self.sock.sendall(bytes([0x8A, 0x80 | len(data)]) + mask + masked)
                continue
            if opcode == 0xA:                         # pong
                continue
            if opcode == 0x8:
                raise CDPError("devtools closed the connection")
            if opcode in (0x1, 0x2):
                op, parts = opcode, [data]
            elif opcode == 0x0:
                parts.append(data)
            if fin and op is not None:
                return b"".join(parts).decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------- session
class Page:
    """A CDP session against one tab. Only Runtime.evaluate is needed."""

    def __init__(self, ws_url, timeout=20):
        self.ws = _WS(ws_url, timeout)
        self._id = 0

    def call(self, method, params=None, max_events=400):
        self._id += 1
        mid = self._id
        self.ws.send_text(json.dumps({"id": mid, "method": method,
                                      "params": params or {}}))
        for _ in range(max_events):
            msg = json.loads(self.ws.recv_text())
            if msg.get("id") != mid:
                continue                              # unsolicited event
            if "error" in msg:
                raise CDPError(f"{method}: {msg['error'].get('message')}")
            return msg.get("result", {})
        raise CDPError(f"no reply to {method} after {max_events} messages")

    def evaluate(self, expression, timeout_ms=10000):
        """Evaluate JS in the page and return the value.

        The expression must produce a JSON string; returning by value keeps
        Chrome from handing back opaque remote object handles.
        """
        res = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
            "timeout": timeout_ms,
        })
        if res.get("exceptionDetails"):
            det = res["exceptionDetails"]
            msg = (det.get("exception") or {}).get("description") or det.get("text")
            raise CDPError(f"page threw: {msg}")
        val = (res.get("result") or {}).get("value")
        if isinstance(val, str):
            try:
                return json.loads(val)
            except ValueError:
                return val
        return val

    def close(self):
        self.ws.close()


def attach(url_substring, port=DEFAULT_PORT, host="127.0.0.1"):
    tab = find_tab(url_substring, port, host)
    return Page(tab["webSocketDebuggerUrl"]), tab


def open_url(url, port=DEFAULT_PORT, host="127.0.0.1", timeout=30,
             ready_js=None):
    """Point an existing tab at `url` and wait until it is genuinely usable.

    Auto-submit needs the draft room already open in the debugging Chrome. Making
    that a manual step meant one forgotten browser tab could cost a live pick, so
    it is scripted instead. Reuses a tab rather than opening new ones, since
    /json/new is refused for security reasons in current Chrome builds.

    `ready_js` must be an expression returning JSON `true` once the page is
    actually ready. Do not rely on `document.readyState`: Sleeper is a React app
    that reports "complete" long before it renders anything, and a *finished*
    draft sits on "LOADING" forever while still claiming to be complete. Waiting
    on the element you need is the only honest signal.
    """
    tabs = [t for t in http_json("/json/list", port, host) if t.get("type") == "page"]
    if not tabs:
        raise CDPError("no page target in Chrome to navigate")
    page = Page(tabs[0]["webSocketDebuggerUrl"])
    try:
        page.call("Page.enable")
        page.call("Page.navigate", {"url": url})
        expr = ready_js or "JSON.stringify(document.readyState === 'complete')"
        deadline = time.time() + timeout
        ok = False
        while time.time() < deadline:
            try:
                if page.evaluate(expr) is True:
                    ok = True
                    break
            except CDPError:
                pass                      # mid-navigation; try again
            time.sleep(0.5)
    finally:
        page.close()
    if not ok:
        raise CDPError(
            f"{url} did not become ready within {timeout}s. The tab is open but "
            f"the app never rendered - Sleeper does this indefinitely for a "
            f"completed draft.")
    return True


if __name__ == "__main__":
    import sys
    frag = sys.argv[1] if len(sys.argv) > 1 else "sleeper.com"
    page, tab = attach(frag)
    print("attached to:", tab.get("title"))
    print("title via JS:", page.evaluate("JSON.stringify(document.title)"))
    page.close()
