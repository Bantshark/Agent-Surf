"""A tiny local HTTPS + WebSocket server on the stdlib, for the inbox tests.

One TLS port serves the synthetic inbox page and its WebSocket. Chromium maps
feed.test to 127.0.0.1, so the page and the socket share a local origin (a
page routed from a HAR has a public origin and Chromium's local-network
checks would block a socket to 127.0.0.1). Throwaway cert made with openssl.
"""

import base64
import hashlib
import json
import socket
import ssl
import struct
import subprocess
import threading

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

INBOX_PAGE = b"""<!doctype html><html><head><meta charset="utf-8"><title>Inbox</title></head>
<body><main><h1>Messages</h1><ul id="threads"></ul></main>
<script>
const ws = new WebSocket('wss://' + location.host + '/socket/inbox');
ws.binaryType = 'arraybuffer';
ws.onmessage = (e) => {
  const text = typeof e.data === 'string' ? e.data : new TextDecoder().decode(e.data);
  let j; try { j = JSON.parse(text); } catch (err) { return; }
  for (const t of (j.data && j.data.threads) || []) {
    const li = document.createElement('li');
    li.setAttribute('data-thread-id', t.id);
    li.textContent = t.from + ': ' + t.snippet;
    document.getElementById('threads').appendChild(li);
  }
};
</script></body></html>"""

FRAMES = [
    ("text", json.dumps({"type": "inbox", "data": {"threads": [
        {"id": "m1", "from": "user_a", "snippet": "Synthetic hello \U0001f44b"},
        {"id": "m2", "from": "user_b", "snippet": "Synthetic question"}]}}).encode()),
    ("text", b"ping"),                                   # not JSON: ignored
    ("binary", json.dumps({"type": "inbox", "data": {"threads": [
        {"id": "m3", "from": "user_c", "snippet": "Synthetic binary frame"},
        {"id": "m4", "from": "user_d", "snippet": "Another one"}]}}).encode()),
    ("binary", b"\xff\xfe\x00 not utf-8"),               # not UTF-8: ignored
]


def _frame(kind, payload):
    op = 0x1 if kind == "text" else 0x2
    n = len(payload)
    head = bytes([0x80 | op]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n))
    return head + payload


def make_cert(directory):
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-keyout", str(directory / "key.pem"), "-out", str(directory / "cert.pem"),
                    "-subj", "/CN=feed.test", "-addext", "subjectAltName=DNS:feed.test"],
                   check=True, capture_output=True)


class InboxServer:
    def __init__(self, tls_dir):
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(tls_dir / "cert.pem", tls_dir / "key.pem")
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.sockets_opened = 0
        self._stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        self.sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, raw):
        try:
            conn = self.ctx.wrap_socket(raw, server_side=True)
            req = b""
            while b"\r\n\r\n" not in req:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                req += chunk
            if b"upgrade: websocket" in req.lower():
                key = next(l.split(b":", 1)[1].strip() for l in req.split(b"\r\n")
                           if l.lower().startswith(b"sec-websocket-key"))
                accept = base64.b64encode(hashlib.sha1(key + GUID).digest())
                conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                             b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
                self.sockets_opened += 1
                for kind, payload in FRAMES:
                    conn.sendall(_frame(kind, payload))
                self._stop.wait(30)
            else:
                body = INBOX_PAGE
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                             b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
            conn.close()
        except Exception:
            pass

    def close(self):
        self._stop.set()
        self.sock.close()
