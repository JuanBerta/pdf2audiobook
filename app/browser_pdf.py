"""Serve the book's source PDF to the system browser with read-along sync.

A tiny HTTP server bound to 127.0.0.1 on an ephemeral port serves the PDF,
the sentence alignment and a playback-state snapshot; the page (PDF.js)
polls ``/api/state`` and posts seeks / transport commands back.

Security: every request must carry the random per-session token from the
URL (``?t=...`` or the ``X-Token`` header) and a localhost ``Host`` header,
so other web pages cannot drive playback or read the PDF (CSRF / DNS
rebinding).
"""
import json
import os
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

CONTROL_ACTIONS = {"toggle", "next", "prev", "back", "forward"}


def _resource(rel_path):
    """Resolve a bundled resource in dev and PyInstaller frozen modes."""
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel_path)


class ReadAlongServer:
    """Local server exposing the PDF + sync data to the browser page."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pdf_path = None
        self._payload = {"has_data": False, "alignments": [], "chapters": []}
        self._state = {"chapter_idx": -1, "global_time": 0.0, "playing": False, "rate": 1.0}
        self._seek_cb = None
        self._control_cb = None
        self._server = None
        self._thread = None
        self._port = None
        self.token = secrets.token_urlsafe(18)

    # ------------------------------------------------------------ control
    @property
    def url(self):
        if self._port is None:
            return None
        return f"http://127.0.0.1:{self._port}/?t={self.token}"

    def start(self):
        if self._server is not None:
            return
        server = ThreadingHTTPServer(("127.0.0.1", 0), _HandlerFactory(self))
        server.daemon_threads = True
        self._server = server
        self._port = server.server_address[1]
        self._thread = threading.Thread(target=server.serve_forever, name="readalong-http", daemon=True)
        self._thread.start()

    def shutdown(self):
        server = self._server
        self._server = None
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:  # noqa: BLE001 - best effort
                pass

    def set_seek_callback(self, cb):
        """cb(global_audio_seconds, chapter_idx) - called on an HTTP thread."""
        self._seek_cb = cb

    def set_control_callback(self, cb):
        """cb(action) for transport buttons - called on an HTTP thread."""
        self._control_cb = cb

    def set_book(self, pdf_path, alignments, chapters, lang, title=""):
        rows = []
        for r in alignments or []:
            bbox = r.get("bbox")
            if isinstance(bbox, str):
                try:
                    bbox = json.loads(bbox)
                except (ValueError, TypeError):
                    bbox = None
            rows.append({
                "chapter_idx": r.get("chapter_idx", 0),
                "page_no": r.get("page_no", 0),
                "text": r.get("text", ""),
                "audio_start": r.get("audio_start", 0.0),
                "audio_end": r.get("audio_end", 0.0),
                "bbox": bbox,
            })
        chapter_list = []
        offset = 0.0
        for ch in chapters or []:
            chapter_list.append({"idx": ch["idx"], "title": ch.get("title", ""),
                                 "offset": offset, "duration": ch.get("duration", 0.0)})
            offset += ch.get("duration", 0.0)
        with self._lock:
            self._pdf_path = pdf_path if pdf_path and os.path.exists(pdf_path) else None
            self._payload = {
                "title": title,
                "lang": lang or "en",
                "has_pdf": bool(self._pdf_path),
                "has_data": bool(self._pdf_path) and bool(rows),
                "alignments": rows,
                "chapters": chapter_list,
            }
            self._state = {"chapter_idx": -1, "global_time": 0.0, "playing": False, "rate": 1.0}

    def update_state(self, chapter_idx, global_time, playing, rate):
        with self._lock:
            self._state = {
                "chapter_idx": chapter_idx,
                "global_time": round(float(global_time), 3),
                "playing": bool(playing),
                "rate": float(rate),
            }

    def _snapshot(self):
        with self._lock:
            return self._pdf_path, self._payload, dict(self._state)


class _HandlerFactory:
    def __init__(self, owner):
        self._owner = owner

    def __call__(self, *args, **kwargs):
        return _Handler(self._owner, *args, **kwargs)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, owner, *args, **kwargs):
        self._owner = owner
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *args):  # silence request logs
        return

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
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type, cache=False):
        try:
            size = os.path.getsize(path)
            f = open(path, "rb")
        except OSError:
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        with f:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
            self.end_headers()
            while True:
                chunk = f.read(1 << 16)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _authorized(self, query):
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return False
        token = self.headers.get("X-Token") or (query.get("t") or [""])[0]
        return secrets.compare_digest(token, self._owner.token)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 1 << 16:
            raise ValueError("body too large")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    # ------------------------------------------------------------ routing
    def do_GET(self):
        parts = urlsplit(self.path)
        path, query = parts.path, parse_qs(parts.query)
        if path.startswith("/pdfjs/"):  # static library files, no secrets
            name = os.path.basename(path)
            if name not in ("pdf.min.js", "pdf.worker.min.js"):
                self._send(404, "not found", "text/plain; charset=utf-8")
                return
            fp = _resource(os.path.join("assets", "readalong", "pdfjs", name))
            self._send_file(fp, "application/javascript; charset=utf-8", cache=True)
            return
        if not self._authorized(query):
            self._send(403, "forbidden", "text/plain; charset=utf-8")
            return
        owner = self._owner
        if path == "/":
            html = _resource(os.path.join("assets", "readalong", "reader.html"))
            self._send_file(html, "text/html; charset=utf-8")
        elif path == "/pdf":
            pdf_path, _, _ = owner._snapshot()
            if not pdf_path:
                self._send(404, "no pdf", "text/plain; charset=utf-8")
                return
            self._send_file(pdf_path, "application/pdf")
        elif path == "/api/data":
            self._send(200, owner._snapshot()[1])
        elif path == "/api/state":
            self._send(200, owner._snapshot()[2])
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        parts = urlsplit(self.path)
        if not self._authorized(parse_qs(parts.query)):
            self._send(403, {"ok": False})
            return
        owner = self._owner
        try:
            data = self._read_json()
        except (ValueError, TypeError):
            self._send(400, {"ok": False, "error": "bad json"})
            return
        if parts.path == "/api/seek":
            try:
                audio_start = float(data.get("audio_start", -1.0))
                raw_idx = data.get("chapter_idx")
                chapter_idx = int(raw_idx) if raw_idx is not None else None
            except (ValueError, TypeError):
                self._send(400, {"ok": False, "error": "bad audio_start"})
                return
            if owner._seek_cb is not None and audio_start >= 0.0:
                try:
                    owner._seek_cb(audio_start, chapter_idx)
                except Exception:  # noqa: BLE001 - never kill the worker
                    pass
            self._send(200, {"ok": True})
        elif parts.path == "/api/control":
            action = str(data.get("action", ""))
            if action not in CONTROL_ACTIONS:
                self._send(400, {"ok": False, "error": "bad action"})
                return
            if owner._control_cb is not None:
                try:
                    owner._control_cb(action)
                except Exception:  # noqa: BLE001
                    pass
            self._send(200, {"ok": True})
        else:
            self._send(404, {"ok": False})
