"""Image Optimizer — local app.

Run:  python app.py      (then use the page that opens in your browser)

A tiny web server bound to 127.0.0.1 only: the browser page sends the images you
pick to this local process, which compresses them with Pillow and writes the
results to ~/Downloads/OptimizedImages_<date-time>/ (wherever this project folder
is kept). Nothing leaves your computer, and the original
files are never touched (the app never even learns where they are on disk).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
import zipfile
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, quote, urlparse

import optimizer

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"


def _downloads_dir() -> Path:
    """The user's Downloads folder (macOS / Windows / Linux), independent of where the app lives."""
    if os.environ.get("IMAGE_OPTIMIZER_OUTPUT"):          # optional override
        return Path(os.environ["IMAGE_OPTIMIZER_OUTPUT"]).expanduser()
    home = Path.home()
    if os.name == "nt":
        try:  # the real Downloads location, even if the user moved it
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") as k:
                return Path(winreg.QueryValueEx(k, "{374DE290-123F-4565-9164-39C4925E467B}")[0])
        except OSError:
            pass
    elif sys.platform.startswith("linux"):
        try:
            d = subprocess.run(["xdg-user-dir", "DOWNLOAD"], capture_output=True, text=True, timeout=3).stdout.strip()
            if d and d != str(home):
                return Path(d)
        except (OSError, subprocess.SubprocessError):
            pass
    downloads = home / "Downloads"
    return downloads if downloads.is_dir() else home


OUTPUT_ROOT = _downloads_dir()
HOST = "127.0.0.1"
PORTS = range(8765, 8785)
MAX_UPLOAD = 500 * 1024 * 1024
WORKERS = max(1, min(4, (os.cpu_count() or 2) // 2))

_pool = ThreadPoolExecutor(max_workers=WORKERS)   # Pillow releases the GIL while coding images
_name_lock = threading.Lock()
BATCH_PREFIX = "OptimizedImages_"
BATCH_RE = re.compile(r"^OptimizedImages_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(-\d+)?$")


def safe_relpath(raw: str) -> PurePosixPath:
    """Keep a folder structure from the browser, but never allow escaping the batch folder."""
    parts = [p for p in re.split(r"[\\/]+", raw) if p not in ("", ".", "..")]
    parts = [re.sub(r'[<>:"|?*\x00-\x1f]', "_", p).strip() or "_" for p in parts]
    if not parts:
        parts = ["image"]
    return PurePosixPath(*parts)


def batch_dir(batch: str) -> Path:
    if not BATCH_RE.match(batch or ""):
        raise ValueError("bad batch id")
    d = (OUTPUT_ROOT / batch).resolve()
    if d.parent != OUTPUT_ROOT.resolve() or not d.is_dir():
        raise ValueError("unknown batch")
    return d


def reserve_path(folder: Path, rel: PurePosixPath, ext: str) -> Path:
    """Unique output path (photo.webp, photo-2.webp, ...) — created atomically."""
    target_dir = folder.joinpath(*rel.parent.parts)
    target_dir.mkdir(parents=True, exist_ok=True)
    stem = rel.stem or "image"
    with _name_lock:
        n = 1
        while True:
            p = target_dir / (stem + ("" if n == 1 else f"-{n}") + ext)
            if not p.exists():
                p.touch()
                return p
            n += 1


def open_folder(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path)])


