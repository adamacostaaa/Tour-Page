#!/usr/bin/env python3
"""
Shared photo-store backend for the Tour Page site.

Serves the static site (index.html, ds-styles.css, images/) and a small
JSON/multipart API under /api/photos so every visitor's phone reads and
writes the SAME gallery, stored on disk here (not per-device like the
old IndexedDB version).

Run:  python3 server.py [port]
Data lives in ./server_data/ (created automatically) — photos.db (SQLite
metadata) and photos/ (the actual uploaded image files).

Pure standard library. No pip installs needed.
"""
import http.server
import json
import mimetypes
import os
import re
import socketserver
import sqlite3
import sys
import time
import uuid

SITE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SITE_DIR, "server_data")
PHOTOS_DIR = os.path.join(DATA_DIR, "photos")
DB_PATH = os.path.join(DATA_DIR, "photos.db")
TTL_MS = 24 * 3600 * 1000
# Keeps peak memory (body + multipart-split copies) well under Render's
# free-tier 512MB while still allowing a short phone video clip or two.
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

mimetypes.add_type("image/avif", ".avif")
mimetypes.add_type("image/heic", ".heic")
mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("video/quicktime", ".mov")
mimetypes.add_type("video/webm", ".webm")
mimetypes.add_type("video/x-m4v", ".m4v")


def media_kind(name):
    ctype = mimetypes.guess_type(name)[0] or ""
    return "video" if ctype.startswith("video/") else "photo"


