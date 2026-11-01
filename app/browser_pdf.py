"""Serve the book's source PDF to the system browser with read-along sync.

Replaces the built-in QLabel+QScrollArea read-along panel: clicking
"Read PDF" now opens a lightweight local HTTP server (bound to
127.0.0.1 on an ephemeral port) and the system default browser. The
served page uses PDF.js to render the book PDF and keeps the existing
capabilities:

- auto-scroll to the sentence currently being read
- highlight the current sentence + word as audio advances
- click a sentence to seek the audio to that position

The main window pushes a small playback-state snapshot into the server
every UI tick (``update_state``) and the browser page polls
``/api/state`` every few hundred ms, so no WebSocket is needed. When a
book has no alignment data the page degrades gracefully to a plain PDF
viewer (page navigation, no highlight/scroll).
"""
import json
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _resource(rel_path):
    """Resolve a bundled resource in dev and PyInstaller frozen modes."""
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel_path)


class ReadAlongServer:
    """Tiny local server exposing PDF + sync data to the browser."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pdf_path = None
        self._alignments = []
        self._chapter_offsets = []
        self._lang = "en"
        self._state = {
            "chapter_idx": -1,
            "global_time": 0.0,
            "playing": False,
            "rate": 1.0,
        }
        self._seek_cb = None
        self._server = None
        self._thread = None
        self._url = None

    # ------------------------------------------------------------ control
    @property
    def url(self):
        return self._url

    def start(self):
        if self._server is not None:
            return
        handler = _HandlerFactory(self)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever,
            name="readalong-http",
            daemon=True,
        )
        self._thread.start()
        port = server.server_address[1]
        self._url = f"http://127.0.0.1:{port}/"

    def shutdown(self):
        server = self._server
        self._server = None
        if server is not None:
            try:
                server.shutdown()
            except Exception:  # noqa: BLE001 - best effort
                pass

    def set_seek_callback(self, cb):
        """cb(global_audio_seconds, chapter_idx=None) on the HTTP worker thread."""
        self._seek_cb = cb

    def set_book(self, pdf_path, alignments, chapter_offsets, lang):
        rows = []
        for r in (alignments or []):
            row = dict(r)
            if isinstance(row.get("bbox"), str):
                try:
                    row["bbox"] = json.loads(row["bbox"])
                except (ValueError, TypeError):
                    row["bbox"] = None
            rows.append(row)
        with self._lock:
            self._pdf_path = pdf_path if pdf_path and os.path.exists(pdf_path) else None
            self._alignments = rows
            self._chapter_offsets = list(chapter_offsets or [])
            self._lang = lang or "en"
            self._state = {
                "chapter_idx": -1,
                "global_time": 0.0,
                "playing": False,
                "rate": 1.0,
            }

    def update_state(self, chapter_idx, global_time, playing, rate):
        with self._lock:
            self._state = {
                "chapter_idx": chapter_idx,
                "global_time": global_time,
                "playing": bool(playing),
                "rate": float(rate),
            }

    # ------------------------------------------------------------ data out
    def _snapshot(self):
        with self._lock:
            return {
                "pdf_path": self._pdf_path,
                "alignments": self._alignments,
                "chapter_offsets": self._chapter_offsets,
                "lang": self._lang,
                "state": dict(self._state),
            }

    def _payload_data(self):
        snap = self._snapshot()
        return {
            "pdf_url": "/pdf",
            "lang": snap["lang"],
            "has_data": bool(snap["pdf_path"]) and bool(snap["alignments"]),
            "chapter_offsets": snap["chapter_offsets"],
            "alignments": snap["alignments"],
        }

    def _payload_state(self):
        return dict(self._snapshot()["state"])


class _HandlerFactory:
    """Build a handler class bound to a specific server instance."""

    def __init__(self, owner):
        self._owner = owner

    def __call__(self, *args, **kwargs):
        return _Handler(self._owner, *args, **kwargs)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, owner, *args, **kwargs):
        self._owner = owner
        super().__init__(*args, **kwargs)

    # ------------------------------------------------------------ helpers
    def _send(self, code, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # silence request logs
        return

    # ------------------------------------------------------------ routing
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        owner = self._owner
        if path == "/":
            html = _resource(os.path.join("assets", "readalong", "reader.html"))
            self._send_file(html, "text/html; charset=utf-8")
        elif path == "/pdf":
            snap = owner._snapshot()
            if not snap["pdf_path"]:
                self._send(404, "no pdf", "text/plain; charset=utf-8")
                return
            self._send_file(snap["pdf_path"], "application/pdf")
        elif path == "/api/data":
            self._send(200, owner._payload_data())
        elif path == "/api/state":
            self._send(200, owner._payload_state())
        elif path.startswith("/pdfjs/"):
            name = os.path.basename(path)
            if name not in ("pdf.min.js", "pdf.worker.min.js"):
                self._send(404, "not found", "text/plain; charset=utf-8")
                return
            fp = _resource(os.path.join("assets", "readalong", "pdfjs", name))
            self._send_file(fp, "application/javascript; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        owner = self._owner
        if path == "/api/seek":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                data = json.loads(raw.decode("utf-8") or "{}")
                audio_start = float(data.get("audio_start", -1.0))
                chapter_raw = data.get("chapter_idx")
                chapter_idx = (
                    int(chapter_raw) if chapter_raw is not None else None
                )
            except (ValueError, TypeError):
                self._send(400, {"ok": False, "error": "bad audio_start"})
                return
            cb = owner._seek_cb
            if cb is not None and audio_start >= 0.0:
                try:
                    cb(audio_start, chapter_idx)
                except Exception:  # noqa: BLE001 - never kill the worker
                    pass
            self._send(200, {"ok": True})
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")


def open_in_browser(pdf_path=None, alignments=None, chapter_offsets=None,
                    lang="en", seek_cb=None):
    """One-shot helper: start the server (if needed) and open the page."""
    server = getattr(open_in_browser, "_server", None)
    if server is None:
        server = ReadAlongServer()
        server.set_seek_callback(seek_cb)
        server.start()
        open_in_browser._server = server
    server.set_book(pdf_path, alignments, chapter_offsets, lang)
    if server.url:
        webbrowser.open(server.url)
    return server