class Handler(BaseHTTPRequestHandler):
    server_version = "ImageOptimizer"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    # --- helpers -------------------------------------------------------------
    def _host_ok(self) -> bool:
        # Blocks DNS-rebinding: only accept requests addressed to this local server.
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _error(self, code, msg):
        self._json({"error": msg}, code)

    def _send_file(self, path: Path, ctype: str, download_name: str, inline: bool = False):
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        disposition = "inline" if inline else "attachment"
        self.send_header("Content-Disposition", disposition + "; filename*=UTF-8''" + quote(download_name))
        self.end_headers()
        with path.open("rb") as f:
            shutil.copyfileobj(f, self.wfile, 1024 * 1024)

    # --- routes --------------------------------------------------------------
    def do_GET(self):
        if not self._host_ok():
            return self._error(403, "forbidden")
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8",
                                  {"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                              "script-src 'self' 'unsafe-inline'; img-src 'self' data:"})
            if url.path.startswith("/static/"):  # brand assets (logo, favicon)
                p = (STATIC / url.path[len("/static/"):]).resolve()
                ctype = {".webp": "image/webp", ".png": "image/png", ".svg": "image/svg+xml",
                         ".ico": "image/x-icon"}.get(p.suffix.lower())
                if STATIC.resolve() not in p.parents or not p.is_file() or not ctype:
                    return self._error(404, "not found")
                return self._send(200, p.read_bytes(), ctype, {"Cache-Control": "max-age=86400"})
            if url.path == "/api/info":
                return self._json({"avif": optimizer.AVIF_AVAILABLE, "webp": optimizer.WEBP_AVAILABLE,
                                   "workers": WORKERS, "output": str(OUTPUT_ROOT)})
            if url.path == "/api/file":
                d = batch_dir(q.get("batch", ""))
                p = (d / q.get("path", "")).resolve()
                if d not in p.parents or not p.is_file():
                    return self._error(404, "not found")
                ctype = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                         ".webp": "image/webp", ".avif": "image/avif"}.get(p.suffix.lower(), "application/octet-stream")
                return self._send_file(p, ctype, p.name, inline=q.get("inline") == "1")
            if url.path == "/api/zip":
                d = batch_dir(q.get("batch", ""))
                with tempfile.TemporaryDirectory() as tmp:
                    zpath = Path(tmp) / "optimized-images.zip"
                    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
                        for f in sorted(d.rglob("*")):
                            if f.is_file() and f.stat().st_size > 0:
                                z.write(f, f.relative_to(d).as_posix())
                    return self._send_file(zpath, "application/zip", f"{d.name}.zip")
            return self._error(404, "not found")
        except ValueError as exc:
            return self._error(400, str(exc))
        except (BrokenPipeError, ConnectionResetError):
            return None

    def do_POST(self):
        # The custom header forces a CORS preflight, which this server never approves,
        # so other websites open in the browser cannot call these endpoints.
        if not self._host_ok() or self.headers.get("X-Image-Optimizer") != "1":
            return self._error(403, "forbidden")
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/api/batch":
                OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
                with _name_lock:
                    base = BATCH_PREFIX + time.strftime("%Y-%m-%d_%H-%M-%S")
                    name, n = base, 2
                    while (OUTPUT_ROOT / name).exists():
                        name, n = f"{base}-{n}", n + 1
                    (OUTPUT_ROOT / name).mkdir()
                return self._json({"batch": name, "folder": str(OUTPUT_ROOT / name)})
            if url.path == "/api/open":
                open_folder(batch_dir(q.get("batch", "")) if q.get("batch") else OUTPUT_ROOT)
                return self._json({"ok": True})
            if url.path == "/api/optimize":
                return self._optimize(q)
            return self._error(404, "not found")
        except ValueError as exc:
            return self._error(400, str(exc))
        except (BrokenPipeError, ConnectionResetError):
            return None

    def _optimize(self, q):
        d = batch_dir(q.get("batch", ""))
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_UPLOAD:
            return self._error(413, "file too large or empty")
        data = self.rfile.read(length)
        rel = safe_relpath(q.get("path", "image"))
        convert = q.get("convert") or None
        if convert not in (None, "webp", "avif"):
            return self._error(400, "bad convert option")
        try:
            mb = float(q.get("max_mb") or 0)
        except ValueError:
            mb = 0
        limit = int(mb * 1_000_000) if mb > 0 else None

        if rel.suffix.lower() not in optimizer.INPUT_EXTENSIONS:
            return self._json({"status": "error", "note": "Unsupported file type", "origSize": length})
        t0 = time.time()
        try:
            r = _pool.submit(optimizer.optimize, data, convert, limit).result()
        except optimizer.UnsupportedImage as exc:
            return self._json({"status": "error", "note": str(exc), "origSize": length})
        except Exception as exc:  # noqa: BLE001
            return self._json({"status": "error", "note": f"Could not process image: {exc}", "origSize": length})

        out = reserve_path(d, rel, optimizer.output_extension(rel.name, convert))
        out.write_bytes(r.data)
        return self._json({
            "status": r.status, "note": r.note, "detail": r.detail,
            "format": optimizer.LABEL[r.fmt],
            "origSize": length, "newSize": len(r.data),
            "width": r.width, "height": r.height, "origWidth": r.orig_width, "origHeight": r.orig_height,
            "file": out.relative_to(d).as_posix(), "seconds": round(time.time() - t0, 2),
        })


def main():
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    server = None
    for port in PORTS:
        try:
            server = ThreadingHTTPServer((HOST, port), Handler)
            break
        except OSError:
            continue
    if server is None:
        sys.exit("No free port between 8765 and 8784.")
    server.daemon_threads = True
    url = f"http://{HOST}:{server.server_address[1]}/"
    print(f"Image Optimizer is running at {url}")
    print(f"Optimized images are saved in: {OUTPUT_ROOT}")
    print(f"AVIF support: {'yes' if optimizer.AVIF_AVAILABLE else 'NO (upgrade Pillow)'}")
    print("Press Ctrl+C to stop.")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