def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS photos (
            id TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            original_name TEXT NOT NULL,
            added INTEGER NOT NULL,
            keep INTEGER NOT NULL,
            posted_by TEXT NOT NULL
        )"""
    )
    return conn


def purge_expired():
    conn = db()
    now = int(time.time() * 1000)
    rows = conn.execute(
        "SELECT id, filename FROM photos WHERE keep = 0 AND (? - added) > ?",
        (now, TTL_MS),
    ).fetchall()
    for pid, filename in rows:
        path = os.path.join(PHOTOS_DIR, filename)
        if os.path.exists(path):
            os.remove(path)
        conn.execute("DELETE FROM photos WHERE id = ?", (pid,))
    conn.commit()
    conn.close()


def list_photos():
    purge_expired()
    conn = db()
    rows = conn.execute(
        "SELECT id, original_name, added, keep, posted_by FROM photos ORDER BY added DESC"
    ).fetchall()
    conn.close()
    return [
        {
            "id": r[0],
            "name": r[1],
            "added": r[2],
            "keep": bool(r[3]),
            "postedBy": r[4],
            "url": "/api/photos/%s/image" % r[0],
            "kind": media_kind(r[1]),
        }
        for r in rows
    ]


def parse_multipart(body, boundary):
    fields, files = {}, []
    marker = b"--" + boundary
    for part in body.split(marker):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if b"\r\n\r\n" not in part:
            continue
        header_blob, _, data = part.partition(b"\r\n\r\n")
        if data.endswith(b"\r\n"):
            data = data[:-2]
        headers = {}
        for line in header_blob.split(b"\r\n"):
            if b":" in line:
                k, v = line.split(b":", 1)
                headers[k.strip().lower()] = v.strip()
        disp = headers.get(b"content-disposition", b"").decode("utf-8", "replace")
        name = None
        filename = None
        for piece in disp.split(";"):
            piece = piece.strip()
            if piece.startswith("name="):
                name = piece[5:].strip('"')
            elif piece.startswith("filename="):
                filename = piece[9:].strip('"')
        if filename is not None:
            files.append({"name": name, "filename": filename, "data": data})
        elif name is not None:
            fields[name] = data.decode("utf-8", "replace")
    return fields, files


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=SITE_DIR, **kwargs)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send_json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/photos":
            return self.send_json(200, list_photos())
        m = re.match(r"^/api/photos/([a-f0-9]+)/image$", self.path)
        if m:
            return self.serve_image(m.group(1))
        return super().do_GET()

    def serve_image(self, pid):
        conn = db()
        row = conn.execute(
            "SELECT filename, original_name FROM photos WHERE id = ?", (pid,)
        ).fetchone()
        conn.close()
        if not row:
            return self.send_json(404, {"error": "not found"})
        filename, original_name = row
        path = os.path.join(PHOTOS_DIR, filename)
        if not os.path.exists(path):
            return self.send_json(404, {"error": "file missing"})
        ctype = mimetypes.guess_type(original_name)[0] or "application/octet-stream"
        size = os.path.getsize(path)

        # Video elements rely on Range requests to seek/scrub smoothly
        # (and some mobile browsers won't play at all without it).
        range_header = self.headers.get("Range")
        m = re.match(r"bytes=(\d*)-(\d*)", range_header or "")
        if range_header and m and (m.group(1) or m.group(2)):
            start = int(m.group(1)) if m.group(1) else 0
            end = int(m.group(2)) if m.group(2) else size - 1
            end = min(end, size - 1)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            length = end - start + 1
        else:
            self.send_response(200)
            start, length = 0, size

        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)

    def do_POST(self):
        if self.path != "/api/photos":
            return self.send_json(404, {"error": "not found"})
        ctype = self.headers.get("Content-Type", "")
        m = re.search(r"boundary=([^;]+)", ctype)
        if "multipart/form-data" not in ctype or not m:
            return self.send_json(400, {"error": "expected multipart/form-data"})
        boundary = m.group(1).strip('"').encode("utf-8")
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_UPLOAD_BYTES:
            # Drain the body so the connection can be reused/closed cleanly
            # instead of leaving unread bytes confusing the next request.
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            return self.send_json(413, {"error": "upload too large (max 100MB per batch)"})
        body = self.rfile.read(length)
        fields, files = parse_multipart(body, boundary)
        if not files:
            return self.send_json(400, {"error": "no files"})
        keep = 1 if fields.get("keep") == "true" else 0
        posted_by = "adam" if fields.get("postedBy") == "adam" else "guest"
        os.makedirs(PHOTOS_DIR, exist_ok=True)
        conn = db()
        added_ids = []
        now = int(time.time() * 1000)
        for f in files:
            original_name = os.path.basename(f["filename"] or "photo.jpg")
            ext = os.path.splitext(original_name)[1] or ".jpg"
            pid = uuid.uuid4().hex
            stored_name = pid + ext
            with open(os.path.join(PHOTOS_DIR, stored_name), "wb") as out:
                out.write(f["data"])
            conn.execute(
                "INSERT INTO photos (id, filename, original_name, added, keep, posted_by) VALUES (?, ?, ?, ?, ?, ?)",
                (pid, stored_name, original_name, now, keep, posted_by),
            )
            added_ids.append(pid)
        conn.commit()
        conn.close()
        return self.send_json(201, {"ok": True, "added": added_ids})

    def do_DELETE(self):
        m = re.match(r"^/api/photos/([a-f0-9]+)$", self.path)
        if not m:
            return self.send_json(404, {"error": "not found"})
        pid = m.group(1)
        conn = db()
        row = conn.execute("SELECT filename FROM photos WHERE id = ?", (pid,)).fetchone()
        if not row:
            conn.close()
            return self.send_json(404, {"error": "not found"})
        path = os.path.join(PHOTOS_DIR, row[0])
        if os.path.exists(path):
            os.remove(path)
        conn.execute("DELETE FROM photos WHERE id = ?", (pid,))
        conn.commit()
        conn.close()
        return self.send_json(200, {"ok": True})


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    # Render (and most PaaS hosts) inject the port to bind via $PORT and
    # terminate HTTPS themselves in front of us. Local LAN testing passes
    # a port as argv[1] instead and optionally self-signs its own TLS below.
    env_port = os.environ.get("PORT")
    port = int(env_port) if env_port else (int(sys.argv[1]) if len(sys.argv) > 1 else 8080)
    server = ThreadingServer(("0.0.0.0", port), Handler)

    cert = os.path.join(SITE_DIR, "cert.pem")
    key = os.path.join(SITE_DIR, "key.pem")
    scheme = "http"
    if os.path.exists(cert) and os.path.exists(key):
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        scheme = "https"

    print("Serving Tour Page + shared photo API on %s://0.0.0.0:%d" % (scheme, port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
