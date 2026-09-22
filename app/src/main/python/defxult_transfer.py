#!/usr/bin/env python3
"""
defxult wifi transfer — v42
Native <video> + transcode watchdog. LRU-capped transcode cache.
Floating glass video controls (no background panel, compact).
Click-to-preview media. Playlist follows sort/filter.
Selection + batch cards in sidebar.
"""

import os
import io
import re
import base64
import json
import time
import html
import socket
import secrets
import shutil
import hashlib
import zipfile
import threading
import subprocess
import urllib.parse
import http.server
import socketserver
from email.parser import BytesParser
from email.policy import default

DEFAULT_PORT = 8080

# All paths are env-driven so the same file works under Termux (defaults)
# and inside the Android APK (paths injected by Chaquopy from ServerController).
ROOT_DIR = os.path.abspath(os.environ.get("DEFXULT_ROOT") or "/sdcard")
BASE_HOME = os.environ.get("DEFXULT_HOME") or os.path.expanduser("~")
NODE_MODULES = os.environ.get("DEFXULT_NODE_MODULES") or os.path.join(BASE_HOME, "node_modules")

CONFIG_FILE = os.path.join(ROOT_DIR, ".defxult_transfer_config.json")
WALLPAPER_BASE = ".defxult_wallpaper"
TOKEN_FILE = os.path.join(BASE_HOME, ".defxult_token")
THUMB_CACHE_DIR = os.path.join(BASE_HOME, ".cache", "defxult_thumbs")
UPLOAD_CACHE_DIR = os.path.join(BASE_HOME, ".cache", "defxult_uploads")
TRANSCODE_CACHE_DIR = os.path.join(BASE_HOME, ".cache", "defxult_transcode")
TRANSCODE_CACHE_MAX_BYTES = 2 * 1024 * 1024 * 1024
CHUNK = 1048576
UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024
UPLOAD_MAX_AGE = 24 * 3600
MAX_STAGGER_INDEX = 15
DOWNLOAD_BATCH = 10

# Where drag-and-drop / browse uploads land. Empty = current folder.
# Set via the Android app's Settings screen. The folder-picker path in the
# web UI always uses the folder the user picked there, ignoring this.
UPLOAD_DEST = (os.environ.get("DEFXULT_UPLOAD_DEST") or "").strip("/")
if UPLOAD_DEST and not os.path.isdir(os.path.join(ROOT_DIR, UPLOAD_DEST)):
    UPLOAD_DEST = ""

TUS_UPLOADS = {}
TUS_LOCK = threading.Lock()

WEB_TYPES = {
    ".js": "application/javascript", ".mjs": "application/javascript",
    ".css": "text/css", ".svg": "image/svg+xml",
    ".woff": "font/woff", ".woff2": "font/woff2",
    ".ttf": "font/ttf", ".otf": "font/otf",
    ".json": "application/json", ".map": "application/json",
    ".wasm": "application/wasm",
}

try:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 20_000_000
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import qrcode
    HAS_QR = True
except ImportError:
    HAS_QR = False

FFMPEG = shutil.which("ffmpeg")
HAS_SENDFILE = hasattr(os, "sendfile")

VIDEO_EXTS = (".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v", ".3gp")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
AUDIO_EXTS = (".mp3", ".wav", ".ogg", ".m4a", ".flac", ".opus", ".aac")
ARCHIVE_EXTS = (".zip", ".tar", ".gz", ".7z", ".rar")


def load_or_create_token():
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r") as f:
                t = f.read().strip()
                if t and t.isdigit() and len(t) == 6:
                    return t
        except OSError:
            pass
    while True:
        t = str(secrets.randbelow(900000) + 100000)
        if len(set(t)) > 1:
            break
    try:
        with open(TOKEN_FILE, "w") as f:
            f.write(t)
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    return t


AUTH_TOKEN = load_or_create_token()


def cleanup_old_uploads():
    if not os.path.isdir(UPLOAD_CACHE_DIR):
        return
    now = time.time()
    for name in os.listdir(UPLOAD_CACHE_DIR):
        full = os.path.join(UPLOAD_CACHE_DIR, name)
        try:
            if now - os.path.getmtime(full) > UPLOAD_MAX_AGE:
                if os.path.isdir(full):
                    shutil.rmtree(full, ignore_errors=True)
                else:
                    os.remove(full)
        except OSError:
            pass


def cleanup_transcode_cache(max_bytes=TRANSCODE_CACHE_MAX_BYTES):
    if not os.path.isdir(TRANSCODE_CACHE_DIR):
        return
    entries = []
    total = 0
    for name in os.listdir(TRANSCODE_CACHE_DIR):
        if name.endswith(".part"):
            continue
        full = os.path.join(TRANSCODE_CACHE_DIR, name)
        try:
            st = os.stat(full)
        except OSError:
            continue
        if not os.path.isfile(full):
            continue
        entries.append((st.st_atime, st.st_size, full))
        total += st.st_size
    if total <= max_bytes:
        return
    entries.sort()
    for _atime, size, full in entries:
        if total <= max_bytes:
            break
        try:
            os.remove(full)
            total -= size
            print(f"[transcode-cache] evicted {os.path.basename(full)}")
        except OSError:
            pass


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {"wallpaper": "", "music": ""}


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


def find_wallpaper_file():
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"):
        p = os.path.join(ROOT_DIR, WALLPAPER_BASE + ext)
        if os.path.isfile(p):
            return p
    return None


def find_music_file():
    cfg = load_config()
    mp = cfg.get("music", "")
    if mp:
        full = os.path.join(ROOT_DIR, mp.lstrip("/"))
        if os.path.isfile(full):
            return full
    return None


def rescan_media():
    """
    Re-scan the root for .defxult_music.* and update the config's music key.
    Called from Android after the user picks a new music file in Settings, so
    the running web UI picks it up on next page load without a restart.
    Wallpaper needs no config update — find_wallpaper_file() scans the fs.
    """
    found = None
    for ext in (".mp3", ".ogg", ".m4a", ".wav", ".flac", ".opus", ".aac"):
        p = os.path.join(ROOT_DIR, ".defxult_music" + ext)
        if os.path.isfile(p):
            found = "/" + os.path.basename(p)
            break
    cfg = load_config()
    cfg["music"] = found or ""
    save_config(cfg)
    return True


def format_size(size):
    size = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def get_local_ip():
    forced = (os.environ.get("DEFXULT_FORCED_IP") or "").strip()
    if forced:
        return forced
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def print_qr_code(url):
    if not HAS_QR:
        return
    qr = qrcode.QRCode(box_size=1, border=1)
    qr.add_data(url)
    qr.print_ascii(invert=True)


def qr_png_bytes(data, box_size=8, border=2):
    """Return QR code PNG bytes for `data`, or None if qrcode isn't available."""
    if not HAS_QR:
        return None
    try:
        qr = qrcode.QRCode(box_size=box_size, border=border)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()
    except Exception as e:
        print(f"[qr] failed: {e}")
        return None


def esc(s):
    return html.escape(str(s), quote=True)


def safe_upload_id(s):
    return re.sub(r"[^A-Za-z0-9_\-]", "", s or "")[:64]


def extract_video_thumbnail(src_path, dst_jpg, size=128):
    if not FFMPEG:
        return False
    try:
        subprocess.run(
            [FFMPEG, "-y", "-loglevel", "error", "-ss", "00:00:01",
             "-i", src_path, "-frames:v", "1",
             "-vf", f"scale={size}:-2:force_original_aspect_ratio=decrease",
             "-q:v", "4", dst_jpg],
            capture_output=True, timeout=15, check=True,
        )
        return os.path.isfile(dst_jpg) and os.path.getsize(dst_jpg) > 0
    except (subprocess.SubprocessError, OSError):
        return False


def extract_album_art(src_path, dst_jpg, size=128):
    if not FFMPEG:
        return False
    try:
        subprocess.run(
            [FFMPEG, "-y", "-loglevel", "error", "-i", src_path,
             "-an", "-c:v", "mjpeg", "-frames:v", "1",
             "-vf", f"scale={size}:-2:force_original_aspect_ratio=decrease",
             "-q:v", "4", dst_jpg],
            capture_output=True, timeout=10, check=True,
        )
        return os.path.isfile(dst_jpg) and os.path.getsize(dst_jpg) > 0
    except (subprocess.SubprocessError, OSError):
        return False


def icon_for(name, is_dir):
    if is_dir: return "📁"
    ext = os.path.splitext(name)[1].lower()
    if ext in VIDEO_EXTS: return "🎬"
    if ext in IMAGE_EXTS: return "🖼️"
    if ext in AUDIO_EXTS: return "🎵"
    if ext in ARCHIVE_EXTS: return "🗜️"
    if ext == ".pdf": return "📕"
    if ext in (".txt", ".md", ".log"): return "📝"
    return "📄"


class ModernHandler(http.server.SimpleHTTPRequestHandler):

    server_version = "defxult/42.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def _check_auth(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        supplied = query.get("t", [""])[0]

        if supplied and secrets.compare_digest(supplied, AUTH_TOKEN):
            clean = {k: v for k, v in query.items() if k != "t"}
            new_q = urllib.parse.urlencode(
                [(k, x) for k, vs in clean.items() for x in vs]
            )
            target = parsed.path + ("?" + new_q if new_q else "")
            self.send_response(303)
            self.send_header("Location", target or "/")
            self.send_header(
                "Set-Cookie",
                f"defxult_auth={AUTH_TOKEN}; Path=/; HttpOnly; "
                "SameSite=Lax; Max-Age=31536000",
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True

        cookie_header = self.headers.get("Cookie", "")
        for part in cookie_header.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "defxult_auth":
                if secrets.compare_digest(v, AUTH_TOKEN):
                    return False

        body = (
            b"401 Unauthorized\n"
            b"Append ?t=<PIN> to the URL (see terminal) to authenticate.\n"
        )
        self.send_response(401)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    def translate_path(self, path):
        path = urllib.parse.unquote(path.split("?", 1)[0].split("#", 1)[0])
        relative = path.lstrip("/")
        full = os.path.abspath(os.path.join(ROOT_DIR, relative))
        if full != ROOT_DIR and not full.startswith(ROOT_DIR + os.sep):
            return ROOT_DIR
        return full

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_err(self, status, msg):
        self._json({"error": msg}, status=status)

    def do_GET(self):
        if self._check_auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        path = parsed.path

        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path.startswith("/static/"):
            return self._serve_static(path[len("/static/"):])
        if path == "/glassify.css":
            return self._serve_glassify()
        if path == "/api/list":
            return self._api_list(query)
        if path == "/api/checksum":
            return self._api_checksum(query)
        if path == "/api/folders":
            return self._api_folders(query)
        if path == "/api/search":
            return self._api_search(query)
        if path == "/api/clipboard":
            return self._api_clipboard()
        if path == "/api/upload/status":
            return self._api_upload_status(query)
        if path == "/api/transcode":
            return self._api_transcode(query)
        if path.startswith("/thumbnail/"):
            return self._serve_thumbnail(path[len("/thumbnail"):])
        if path == "/download_zip":
            return self._download_zip(query)

        full = self.translate_path(self.path)
        if os.path.isdir(full):
            return self._render_directory(full)
        return super().do_GET()

    def _serve_static(self, rel_path):
        parts = [p for p in rel_path.lstrip("/").split("/")
                 if p and p not in (".", "..")]
        safe = "/".join(parts)
        if not safe:
            return self.send_error(404)
        full = os.path.normpath(os.path.join(NODE_MODULES, safe))
        base = os.path.normpath(NODE_MODULES)
        if not full.startswith(base + os.sep):
            return self.send_error(404)
        if not os.path.isfile(full):
            return self.send_error(404, "Static file not found")
        ext = os.path.splitext(full)[1].lower()
        ctype = WEB_TYPES.get(ext) or self.guess_type(full)
        try:
            with open(full, "rb") as f:
                body = f.read()
        except OSError:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def _serve_glassify(self):
        for css_path in (
            os.path.join(BASE_HOME, "node_modules", "glassify", "styles.css"),
            os.path.join(BASE_HOME, "node_modules", "glassify", "src", "container.css"),
        ):
            if os.path.exists(css_path):
                with open(css_path, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/css; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        self.send_response(200)
        self.send_header("Content-Type", "text/css; charset=utf-8")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _api_transcode(self, query):
        rel = query.get("path", [""])[0]
        if not rel:
            return self.send_error(400, "Missing path")
        full = self.translate_path(rel)
        if not os.path.isfile(full):
            return self.send_error(404, "Not found")
        if not FFMPEG:
            return self.send_error(503, "ffmpeg not installed")

        try:
            st = os.stat(full)
            key = hashlib.sha1(
                f"{full}|{st.st_mtime_ns}|{st.st_size}".encode()
            ).hexdigest()
        except OSError:
            return self.send_error(404)

        cache_file = os.path.join(TRANSCODE_CACHE_DIR, key + ".mp4")

        if os.path.isfile(cache_file):
            try:
                try:
                    os.utime(cache_file, None)
                except OSError:
                    pass
                f = open(cache_file, "rb")
            except OSError:
                f = None
            if f is not None:
                try:
                    fs = os.fstat(f.fileno())
                    size = fs.st_size
                    range_header = self.headers.get("Range", "")
                    start, end = 0, size - 1
                    is_range = False
                    if range_header.startswith("bytes="):
                        try:
                            spec = range_header[6:].split(",")[0].strip()
                            s, _, e = spec.partition("-")
                            if s:
                                start = int(s)
                                end = int(e) if e else size - 1
                            else:
                                start = max(0, size - int(e))
                                end = size - 1
                            if start > end or start >= size:
                                raise ValueError
                            is_range = True
                        except (ValueError, TypeError):
                            f.close()
                            return self.send_error(416, "Invalid range")
                    length = end - start + 1
                    self._range_remaining = length
                    self.send_response(206 if is_range else 200)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Content-Length", str(length))
                    self.send_header("Accept-Ranges", "bytes")
                    if is_range:
                        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                    self.send_header("Cache-Control", "public, max-age=86400")
                    self.end_headers()
                    if start > 0:
                        f.seek(start)
                    self.copyfile(f, self.wfile)
                    return
                finally:
                    try:
                        f.close()
                    except OSError:
                        pass

        os.makedirs(TRANSCODE_CACHE_DIR, exist_ok=True)
        tmp_cache = cache_file + ".part"

        ext = os.path.splitext(full)[1].lower()
        is_video = ext in VIDEO_EXTS

        if is_video:
            cmd = [
                FFMPEG, "-loglevel", "error", "-y",
                "-i", full,
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-tune", "zerolatency",
                "-pix_fmt", "yuv420p",
                "-profile:v", "baseline",
                "-level", "3.1",
                "-c:a", "aac", "-b:a", "128k", "-ac", "2",
                "-movflags", "frag_keyframe+empty_moov+default_base_moof",
                "-f", "mp4",
                "pipe:1",
            ]
            ctype = "video/mp4"
        else:
            cmd = [
                FFMPEG, "-loglevel", "error", "-y",
                "-i", full,
                "-vn",
                "-c:a", "aac", "-b:a", "128k", "-ac", "2",
                "-movflags", "frag_keyframe+empty_moov+default_base_moof",
                "-f", "mp4",
                "pipe:1",
            ]
            ctype = "audio/mp4"

        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0
            )
        except OSError:
            return self.send_error(500, "ffmpeg failed to start")

        print(f"[transcode] {os.path.basename(full)} -> {ctype}")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        cache_writer = None
        try:
            cache_writer = open(tmp_cache, "wb")
        except OSError:
            cache_writer = None

        cache_failed = False
        client_ok = True
        completed = False
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    completed = True
                    break
                if cache_writer:
                    try:
                        cache_writer.write(chunk)
                    except OSError:
                        try:
                            cache_writer.close()
                        except OSError:
                            pass
                        cache_writer = None
                        cache_failed = True
                if client_ok:
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        client_ok = False
        except (BrokenPipeError, ConnectionResetError):
            client_ok = False
        finally:
            if not completed:
                try:
                    proc.kill()
                except OSError:
                    pass
            if cache_writer:
                try:
                    cache_writer.close()
                except OSError:
                    pass

        if (completed and not cache_failed
                and os.path.isfile(tmp_cache) and os.path.getsize(tmp_cache) > 0):
            try:
                os.replace(tmp_cache, cache_file)
                cleanup_transcode_cache()
            except OSError:
                pass
        else:
            try:
                os.remove(tmp_cache)
            except OSError:
                pass

    def _build_dir_data(self, full_path):
        try:
            list_dir = os.listdir(full_path)
        except PermissionError:
            return None

        rel_path = os.path.relpath(full_path, ROOT_DIR)
        if rel_path == ".":
            rel_path = ""

        parent_row = ""
        if full_path != ROOT_DIR:
            parent_rel = os.path.relpath(os.path.dirname(full_path), ROOT_DIR)
            if parent_rel == ".":
                parent_url = "/"
            else:
                parent_url = "/" + urllib.parse.quote(parent_rel).replace("%2F", "/") + "/"
            parent_row = (
                '<li data-name=".." style="--i:0">'
                '<input type="checkbox" style="visibility:hidden" class="file-chk">'
                f'<a href="{esc(parent_url)}" class="file-info">'
                '<span class="file-ico">📁</span>'
                '<span class="file-name"><strong>.. (Parent Directory)</strong></span>'
                "</a></li>"
            )

        rows = []
        sorted_dir = sorted(
            list_dir,
            key=lambda s: (not os.path.isdir(os.path.join(full_path, s)), s.lower())
        )
        for idx, name in enumerate(sorted_dir):
            if name.startswith("."):
                continue
            fp = os.path.join(full_path, name)
            is_dir = os.path.isdir(fp)
            display = name + "/" if is_dir else name
            icon = icon_for(name, is_dir)
            try:
                f_size = 0 if is_dir else os.path.getsize(fp)
                f_mtime = os.path.getmtime(fp)
            except OSError:
                f_size = 0
                f_mtime = 0
            size_str = "" if is_dir else format_size(f_size)

            link = "/"
            if rel_path:
                link += urllib.parse.quote(rel_path).replace("%2F", "/") + "/"
            link += urllib.parse.quote(name)
            if is_dir:
                link += "/"

            ext = os.path.splitext(name)[1].lower().lstrip(".")
            ftype = "dir" if is_dir else ext

            previewable = (
                ext in [e.lstrip(".") for e in IMAGE_EXTS]
                or (ext in [e.lstrip(".") for e in VIDEO_EXTS] and FFMPEG)
                or (ext in [e.lstrip(".") for e in AUDIO_EXTS] and FFMPEG)
            )
            if previewable:
                thumb = (
                    f'<img src="/thumbnail{esc(link)}" class="file-thumb" '
                    'alt="" loading="lazy" '
                    f'data-fallback="{esc(icon)}">'
                )
            else:
                thumb = f'<span class="file-ico">{icon}</span>'

            capped = min(idx + 1, MAX_STAGGER_INDEX)
            rows.append(
                f'<li data-name="{esc(name.lower())}" data-size="{f_size}" '
                f'data-mtime="{f_mtime:.0f}" data-type="{esc(ftype)}" '
                f'style="--i:{capped}">'
                f'<input type="checkbox" class="file-chk" '
                f'data-path="{esc(link)}" data-name="{esc(name)}">'
                f'<a href="{esc(link)}" class="file-info">'
                f'{thumb}<span class="file-name">{esc(display)}</span>'
                f'<span class="file-meta">{esc(size_str)}</span>'
                "</a></li>"
            )
        file_rows = "\n".join(rows) if rows else (
            '<li class="empty-row" style="--i:1">'
            '<span style="color:var(--text-muted);width:100%;text-align:center;'
            'padding:24px;">This folder is empty</span></li>'
        )

        try:
            total_b, used_b, _free_b = shutil.disk_usage(ROOT_DIR)
        except OSError:
            total_b = used_b = 1
        storage_pct = (used_b / total_b) * 100 if total_b else 0
        storage_meter = f"Used {format_size(used_b)} of {format_size(total_b)}"

        wall_path = find_wallpaper_file()
        bg_override = ""
        if wall_path:
            try:
                st = os.stat(wall_path)
                rel_wall = os.path.relpath(wall_path, ROOT_DIR).replace(os.sep, "/")
                bg_override = f'url("/{rel_wall}?v={st.st_mtime_ns}"),'
            except OSError:
                bg_override = ""

        music_path = find_music_file()
        music_url = None
        if music_path:
            try:
                st = os.stat(music_path)
                rel_music = os.path.relpath(music_path, ROOT_DIR).replace(os.sep, "/")
                music_url = f"/{rel_music}?v={st.st_mtime_ns}"
            except OSError:
                music_url = None

        return {
            "rel_path": rel_path,
            "parent_row": parent_row,
            "file_rows": file_rows,
            "storage_meter": storage_meter,
            "storage_pct": storage_pct,
            "bg_override": bg_override,
            "music_url": music_url,
        }

    def _api_list(self, query):
        req_dir = query.get("dir", [""])[0]
        full_path = self.translate_path(req_dir) if req_dir else ROOT_DIR
        if not os.path.isdir(full_path):
            return self._json_err(404, "Not a directory")
        data = self._build_dir_data(full_path)
        if data is None:
            return self._json_err(403, "Permission denied")
        self._json(data)

    def _api_checksum(self, query):
        req_path = query.get("path", [""])[0]
        full_path = self.translate_path(req_path)
        if not os.path.isfile(full_path):
            return self._json({"checksum": "File not found"})
        sha = hashlib.sha256()
        try:
            with open(full_path, "rb") as f:
                for block in iter(lambda: f.read(CHUNK), b""):
                    sha.update(block)
            return self._json({"checksum": sha.hexdigest()})
        except OSError as e:
            return self._json({"checksum": f"Error: {e}"})

    def _api_folders(self, query):
        req_dir = query.get("dir", [""])[0]
        full_path = self.translate_path(req_dir) if req_dir else ROOT_DIR
        if not os.path.isdir(full_path):
            full_path = ROOT_DIR
        try:
            folders = sorted(
                f for f in os.listdir(full_path)
                if os.path.isdir(os.path.join(full_path, f)) and not f.startswith(".")
            )
        except OSError:
            folders = []
        rel = os.path.relpath(full_path, ROOT_DIR)
        if rel == ".":
            rel = ""
        self._json({"current": rel, "folders": folders})

    def _api_search(self, query):
        q = query.get("q", [""])[0].strip().lower()
        scope = query.get("scope", ["here"])[0]
        cur_dir = query.get("dir", [""])[0]
        if not q:
            return self._json({"results": [], "scope": scope})

        if scope == "all":
            base = ROOT_DIR
            recursive = True
        else:
            base = self.translate_path(cur_dir) if cur_dir else ROOT_DIR
            recursive = False

        results = []
        limit = 500

        def add_entry(full, name):
            is_dir = os.path.isdir(full)
            try:
                size = 0 if is_dir else os.path.getsize(full)
                mtime = os.path.getmtime(full)
            except OSError:
                size = 0
                mtime = 0
            results.append({
                "name": name,
                "rel": os.path.relpath(full, ROOT_DIR),
                "is_dir": is_dir,
                "size": size,
                "mtime": mtime,
            })

        try:
            if recursive:
                for root, dirs, files in os.walk(base):
                    dirs[:] = [d for d in dirs if not d.startswith(".")]
                    for name in list(dirs) + list(files):
                        if name.startswith("."):
                            continue
                        if q not in name.lower():
                            continue
                        add_entry(os.path.join(root, name), name)
                        if len(results) >= limit:
                            break
                    if len(results) >= limit:
                        break
            else:
                for name in os.listdir(base):
                    if name.startswith(".") or q not in name.lower():
                        continue
                    add_entry(os.path.join(base, name), name)
                    if len(results) >= limit:
                        break
        except OSError as e:
            return self._json_err(500, str(e))

        self._json({"results": results, "scope": scope, "truncated": len(results) >= limit})

    def _api_clipboard(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _api_upload_status(self, query):
        uid = safe_upload_id(query.get("id", [""])[0])
        if not uid:
            return self._json_err(400, "Missing id")
        d = os.path.join(UPLOAD_CACHE_DIR, uid)
        received = []
        if os.path.isdir(d):
            for name in os.listdir(d):
                if name.endswith(".part"):
                    try:
                        received.append(int(name[:-5]))
                    except ValueError:
                        pass
            received.sort()
        self._json({"received": received})

    def _serve_thumbnail(self, url_path):
        img_path = self.translate_path(url_path)
        if not os.path.isfile(img_path):
            return self.send_error(404)
        try:
            st = os.stat(img_path)
            key = hashlib.sha1(
                f"{img_path}|{st.st_mtime_ns}|{st.st_size}".encode()
            ).hexdigest()
        except OSError:
            return self.send_error(404)

        cache_file = os.path.join(THUMB_CACHE_DIR, key + ".jpg")
        body = None
        if os.path.isfile(cache_file):
            try:
                with open(cache_file, "rb") as cf:
                    body = cf.read()
            except OSError:
                body = None

        if body is None:
            body = self._make_thumbnail(img_path, cache_file)

        if not body:
            return self.send_error(404)

        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def _make_thumbnail(self, src_path, cache_file):
        ext = os.path.splitext(src_path)[1].lower()
        body = None

        if ext in VIDEO_EXTS and FFMPEG:
            os.makedirs(THUMB_CACHE_DIR, exist_ok=True)
            tmp = cache_file + ".tmp.jpg"
            if extract_video_thumbnail(src_path, tmp, size=128):
                try:
                    with open(tmp, "rb") as f:
                        body = f.read()
                except OSError:
                    body = None
                finally:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass

        elif ext in AUDIO_EXTS and FFMPEG:
            os.makedirs(THUMB_CACHE_DIR, exist_ok=True)
            tmp = cache_file + ".tmp.jpg"
            if extract_album_art(src_path, tmp, size=128):
                try:
                    with open(tmp, "rb") as f:
                        body = f.read()
                except OSError:
                    body = None
                finally:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass

        elif ext in IMAGE_EXTS and HAS_PIL:
            try:
                img = Image.open(src_path)
                img.thumbnail((128, 128))
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="JPEG", quality=78, optimize=True)
                body = buf.getvalue()
            except Exception:
                body = None

        if body:
            try:
                os.makedirs(THUMB_CACHE_DIR, exist_ok=True)
                tmp = cache_file + ".tmp"
                with open(tmp, "wb") as cf:
                    cf.write(body)
                os.replace(tmp, cache_file)
            except OSError:
                pass
        return body

    def _download_zip(self, query):
        files = query.get("file", [])
        valid = [self.translate_path(f) for f in files]
        valid = [p for p in valid if os.path.exists(p)]
        if not valid:
            return self.send_error(404, "No valid files")

        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header(
            "Content-Disposition", 'attachment; filename="defxult_transfer.zip"'
        )
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        try:
            with zipfile.ZipFile(self.wfile, "w", zipfile.ZIP_STORED) as z:
                for vp in valid:
                    if os.path.isdir(vp):
                        parent = os.path.dirname(vp)
                        for root, _dirs, names in os.walk(vp):
                            for name in names:
                                fp = os.path.join(root, name)
                                arc = os.path.relpath(fp, parent)
                                try:
                                    z.write(fp, arc)
                                except OSError:
                                    continue
                    else:
                        try:
                            z.write(vp, os.path.basename(vp))
                        except OSError:
                            continue
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _render_directory(self, full_path):
        data = self._build_dir_data(full_path)
        if data is None:
            return self.send_error(403, "Permission denied")

        repl = {
            "__BG_OVERRIDE__": data["bg_override"],
            "__REL_PATH__": esc(data["rel_path"]),
            "__STORAGE_METER__": esc(data["storage_meter"]),
            "__STORAGE_PCT__": f"{data['storage_pct']:.1f}",
            "__PARENT_ROW__": data["parent_row"],
            "__FILE_ROWS__": data["file_rows"],
            "__CHUNK_SIZE__": str(UPLOAD_CHUNK_SIZE),
            "__HAS_FFMPEG__": "true" if FFMPEG else "false",
            "__MUSIC_URL__": json.dumps(data["music_url"]),
            "__UPLOAD_DEST_JS__": json.dumps(UPLOAD_DEST) if UPLOAD_DEST else "null",
        }
        page = re.sub(
            r"__[A-Z_]+__",
            lambda m: repl.get(m.group(0), m.group(0)),
            TEMPLATE,
        )

        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def send_head(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            self.send_error(404, "File not found")
            return None
        ctype = self.guess_type(path)
        try:
            f = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None
        fs = os.fstat(f.fileno())
        size = fs.st_size
        range_header = self.headers.get("Range", "")
        start, end = 0, size - 1
        is_range = False
        if range_header.startswith("bytes="):
            try:
                spec = range_header[6:].split(",")[0].strip()
                s, _, e = spec.partition("-")
                if s:
                    start = int(s)
                    end = int(e) if e else size - 1
                else:
                    start = max(0, size - int(e))
                    end = size - 1
                if start > end or start >= size:
                    raise ValueError
                is_range = True
            except (ValueError, TypeError):
                f.close()
                self.send_error(416, "Invalid range")
                return None
        length = end - start + 1
        self._range_remaining = length
        self.send_response(206 if is_range else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if is_range:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Last-Modified", self.date_time_string(fs.st_mtime))
        base = os.path.basename(path)
        force_dl = query.get("dl", ["0"])[0] == "1"
        if force_dl:
            safe_name = base.replace('"', "_").replace("\\", "_").replace("\n", "_")
            self.send_header("Content-Disposition", f'attachment; filename="{safe_name}"')
        if base.startswith(".defxult_"):
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        self.end_headers()
        if start > 0:
            f.seek(start)
        return f

    def copyfile(self, source, outputfile):
        remaining = getattr(self, "_range_remaining", None)
        if HAS_SENDFILE:
            start_offset = source.tell()
            sent_total = 0
            try:
                in_fd = source.fileno()
                out_fd = self.connection.fileno()
                offset = start_offset
                total = remaining if remaining is not None else (
                    os.fstat(in_fd).st_size - start_offset
                )
                while total > 0:
                    try:
                        n = os.sendfile(out_fd, in_fd, offset, total)
                    except InterruptedError:
                        continue
                    except BlockingIOError:
                        continue
                    if n == 0:
                        break
                    offset += n
                    total -= n
                    sent_total += n
                return
            except (OSError, AttributeError, ValueError):
                try:
                    source.seek(start_offset + sent_total)
                except Exception:
                    pass
                if remaining is not None:
                    remaining -= sent_total

        if remaining is None:
            shutil.copyfileobj(source, outputfile, length=CHUNK)
            return
        while remaining > 0:
            chunk = source.read(min(CHUNK, remaining))
            if not chunk:
                break
            try:
                outputfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                break
            remaining -= len(chunk)

    def do_POST(self):
        if self._check_auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        path = parsed.path
        if path == "/api/tus":
            return self._api_tus_create()
        if path == "/api/wallpaper":
            return self._api_wallpaper()
        if path == "/api/music":
            return self._api_music()
        if path == "/api/upload/chunk":
            return self._api_upload_chunk(query)
        if path == "/api/upload/finalize":
            return self._api_upload_finalize(query)
        if path == "/api/upload":
            return self._api_upload(query)
        if path == "/api/mkdir":
            return self._api_mkdir(query)
        if path == "/api/rename":
            return self._api_rename(query)
        if path == "/api/delete":
            return self._api_delete(query)
        self._json_err(404, "Unknown endpoint")

    def do_PATCH(self):
        if self._check_auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/tus/"):
            uid = parsed.path[len("/api/tus/"):]
            return self._api_tus_patch(uid)
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        if self._check_auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/tus/"):
            uid = parsed.path[len("/api/tus/"):]
            return self._api_tus_head(uid)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_DELETE(self):
        if self._check_auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/tus/"):
            uid = parsed.path[len("/api/tus/"):]
            return self._api_tus_delete(uid)
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Tus-Resumable", "1.0.0")
        self.send_header("Tus-Version", "1.0.0")
        self.send_header("Tus-Max-Size", str(20 * 1024 * 1024 * 1024))
        self.send_header("Tus-Extension", "creation")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _api_tus_create(self):
        try:
            length = int(self.headers.get("Upload-Length", 0))
        except ValueError:
            return self._json_err(400, "Bad Upload-Length")
        if length <= 0:
            return self._json_err(400, "Missing Upload-Length")

        meta_raw = self.headers.get("Upload-Metadata", "")
        meta = {}
        for pair in meta_raw.split(","):
            pair = pair.strip()
            if not pair:
                continue
            k, _, v = pair.partition(" ")
            if not k:
                continue
            try:
                meta[k] = base64.b64decode(v).decode("utf-8") if v else ""
            except Exception:
                meta[k] = ""

        rel = meta.get("path", "")
        if not rel:
            return self._json_err(400, "Missing 'path' metadata")

        uid = secrets.token_urlsafe(16)
        os.makedirs(UPLOAD_CACHE_DIR, exist_ok=True)
        tmp = os.path.join(UPLOAD_CACHE_DIR, "tus_" + uid)
        try:
            with open(tmp, "wb"):
                pass
        except OSError as e:
            return self._json_err(500, f"Create failed: {e}")

        with TUS_LOCK:
            TUS_UPLOADS[uid] = {
                "path": rel,
                "total": length,
                "offset": 0,
                "tmp": tmp,
                "started": time.time(),
            }

        self.send_response(201)
        self.send_header("Tus-Resumable", "1.0.0")
        self.send_header("Location", "/api/tus/" + uid)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _api_tus_head(self, uid):
        with TUS_LOCK:
            sess = TUS_UPLOADS.get(uid)
        if not sess:
            self.send_response(404)
            self.send_header("Tus-Resumable", "1.0.0")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Tus-Resumable", "1.0.0")
        self.send_header("Upload-Offset", str(sess["offset"]))
        self.send_header("Upload-Length", str(sess["total"]))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _api_tus_patch(self, uid):
        with TUS_LOCK:
            sess = TUS_UPLOADS.get(uid)
        if not sess:
            self.send_response(404)
            self.send_header("Tus-Resumable", "1.0.0")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        try:
            client_offset = int(self.headers.get("Upload-Offset", -1))
        except ValueError:
            return self._json_err(400, "Bad Upload-Offset")

        if client_offset != sess["offset"]:
            self.send_response(409)
            self.send_header("Tus-Resumable", "1.0.0")
            self.send_header("Upload-Offset", str(sess["offset"]))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return self._json_err(400, "Bad Content-Length")

        written = 0
        try:
            with open(sess["tmp"], "ab") as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)
                    remaining -= len(chunk)
        except OSError as e:
            return self._json_err(500, f"Write failed: {e}")

        if written != length:
            # Client disconnected mid-chunk. Record what actually landed so the
            # next PATCH from the client can resume from the real offset.
            sess["offset"] += written
            self.send_response(400)
            self.send_header("Tus-Resumable", "1.0.0")
            self.send_header("Upload-Offset", str(sess["offset"]))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        sess["offset"] += written
        new_offset = sess["offset"]

        if sess["offset"] >= sess["total"]:
            target = self.translate_path(sess["path"])
            staged = None
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                staged = target + ".tuspart"
                shutil.copyfile(sess["tmp"], staged)
                os.replace(staged, target)
                staged = None
                try:
                    os.remove(sess["tmp"])
                except OSError:
                    pass
            except OSError as e:
                if staged:
                    try:
                        os.remove(staged)
                    except OSError:
                        pass
                with TUS_LOCK:
                    TUS_UPLOADS.pop(uid, None)
                return self._json_err(500, f"Finalize failed: {e}")
            with TUS_LOCK:
                TUS_UPLOADS.pop(uid, None)

        self.send_response(204)
        self.send_header("Tus-Resumable", "1.0.0")
        self.send_header("Upload-Offset", str(new_offset))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _api_tus_delete(self, uid):
        with TUS_LOCK:
            sess = TUS_UPLOADS.pop(uid, None)
        if sess:
            try:
                os.remove(sess["tmp"])
            except OSError:
                pass
        self.send_response(204)
        self.send_header("Tus-Resumable", "1.0.0")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_multipart_file(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            raise ValueError("Bad Content-Length")
        if length <= 0:
            raise ValueError("Empty body")
        body = self.rfile.read(length)
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            raise ValueError("Expected multipart/form-data")
        raw = b"Content-Type: " + ctype.encode("utf-8") + b"\r\n\r\n" + body
        msg = BytesParser(policy=default).parsebytes(raw)
        if not msg.is_multipart():
            raise ValueError("Not multipart")
        for part in msg.iter_parts():
            if part.get_content_disposition() == "form-data":
                return part.get_filename() or "", part.get_payload(decode=True)
        raise ValueError("No file field found")

    def _api_wallpaper(self):
        try:
            orig, data = self._read_multipart_file()
        except ValueError as e:
            return self._json_err(400, str(e))
        if not data:
            return self._json_err(400, "Empty file")
        ext = os.path.splitext(orig)[1].lower() or ".jpg"
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"):
            ext = ".jpg"
        for old_ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"):
            old = os.path.join(ROOT_DIR, WALLPAPER_BASE + old_ext)
            if os.path.exists(old):
                try:
                    os.remove(old)
                except OSError:
                    pass
        target = os.path.join(ROOT_DIR, WALLPAPER_BASE + ext)
        try:
            with open(target, "wb") as wf:
                wf.write(data)
        except OSError as e:
            return self._json_err(500, f"Write failed: {e}")
        cfg = load_config()
        cfg["wallpaper"] = "/" + os.path.basename(target)
        save_config(cfg)
        print(f"[wallpaper] saved {len(data)} bytes -> {target}")
        self._json({"ok": True, "path": cfg["wallpaper"], "bytes": len(data)})

    def _api_music(self):
        try:
            orig, data = self._read_multipart_file()
        except ValueError as e:
            return self._json_err(400, str(e))
        if not data:
            return self._json_err(400, "Empty file")
        ext = os.path.splitext(orig)[1].lower()
        if ext not in (".mp3", ".ogg", ".m4a", ".wav", ".flac", ".opus", ".aac"):
            return self._json_err(400, "Unsupported audio format")
        cfg = load_config()
        old_music = cfg.get("music", "")
        if old_music:
            old_path = os.path.join(ROOT_DIR, old_music.lstrip("/"))
            if os.path.isfile(old_path):
                try:
                    os.remove(old_path)
                except OSError:
                    pass
        target = os.path.join(ROOT_DIR, ".defxult_music" + ext)
        try:
            with open(target, "wb") as wf:
                wf.write(data)
        except OSError as e:
            return self._json_err(500, f"Write failed: {e}")
        cfg["music"] = "/" + os.path.basename(target)
        save_config(cfg)
        print(f"[music] saved {len(data)} bytes -> {target}")
        self._json({"ok": True, "path": cfg["music"], "bytes": len(data)})

    def _api_upload_chunk(self, query):
        uid = safe_upload_id(query.get("id", [""])[0])
        try:
            index = int(query.get("index", ["-1"])[0])
            total = int(query.get("total", ["0"])[0])
        except ValueError:
            return self._json_err(400, "Bad index/total")
        rel = query.get("path", [""])[0]
        if not uid or index < 0 or total <= 0 or not rel:
            return self._json_err(400, "Missing id/index/total/path")

        target = self.translate_path(rel)
        if os.path.isdir(target):
            return self._json_err(400, "Target is a directory")

        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return self._json_err(400, "Bad Content-Length")
        if length <= 0:
            return self._json_err(400, "Empty chunk")

        udir = os.path.join(UPLOAD_CACHE_DIR, uid)
        written = 0
        try:
            os.makedirs(udir, exist_ok=True)
            meta_path = os.path.join(udir, "meta.json")
            if not os.path.exists(meta_path):
                with open(meta_path, "w") as mf:
                    json.dump({"path": rel, "total": total, "started": time.time()}, mf)
            chunk_path = os.path.join(udir, f"{index}.part")
            with open(chunk_path, "wb") as cf:
                remaining = length
                while remaining > 0:
                    piece = self.rfile.read(min(CHUNK, remaining))
                    if not piece:
                        break
                    cf.write(piece)
                    written += len(piece)
                    remaining -= len(piece)
        except OSError as e:
            return self._json_err(500, f"Write failed: {e}")

        if written != length:
            try:
                os.remove(os.path.join(udir, f"{index}.part"))
            except OSError:
                pass
            return self._json_err(400, f"Short body: got {written} of {length}")

        self._json({"ok": True, "index": index})

    def _api_upload_finalize(self, query):
        uid = safe_upload_id(query.get("id", [""])[0])
        if not uid:
            return self._json_err(400, "Missing id")
        udir = os.path.join(UPLOAD_CACHE_DIR, uid)
        meta_path = os.path.join(udir, "meta.json")
        if not os.path.isfile(meta_path):
            return self._json_err(404, "Upload session not found")
        try:
            with open(meta_path, "r") as mf:
                meta = json.load(mf)
        except (OSError, ValueError):
            return self._json_err(500, "Corrupt metadata")

        total = int(meta.get("total", 0))
        rel = meta.get("path", "")
        if not total or not rel:
            return self._json_err(400, "Missing metadata")

        chunks = []
        for i in range(total):
            p = os.path.join(udir, f"{i}.part")
            if not os.path.isfile(p):
                return self._json_err(400, f"Missing chunk {i}")
            chunks.append(p)

        target = self.translate_path(rel)
        ok = False
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            tmp = target + ".part"
            with open(tmp, "wb") as out:
                if HAS_SENDFILE:
                    out_fd = out.fileno()
                    for c in chunks:
                        with open(c, "rb") as cf:
                            in_fd = cf.fileno()
                            offset = 0
                            csize = os.fstat(in_fd).st_size
                            while offset < csize:
                                try:
                                    n = os.sendfile(out_fd, in_fd, offset, csize - offset)
                                except InterruptedError:
                                    continue
                                if n == 0:
                                    break
                                offset += n
                else:
                    for c in chunks:
                        with open(c, "rb") as cf:
                            shutil.copyfileobj(cf, out, length=CHUNK)
            os.replace(tmp, target)
            ok = True
        except OSError as e:
            return self._json_err(500, f"Finalize failed: {e}")
        finally:
            if ok:
                shutil.rmtree(udir, ignore_errors=True)

        size = os.path.getsize(target) if os.path.exists(target) else 0
        self._json({"ok": True, "bytes": size})

    def _api_upload(self, query):
        rel = query.get("path", [""])[0]
        if not rel:
            return self._json_err(400, "Missing path")
        target = self.translate_path(rel)
        if os.path.isdir(target):
            return self._json_err(400, "Target is a directory")
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return self._json_err(400, "Bad Content-Length")
        if length <= 0:
            return self._json_err(400, "Empty upload")
        written = 0
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)
                    remaining -= len(chunk)
        except OSError as e:
            return self._json_err(500, f"Write failed: {e}")

        if written != length:
            try:
                os.remove(target)
            except OSError:
                pass
            return self._json_err(400, f"Short body: got {written} of {length}")

        self._json({"ok": True, "bytes": written})

    def _api_mkdir(self, query):
        cur = query.get("dir", [""])[0]
        name = query.get("name", [""])[0].strip()
        if not name or "/" in name or "\0" in name or name in (".", ".."):
            return self._json_err(400, "Invalid folder name")
        parent = self.translate_path(cur) if cur else ROOT_DIR
        target = os.path.abspath(os.path.join(parent, name))
        if target != ROOT_DIR and not target.startswith(ROOT_DIR + os.sep):
            return self._json_err(400, "Path escapes root")
        try:
            os.makedirs(target, exist_ok=True)
        except OSError as e:
            return self._json_err(500, f"mkdir failed: {e}")
        self._json({"ok": True})

    def _api_rename(self, query):
        old_rel = query.get("old", [""])[0]
        new_name = query.get("new", [""])[0].strip()
        if not old_rel or not new_name:
            return self._json_err(400, "Missing old/new")
        if "/" in new_name or "\0" in new_name or new_name in (".", ".."):
            return self._json_err(400, "Invalid new name")
        old_p = self.translate_path(old_rel)
        if not os.path.exists(old_p):
            return self._json_err(404, "Source not found")
        new_p = os.path.join(os.path.dirname(old_p), new_name)
        if os.path.exists(new_p):
            return self._json_err(409, "Target already exists")
        try:
            os.rename(old_p, new_p)
        except OSError as e:
            return self._json_err(500, f"Rename failed: {e}")
        self._json({"ok": True})

    def _api_delete(self, query):
        paths = query.get("path", [])
        deleted = 0
        for p in paths:
            full = self.translate_path(p)
            if full == ROOT_DIR:
                continue
            try:
                if os.path.isdir(full):
                    shutil.rmtree(full)
                elif os.path.exists(full):
                    os.remove(full)
                else:
                    continue
                deleted += 1
            except OSError:
                continue
        self._json({"ok": True, "deleted": deleted})


class HighSpeedHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 128
    block_on_close = False

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        if hasattr(socket, "TCP_QUICKACK"):
            try:
                self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_QUICKACK, 1)
            except OSError:
                pass
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 * 1024 * 1024)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
        except OSError:
            pass
        super().server_bind()

    def process_request(self, request, client_address):
        try:
            request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        if hasattr(socket, "TCP_QUICKACK"):
            try:
                request.setsockopt(socket.IPPROTO_TCP, socket.TCP_QUICKACK, 1)
            except OSError:
                pass
        try:
            request.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 * 1024 * 1024)
            request.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
        except OSError:
            pass
        super().process_request(request, client_address)


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<title>defxult transfer</title>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0a0b12">
<link rel="stylesheet" href="/glassify.css">
<style>
:root, [data-theme="dark"] {
    --bg: #0a0b12;
    --bg-gradient: linear-gradient(180deg, #0d0f18 0%, #0a0b12 100%);
    --surface-1: #12141f;
    --surface-2: #171a28;
    --surface-3: #1e2233;
    --surface-hi: #232739;
    --border: rgba(255, 255, 255, 0.07);
    --border-hi: rgba(255, 255, 255, 0.12);
    --text: #f5f5f7;
    --text-muted: #8e8e9c;
    --text-dim: #6b6b7a;
    --accent: #64d2ff;
    --accent-2: #0a84ff;
    --accent-gradient: linear-gradient(135deg, #64d2ff 0%, #0a84ff 100%);
    --danger: #ff453a;
    --success: #30d158;
    --row-hover: rgba(255, 255, 255, 0.04);
    --overlay: rgba(0, 0, 0, 0.7);
    --shadow-sm: 0 2px 6px rgba(0, 0, 0, 0.25), 0 1px 2px rgba(0, 0, 0, 0.2);
    --shadow-md: 0 8px 24px rgba(0, 0, 0, 0.35), 0 2px 6px rgba(0, 0, 0, 0.25);
    --shadow-lg: 0 20px 48px rgba(0, 0, 0, 0.45), 0 4px 12px rgba(0, 0, 0, 0.3);
    --shadow-xl: 0 30px 80px rgba(0, 0, 0, 0.65), 0 8px 24px rgba(0, 0, 0, 0.4);
}
[data-theme="light"] {
    --bg: #eef1f8;
    --bg-gradient: linear-gradient(180deg, #f5f7fb 0%, #eef1f8 100%);
    --surface-1: #ffffff;
    --surface-2: #ffffff;
    --surface-3: #f4f6fb;
    --surface-hi: #e9edf6;
    --border: rgba(15, 20, 40, 0.08);
    --border-hi: rgba(15, 20, 40, 0.14);
    --text: #101014;
    --text-muted: #5a5c68;
    --text-dim: #8a8c98;
    --accent: #007aff;
    --accent-2: #0a84ff;
    --accent-gradient: linear-gradient(135deg, #007aff 0%, #34aadc 100%);
    --danger: #ff3b30;
    --success: #34c759;
    --row-hover: rgba(0, 0, 0, 0.03);
    --overlay: rgba(15, 20, 40, 0.5);
    --shadow-sm: 0 2px 6px rgba(80, 100, 140, 0.12), 0 1px 2px rgba(80, 100, 140, 0.08);
    --shadow-md: 0 8px 24px rgba(80, 100, 140, 0.14), 0 2px 6px rgba(80, 100, 140, 0.1);
    --shadow-lg: 0 20px 48px rgba(80, 100, 140, 0.18), 0 4px 12px rgba(80, 100, 140, 0.12);
    --shadow-xl: 0 30px 80px rgba(80, 100, 140, 0.24), 0 8px 24px rgba(80, 100, 140, 0.14);
}
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
html, body { height: 100%; }
body {
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, sans-serif;
    color: var(--text);
    max-width: 1400px;
    margin: 0 auto;
    padding: 20px 24px 120px;
    padding-top: max(20px, env(safe-area-inset-top));
    overflow-x: clip;
    -webkit-font-smoothing: antialiased;
    background: var(--bg);
    background-image: __BG_OVERRIDE__ var(--bg-gradient);
    background-size: cover;
    background-position: center;
    background-attachment: fixed;
    background-repeat: no-repeat;
    transition: color 0.3s ease, background 0.3s ease;
}
.surface { background: var(--surface-1); border: 1px solid var(--border); box-shadow: var(--shadow-md); }
#mainContainer { transition: opacity 0.3s ease, transform 0.3s ease; }
#mainContainer.blur-bg { opacity: 0.35; transform: scale(0.98); pointer-events: none; }
.app-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 18px; padding: 14px 20px; border-radius: 16px; gap: 14px; flex-wrap: wrap; }
.brand { display: flex; align-items: center; gap: 10px; min-width: 0; }
.brand-mark { width: 30px; height: 30px; border-radius: 10px; background: var(--accent-gradient); display: grid; place-items: center; box-shadow: 0 4px 12px rgba(100, 210, 255, 0.4), inset 0 1px 0 rgba(255,255,255,0.4); flex-shrink: 0; color: #ffffff; }
h1 { font-size: 1.05rem; margin: 0; font-weight: 700; letter-spacing: -0.02em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
h1 .accent { background: var(--accent-gradient); -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent; }
.header-actions { display: flex; gap: 8px; flex-wrap: wrap; }
svg.ico-svg { width: 1em; height: 1em; vertical-align: -0.15em; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
.brand-mark svg.ico-svg { width: 16px; height: 16px; }
.btn-icon svg.ico-svg { width: 16px; height: 16px; }
.audio-btn svg.ico-svg { width: 15px; height: 15px; }
.drop-icon svg.ico-svg { width: 32px; height: 32px; }
.layout { display: grid; grid-template-columns: minmax(0, 1fr) 340px; gap: 18px; align-items: start; }
.main-col { min-width: 0; }
.sidebar { position: sticky; top: 20px; display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.sticky-nav { position: sticky; top: 12px; z-index: 800; border-radius: 14px; padding: 10px 14px; margin-bottom: 14px; }
.nav-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.nav-row .grow { flex: 1 1 180px; min-width: 130px; }
#searchBox { width: 100%; padding: 10px 14px; font-size: 0.86rem; border-radius: 10px; background: var(--surface-3); border: 1px solid var(--border); color: var(--text); outline: none; transition: border-color 0.2s, box-shadow 0.2s, background 0.2s; font-family: inherit; }
#searchBox:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(100, 210, 255, 0.15); }
#searchBox::placeholder { color: var(--text-dim); }
.nav-select { width: auto; min-width: 0; padding: 10px 28px 10px 12px; font-size: 0.82rem; border-radius: 10px; background-color: var(--surface-3); border: 1px solid var(--border); color: var(--text); font-family: inherit; -webkit-appearance: none; appearance: none; background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10' viewBox='0 0 12 12'><path d='M2 4l4 4 4-4' stroke='%23888' stroke-width='1.7' fill='none' stroke-linecap='round'/></svg>"); background-repeat: no-repeat; background-position: right 10px center; flex: 0 0 auto; cursor: pointer; outline: none; }
.nav-select:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(100, 210, 255, 0.15); }
.btn-nav { padding: 10px 14px; font-size: 0.8rem; border-radius: 10px; flex: 0 0 auto; }
.nav-path { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 0.72rem; color: var(--text-muted); margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--border); overflow-x: auto; white-space: nowrap; letter-spacing: -0.01em; scrollbar-width: none; }
.nav-path::-webkit-scrollbar { display: none; }
.selection-title { font-size: 0.82rem; font-weight: 600; color: var(--text-muted); margin-bottom: 10px; letter-spacing: -0.01em; }
.selection-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.selection-grid .btn-sm { padding: 8px 10px; font-size: 0.78rem; border-radius: 9px; }
.selection-grid .full-row { grid-column: 1 / -1; }
#batchBanner { display: none; border-radius: 14px; padding: 14px 16px; background: var(--surface-1); border: 1px solid var(--border); box-shadow: var(--shadow-md); animation: bannerIn 0.3s cubic-bezier(0.34,1.56,0.64,1); }
#batchBanner.show { display: block; }
#batchBanner .grow { display: block; font-weight: 500; color: var(--text-muted); font-size: 0.82rem; margin-bottom: 10px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
@keyframes bannerIn { from { opacity: 0; transform: translateY(-6px); } to { opacity: 1; transform: translateY(0); } }
.card { border-radius: 14px; padding: 18px; margin-bottom: 14px; }
.card:last-child { margin-bottom: 0; }
.storage-meter { font-size: 0.82rem; color: var(--text-muted); margin-bottom: 10px; display: flex; justify-content: space-between; font-weight: 500; }
.progress-track { background: var(--surface-hi); border-radius: 100px; height: 8px; width: 100%; overflow: hidden; position: relative; box-shadow: inset 0 1px 3px rgba(0, 0, 0, 0.3); }
.progress-fill { background: var(--accent-gradient); height: 100%; width: __STORAGE_PCT__%; border-radius: 100px; transition: width 0.8s cubic-bezier(0.34,1.56,0.64,1); position: relative; overflow: hidden; box-shadow: 0 0 10px rgba(100, 210, 255, 0.5); }
.progress-fill::after { content: ""; position: absolute; inset: 0; background: linear-gradient(90deg, transparent, rgba(255,255,255,0.35), transparent); background-size: 200% 100%; animation: shimmer 2.4s ease-in-out infinite; }
@keyframes shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
#audioBar { padding: 12px 14px; display: none; flex-direction: column; gap: 10px; border-radius: 14px; }
#audioBar.show { display: flex; }
#waveform { width: 100%; height: 44px; border-radius: 10px; background: var(--surface-3); overflow: hidden; box-shadow: inset 0 1px 3px rgba(0, 0, 0, 0.25); }
.audio-row { display: flex; align-items: center; gap: 10px; }
.audio-btn { background: var(--surface-3); border: 1px solid var(--border); color: var(--text); width: 32px; height: 32px; border-radius: 50%; display: grid; place-items: center; cursor: pointer; flex-shrink: 0; padding: 0; transition: transform 0.15s ease, background 0.15s ease; box-shadow: var(--shadow-sm); }
.audio-btn:hover { background: var(--surface-hi); }
.audio-btn:active { transform: scale(0.9); }
.volume-slider { -webkit-appearance: none; appearance: none; flex: 1; height: 4px; background: var(--surface-hi); border-radius: 4px; outline: none; cursor: pointer; }
.volume-slider::-webkit-slider-thumb { -webkit-appearance: none; appearance: none; width: 14px; height: 14px; border-radius: 50%; background: var(--accent); cursor: pointer; box-shadow: 0 2px 6px rgba(10, 132, 255, 0.5); }
.volume-slider::-moz-range-thumb { width: 14px; height: 14px; border: none; border-radius: 50%; background: var(--accent); cursor: pointer; }
.vol-label { font-size: 0.74rem; color: var(--text-muted); min-width: 2.6em; text-align: right; font-variant-numeric: tabular-nums; flex-shrink: 0; }
.sidebar-dropzone { border: 2px dashed var(--border-hi); border-radius: 14px; padding: 22px 16px; text-align: center; color: var(--text-muted); cursor: pointer; transition: all 0.25s cubic-bezier(0.34,1.56,0.64,1); background: var(--surface-3); font-size: 0.86rem; margin-bottom: 12px; line-height: 1.5; }
.sidebar-dropzone:hover { border-color: var(--accent); background: rgba(100, 210, 255, 0.06); }
.sidebar-dropzone.drag { border-color: var(--accent); background: rgba(100, 210, 255, 0.12); transform: scale(1.02); }
.sidebar-dropzone label { color: var(--accent); cursor: pointer; font-weight: 600; text-decoration: underline; text-underline-offset: 3px; }
.sidebar-dropzone .drop-icon { display: block; margin-bottom: 6px; color: var(--accent); }
.sidebar-dropzone .drop-hint { font-size: 0.72rem; color: var(--text-dim); margin-top: 6px; line-height: 1.4; word-break: break-word; }
#uploadProgressContainer { margin-top: 12px; }
#uploadProgressContainer .storage-meter { font-size: 0.74rem; margin-bottom: 6px; }
.btn { position: relative; background: var(--accent-gradient); color: #ffffff; border: none; padding: 10px 16px; border-radius: 10px; font-weight: 600; font-size: 0.86rem; font-family: inherit; letter-spacing: -0.01em; cursor: pointer; overflow: hidden; display: inline-flex; align-items: center; gap: 6px; justify-content: center; box-shadow: 0 4px 14px rgba(10, 132, 255, 0.35), inset 0 1px 0 rgba(255,255,255,0.25); transition: transform 0.15s cubic-bezier(0.34,1.56,0.64,1), box-shadow 0.2s ease, filter 0.15s ease; white-space: nowrap; }
.btn:hover { filter: brightness(1.08); }
.btn:active { transform: scale(0.94); box-shadow: 0 2px 8px rgba(10, 132, 255, 0.35); }
.btn-secondary { background: var(--surface-3); color: var(--text); border: 1px solid var(--border); box-shadow: var(--shadow-sm); }
.btn-secondary:hover { background: var(--surface-hi); filter: none; }
.btn-danger { background: linear-gradient(135deg, #ff453a, #c81f14); color: #ffffff; box-shadow: 0 4px 14px rgba(255, 69, 58, 0.35); }
.btn-icon { width: 36px; height: 36px; padding: 0; border-radius: 10px; }
.btn-icon.active { background: var(--accent-gradient); color: #ffffff; box-shadow: 0 4px 14px rgba(10, 132, 255, 0.4); }
.btn-sm { padding: 7px 12px; font-size: 0.78rem; border-radius: 9px; }
.ripple { position: absolute; width: 4px; height: 4px; border-radius: 50%; background: rgba(255, 255, 255, 0.55); transform: translate(-50%, -50%); animation: rippleAnim 0.6s ease-out forwards; pointer-events: none; }
@keyframes rippleAnim { to { width: 320px; height: 320px; opacity: 0; } }
ul { list-style: none; padding: 0; margin: 0; }
li { display: flex; align-items: center; justify-content: space-between; padding: 12px 14px; border-bottom: 1px solid var(--border); gap: 12px; position: relative; background: transparent; border-radius: 12px; transition: background 0.15s ease, transform 0.25s ease; animation: rowIn 0.3s ease backwards; animation-delay: calc(var(--i, 0) * 22ms); }
li:last-child { border-bottom: none; }
li:hover { background: var(--row-hover); }
@keyframes rowIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }
@keyframes fadeUp { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
@keyframes navIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
.card { animation: fadeUp 0.4s ease backwards; }
.card:nth-of-type(1) { animation-delay: 0.03s; }
.card:nth-of-type(2) { animation-delay: 0.07s; }
#fileList { transition: opacity 0.12s ease, transform 0.12s ease; }
#fileList.nav-out { opacity: 0; transform: translateY(4px); }
#fileList.nav-in { animation: navIn 0.26s ease; }
#loadSentinel { height: 1px; width: 100%; list-style: none; pointer-events: none; padding: 0; border: none; }
#fileList.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 12px; padding: 4px; }
#fileList.grid li { flex-direction: column; align-items: stretch; border-bottom: none; padding: 10px; gap: 8px; background: var(--surface-2); border: 1px solid var(--border); position: relative; overflow: hidden; box-shadow: var(--shadow-sm); }
#fileList.grid li:hover { background: var(--surface-3); transform: translateY(-2px); box-shadow: var(--shadow-md); }
#fileList.grid .file-chk { position: absolute; top: 8px; left: 8px; z-index: 5; background: var(--surface-1); border-radius: 6px; padding: 2px; width: 22px; height: 22px; }
#fileList.grid .file-info { flex-direction: column; align-items: stretch; gap: 8px; padding: 0; margin: 0; pointer-events: auto; }
#fileList.grid .file-ico, #fileList.grid .file-thumb { width: 100%; height: 100px; font-size: 2.4rem; line-height: 100px; text-align: center; border-radius: 10px; display: flex; align-items: center; justify-content: center; object-fit: cover; background: var(--surface-3); }
#fileList.grid .file-thumb { border: 1px solid var(--border); }
#fileList.grid .file-name { font-size: 0.8rem; text-align: center; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; width: 100%; }
#fileList.grid .file-meta { font-size: 0.72rem; text-align: center; margin: 0; }
#fileList.grid #loadSentinel { grid-column: 1 / -1; height: 1px; }
.file-chk { width: 18px; height: 18px; accent-color: var(--accent); cursor: pointer; flex-shrink: 0; }
.file-info { display: flex; align-items: center; gap: 14px; flex: 1; min-width: 0; text-decoration: none; color: inherit; border-radius: 8px; padding: 4px 6px; margin: -4px -6px; transition: background 0.15s ease; cursor: pointer; }
.file-info:active { background: var(--row-hover); }
.file-ico { font-size: 1.25rem; width: 40px; text-align: center; flex-shrink: 0; line-height: 1; }
.file-thumb { width: 40px; height: 40px; border-radius: 8px; object-fit: cover; border: 1px solid var(--border); flex-shrink: 0; background: var(--surface-3); }
.file-name { font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 0.9rem; letter-spacing: -0.01em; }
.file-meta { font-size: 0.76rem; color: var(--text-muted); margin-left: auto; flex-shrink: 0; font-variant-numeric: tabular-nums; letter-spacing: -0.01em; padding-left: 12px; }
.empty-row { display: flex; justify-content: center; padding: 14px; animation: fadeUp 0.4s ease 0.2s backwards; }
.modal { position: fixed; inset: 0; background: var(--overlay); display: none; justify-content: center; align-items: center; z-index: 2000; opacity: 0; transition: opacity 0.3s ease; padding: 20px; }
.modal.show { opacity: 1; }
.modal-content { background: var(--surface-2); border: 1px solid var(--border-hi); padding: 24px; border-radius: 22px; max-width: 780px; width: 100%; box-shadow: var(--shadow-xl); transform: translateY(30px) scale(0.94); transition: transform 0.4s cubic-bezier(0.34,1.56,0.64,1); }
.modal.show .modal-content { transform: translateY(0) scale(1); }
.modal h3 { margin: 0 0 18px; font-size: 1.05rem; font-weight: 700; letter-spacing: -0.02em; }
.modal input[type="text"] { padding: 11px 14px; background: var(--surface-3); border: 1px solid var(--border); border-radius: 10px; color: var(--text); font-size: 0.9rem; font-family: inherit; outline: none; width: 100%; }
.folder-list-item { padding: 11px 14px; cursor: pointer; border-radius: 10px; margin: 2px 4px; transition: background 0.15s ease; font-size: 0.88rem; }
.folder-list-item:hover { background: var(--surface-3); }
#mediaContainer { margin-bottom: 14px; min-height: 80px; display: flex; align-items: center; justify-content: center; flex-direction: column; }
#mediaContainer img { width: 100%; max-height: 68vh; display: block; background: #000; border-radius: 14px; overflow: hidden; }
.vm-stage { display: grid; grid-template-columns: 56px minmax(0, 1fr) 56px; gap: 14px; align-items: center; width: 100%; }
.vm-column { min-width: 0; }
.vm-stage .vm-arrow, .img-stage .nav-arrow.small { background: transparent; border: 1px solid var(--border-hi); color: var(--text); width: 56px; height: 120px; border-radius: 100px; font-size: 1.6rem; line-height: 1; display: grid; place-items: center; cursor: pointer; font-family: inherit; transition: transform 0.2s ease, background 0.2s ease, opacity 0.2s ease; }
.vm-stage .vm-arrow:hover:not(:disabled), .img-stage .nav-arrow.small:hover:not(:disabled) { background: var(--surface-3); transform: scale(1.05); }
.vm-stage .vm-arrow:active:not(:disabled), .img-stage .nav-arrow.small:active:not(:disabled) { transform: scale(0.94); }
.vm-stage .vm-arrow:disabled, .img-stage .nav-arrow.small:disabled { opacity: 0.2; cursor: not-allowed; }
.vm-frame { position: relative; background: #000; overflow: hidden; width: 100%; min-width: 0; border: 1px solid rgba(255, 255, 255, 0.18); aspect-ratio: 16 / 9; max-height: 62vh; }
.vm-video { display: block; width: 100%; height: 100%; object-fit: contain; background: #000; cursor: pointer; }

/* ---- Floating glass video controls ---- */
.vm-glass-top {
    position: absolute;
    top: 10px; left: 14px; right: 14px;
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
    pointer-events: none;
    z-index: 3;
    opacity: 0;
    transition: opacity 0.28s ease;
}
.vm-frame.controls-visible .vm-glass-top { opacity: 1; }
.vm-title {
    font-weight: 600;
    font-size: 0.74rem;
    color: #fff;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    min-width: 0;
    flex: 1;
    text-shadow: 0 1px 3px rgba(0,0,0,0.85);
    letter-spacing: -0.01em;
}
.vm-counter {
    font-size: 0.62rem;
    color: #fff;
    flex-shrink: 0;
    font-variant-numeric: tabular-nums;
    padding: 3px 9px;
    background: rgba(255,255,255,0.14);
    backdrop-filter: blur(12px) saturate(1.5);
    -webkit-backdrop-filter: blur(12px) saturate(1.5);
    border: 1px solid rgba(255,255,255,0.18);
    border-radius: 100px;
    font-weight: 600;
    letter-spacing: 0.04em;
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.15);
}

.vm-center-play {
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%) scale(0.85);
    width: 44px;
    height: 44px;
    border-radius: 50%;
    background: rgba(0,0,0,0.42);
    backdrop-filter: blur(12px) saturate(1.4);
    -webkit-backdrop-filter: blur(12px) saturate(1.4);
    border: 1px solid rgba(255,255,255,0.22);
    color: #fff;
    display: grid;
    place-items: center;
    cursor: pointer;
    z-index: 4;
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.22s ease, transform 0.22s ease, background 0.15s;
    box-shadow: 0 6px 22px rgba(0,0,0,0.5), inset 0 1px 0 rgba(255,255,255,0.15);
    padding: 0;
    font-family: inherit;
    line-height: 1;
}
.vm-frame.controls-visible .vm-center-play {
    opacity: 1;
    pointer-events: auto;
    transform: translate(-50%, -50%) scale(1);
}
.vm-center-play:hover {
    background: rgba(0,0,0,0.6);
}
.vm-center-play:active {
    transform: translate(-50%, -50%) scale(0.94);
}
.vm-center-play svg.ico-svg {
    width: 16px;
    height: 16px;
    margin-left: 2px;
}
.vm-center-play.is-playing svg.ico-svg {
    margin-left: 0;
}

.vm-glass-bottom {
    position: absolute;
    bottom: 10px; left: 14px; right: 14px;
    z-index: 4;
    display: flex;
    flex-direction: column;
    gap: 6px;
    opacity: 0;
    transform: translateY(6px);
    transition: opacity 0.25s ease, transform 0.25s ease;
    pointer-events: none;
}
.vm-frame.controls-visible .vm-glass-bottom {
    opacity: 1;
    transform: translateY(0);
    pointer-events: auto;
}
.vm-seek-row { display: flex; align-items: center; gap: 10px; }
.vm-seek { flex: 1; height: 2px; -webkit-appearance: none; appearance: none; background: rgba(255,255,255,0.22); border-radius: 100px; outline: none; cursor: pointer; }
.vm-seek::-webkit-slider-thumb { -webkit-appearance: none; appearance: none; width: 12px; height: 12px; border-radius: 50%; background: #fff; cursor: pointer; box-shadow: 0 0 0 3px rgba(100,210,255,0.4), 0 2px 6px rgba(0,0,0,0.5); transition: transform 0.15s ease, box-shadow 0.15s ease; }
.vm-seek::-webkit-slider-thumb:hover { transform: scale(1.15); box-shadow: 0 0 0 5px rgba(100,210,255,0.55), 0 2px 6px rgba(0,0,0,0.5); }
.vm-seek::-moz-range-thumb { width: 12px; height: 12px; border: none; border-radius: 50%; background: #fff; box-shadow: 0 0 0 3px rgba(100,210,255,0.4), 0 2px 6px rgba(0,0,0,0.5); }
.vm-time { font-size: 0.64rem; color: #fff; font-variant-numeric: tabular-nums; white-space: nowrap; font-weight: 500; text-shadow: 0 1px 3px rgba(0,0,0,0.6); }
.vm-btn-row { display: flex; align-items: center; gap: 6px; }
.vm-glass-btn {
    background: rgba(255,255,255,0.10);
    backdrop-filter: blur(16px) saturate(1.5);
    -webkit-backdrop-filter: blur(16px) saturate(1.5);
    border: 1px solid rgba(255,255,255,0.16);
    color: #fff;
    cursor: pointer;
    width: 24px;
    height: 24px;
    border-radius: 50%;
    display: grid;
    place-items: center;
    padding: 0;
    font-family: inherit;
    font-size: 0.66rem;
    line-height: 1;
    transition: background 0.15s, transform 0.15s;
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.10);
}
.vm-glass-btn:hover { background: rgba(255,255,255,0.22); }
.vm-glass-btn:active { transform: scale(0.9); }
.vm-glass-btn svg.ico-svg { width: 12px; height: 12px; }
.vm-spacer { flex: 1; }
.vm-vol-wrap {
    display: flex;
    align-items: center;
    gap: 4px;
    background: rgba(255,255,255,0.08);
    backdrop-filter: blur(16px) saturate(1.5);
    -webkit-backdrop-filter: blur(16px) saturate(1.5);
    border: 1px solid rgba(255,255,255,0.14);
    border-radius: 100px;
    padding: 1px 6px 1px 1px;
}
.vm-vol-wrap .vm-glass-btn { width: 20px; height: 20px; border: none; background: transparent; box-shadow: none; backdrop-filter: none; -webkit-backdrop-filter: none; }
.vm-vol-wrap .vm-glass-btn:hover { background: rgba(255,255,255,0.16); transform: none; }
.vm-vol { width: 48px; height: 2px; -webkit-appearance: none; appearance: none; background: rgba(255,255,255,0.22); border-radius: 100px; outline: none; cursor: pointer; }
.vm-vol::-webkit-slider-thumb { -webkit-appearance: none; appearance: none; width: 9px; height: 9px; border-radius: 50%; background: #fff; cursor: pointer; box-shadow: 0 1px 3px rgba(0,0,0,0.5); }
.vm-vol::-moz-range-thumb { width: 9px; height: 9px; border: none; border-radius: 50%; background: #fff; }
.vm-speed {
    background: rgba(255,255,255,0.10);
    backdrop-filter: blur(16px) saturate(1.5);
    -webkit-backdrop-filter: blur(16px) saturate(1.5);
    border: 1px solid rgba(255,255,255,0.16);
    color: #fff;
    border-radius: 100px;
    padding: 3px 7px;
    font-size: 0.64rem;
    font-family: inherit;
    cursor: pointer;
    outline: none;
    font-weight: 600;
}
.vm-speed:hover { background: rgba(255,255,255,0.20); }
.vm-speed option { background: #171a28; color: #f5f5f7; }
.vm-loading { position: absolute; inset: 0; display: none; align-items: center; justify-content: center; background: rgba(0,0,0,0.5); z-index: 5; pointer-events: none; }
.vm-loading.show { display: flex; }
.vm-loading-ring { width: 44px; height: 44px; border-radius: 50%; border: 3px solid rgba(255,255,255,0.15); border-top-color: var(--accent); animation: vmSpin 0.9s linear infinite; }
@keyframes vmSpin { to { transform: rotate(360deg); } }

.img-stage { display: grid; grid-template-columns: 56px minmax(0, 1fr) 56px; gap: 14px; align-items: center; width: 100%; }
.img-main { min-width: 0; display: flex; flex-direction: column; gap: 10px; padding: 14px; background: var(--surface-1); border: 1px solid var(--border); border-radius: 18px; }
.img-name { font-weight: 600; font-size: 0.88rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: center; color: var(--text-muted); }
.img-main img { width: 100%; max-height: 68vh; object-fit: contain; border-radius: 12px; background: #000; display: block; }
@media (max-width: 640px) {
    .vm-stage { grid-template-columns: 40px minmax(0, 1fr) 40px; gap: 8px; }
    .vm-stage .vm-arrow, .img-stage .nav-arrow.small { width: 40px; height: 90px; font-size: 1.3rem; }
    .vm-frame { max-height: 50vh; }
    .vm-glass-top { top: 8px; left: 10px; right: 10px; }
    .vm-glass-bottom { bottom: 8px; left: 10px; right: 10px; gap: 5px; }
    .vm-center-play { width: 38px; height: 38px; }
    .vm-center-play svg.ico-svg { width: 14px; height: 14px; }
    .vm-glass-btn { width: 22px; height: 22px; }
    .vm-glass-btn svg.ico-svg { width: 11px; height: 11px; }
    .vm-vol { width: 40px; }
    .vm-vol-wrap .vm-glass-btn { width: 18px; height: 18px; }
    .vm-title { font-size: 0.7rem; }
    .vm-time { font-size: 0.6rem; }
    .vm-speed { padding: 2px 6px; font-size: 0.6rem; }
    .img-stage { grid-template-columns: 40px minmax(0, 1fr) 40px; gap: 8px; }
}
.audio-stream-layout { display: grid; grid-template-columns: 52px minmax(0, 1fr) 52px; gap: 12px; align-items: center; width: 100%; }
.audio-stream-main { min-width: 0; display: flex; flex-direction: column; gap: 12px; padding: 18px; background: var(--surface-1); border: 1px solid var(--border); border-radius: 18px; }
.audio-stream-name { font-weight: 600; font-size: 0.92rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: center; letter-spacing: -0.01em; }
.audio-stream-main audio { width: 100%; margin-top: 8px; }
.nav-arrow { background: var(--surface-1); border: 1px solid var(--border); color: var(--text); width: 60px; height: 140px; border-radius: 18px; font-size: 2rem; line-height: 1; display: grid; place-items: center; cursor: pointer; font-family: inherit; transition: background 0.15s, transform 0.15s; }
.nav-arrow.small { width: 52px; height: 100px; font-size: 1.6rem; }
.nav-arrow:hover:not(:disabled) { background: var(--surface-3); }
.nav-arrow:active:not(:disabled) { transform: scale(0.92); }
.nav-arrow:disabled { opacity: 0.25; cursor: not-allowed; }
.hover-preview { position: fixed; left: 50%; top: 50%; transform: translate(-50%, -50%) scale(0.92); border-radius: 16px; overflow: hidden; background: #000; border: 1px solid var(--border-hi); box-shadow: var(--shadow-xl); opacity: 0; transition: opacity 0.22s ease, transform 0.22s cubic-bezier(0.34,1.56,0.64,1); pointer-events: none; z-index: 1500; display: none; }
.hover-preview.show { opacity: 1; transform: translate(-50%, -50%) scale(1); display: block; }
.hover-preview video { width: 100%; height: 100%; object-fit: contain; display: block; background: #000; }
.hover-preview-label { position: absolute; left: 0; right: 0; bottom: 0; padding: 10px 14px; background: linear-gradient(to top, rgba(0,0,0,0.95), transparent); color: #fff; font-size: 0.8rem; font-weight: 500; display: flex; justify-content: space-between; gap: 12px; overflow: hidden; }
.hover-preview-label .hpl-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; min-width: 0; }
.hover-preview-label .hpl-speed { color: var(--accent); font-weight: 700; flex-shrink: 0; font-variant-numeric: tabular-nums; }
#toastContainer { position: fixed; right: 20px; bottom: max(20px, env(safe-area-inset-bottom)); left: auto; transform: none; display: flex; flex-direction: column-reverse; gap: 8px; z-index: 3000; pointer-events: none; width: min(360px, calc(100vw - 32px)); align-items: stretch; }
.toast { position: relative; background: var(--surface-2); border: 1px solid var(--border-hi); color: var(--text); padding: 12px 36px 12px 16px; border-radius: 12px; font-size: 0.84rem; font-weight: 500; box-shadow: var(--shadow-lg); display: flex; align-items: center; gap: 8px; transform: translateY(20px) scale(0.95); opacity: 0; transition: transform 0.4s cubic-bezier(0.34,1.56,0.64,1), opacity 0.3s ease; pointer-events: auto; word-break: break-word; }
.toast.show { transform: translateY(0) scale(1); opacity: 1; }
.toast::before { content: ''; width: 7px; height: 7px; border-radius: 50%; background: var(--accent); flex-shrink: 0; box-shadow: 0 0 8px currentColor; }
.toast.success::before { background: var(--success); color: var(--success); }
.toast.error::before { background: var(--danger); color: var(--danger); }
.toast.warn::before { background: #ffb020; color: #ffb020; }
.toast-text { flex: 1; min-width: 0; }
.toast-close { position: absolute; top: 8px; right: 8px; width: 22px; height: 22px; border-radius: 50%; border: none; background: var(--surface-hi); color: var(--text-muted); font-size: 0.72rem; line-height: 1; display: grid; place-items: center; cursor: pointer; padding: 0; font-family: inherit; transition: background 0.15s, color 0.15s; }
.toast-close:hover { background: rgba(255, 69, 58, 0.25); color: var(--danger); }
.scope-btn.active { background: var(--accent-gradient); color: #ffffff; }
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: rgba(127, 127, 127, 0.3); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: rgba(127, 127, 127, 0.5); }
@media (max-width: 1100px) { .layout { grid-template-columns: minmax(0, 1fr) 300px; gap: 14px; } }
@media (max-width: 860px) { .layout { grid-template-columns: 1fr; gap: 14px; } .sidebar { position: static; order: 2; } .main-col { order: 1; } }
@media (max-width: 640px) {
    body { padding: 14px 12px 120px; padding-top: max(14px, env(safe-area-inset-top)); }
    .app-header { padding: 12px 14px; margin-bottom: 14px; border-radius: 14px; }
    h1 { font-size: 1rem; }
    .brand-mark { width: 28px; height: 28px; border-radius: 9px; }
    .card { padding: 14px; border-radius: 14px; margin-bottom: 14px; }
    .sticky-nav { padding: 10px 12px; border-radius: 12px; }
    #audioBar { padding: 10px 12px; gap: 8px; border-radius: 12px; }
    #waveform { height: 38px; }
    .btn { padding: 9px 14px; font-size: 0.8rem; }
    .btn-icon { width: 34px; height: 34px; }
    .btn-nav { padding: 9px 11px; font-size: 0.76rem; }
    .nav-row { gap: 6px; }
    .nav-row .grow { flex: 1 1 100%; min-width: 0; }
    .nav-select { padding: 9px 24px 9px 10px; font-size: 0.78rem; }
    #searchBox { padding: 9px 12px; font-size: 0.82rem; }
    .nav-path { font-size: 0.68rem; margin-top: 6px; padding-top: 6px; }
    .file-ico, .file-thumb { width: 38px; height: 38px; }
    .file-info { gap: 10px; }
    .file-name { font-size: 0.86rem; }
    .modal-content { padding: 18px; border-radius: 18px; }
    #fileList.grid { grid-template-columns: repeat(auto-fill, minmax(108px, 1fr)); gap: 10px; }
    #fileList.grid .file-ico, #fileList.grid .file-thumb { height: 82px; line-height: 82px; font-size: 2rem; }
    .sidebar-dropzone { padding: 18px 14px; font-size: 0.84rem; }
    #toastContainer { right: 12px; width: min(320px, calc(100vw - 24px)); }
    .audio-stream-layout { grid-template-columns: 40px minmax(0, 1fr) 40px; gap: 8px; }
    .nav-arrow { width: 42px; height: 96px; font-size: 1.4rem; border-radius: 14px; }
    .nav-arrow.small { width: 40px; height: 82px; font-size: 1.2rem; }
    .audio-stream-main { padding: 12px; border-radius: 12px; }
    .hover-preview { display: none !important; }
}
@media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation-duration: 0.01ms !important; animation-iteration-count: 1 !important; transition-duration: 0.01ms !important; }
}
</style>
<style id="wallpaperStyle"></style>
</head>
<body>
<div id="mainContainer">
    <header class="app-header surface">
        <div class="brand">
            <div class="brand-mark" id="brandMark"></div>
            <h1>defxult <span class="accent">transfer</span></h1>
        </div>
        <div class="header-actions">
            <button class="btn btn-secondary btn-icon" id="viewBtn" onclick="toggleView()" title="Toggle grid/list view"></button>
            <button class="btn btn-secondary btn-icon" id="themeBtn" onclick="toggleTheme()" title="Toggle theme"></button>
            <button class="btn btn-secondary btn-icon" id="vibeBtn" onclick="toggleVibe()" title="Play/pause music"></button>
            <button class="btn btn-secondary btn-icon" onclick="triggerMusicUpload()" title="Upload custom music" data-icon="file-audio"></button>
            <button class="btn btn-secondary btn-icon" onclick="triggerWallpaperUpload()" title="Set wallpaper" data-icon="image"></button>
            <input type="file" id="wallpaperInput" accept="image/*" style="display:none" onchange="uploadWallpaper(this.files[0])">
            <input type="file" id="musicInput" accept="audio/*" style="display:none" onchange="uploadMusic(this.files[0])">
        </div>
    </header>
    <div class="layout">
        <main class="main-col">
            <div class="sticky-nav surface">
                <div class="nav-row">
                    <button class="btn btn-secondary btn-nav" onclick="goHome()" title="Go to /sdcard root">
                        <svg class="ico-svg" viewBox="0 0 24 24"><path d="m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>
                        Home
                    </button>
                    <div class="grow">
                        <input type="text" id="searchBox" placeholder="Search files..." oninput="filterFiles()">
                    </div>
                    <button class="btn btn-secondary btn-nav scope-btn" id="scopeBtn" onclick="toggleScope()" title="Toggle search scope">🌐</button>
                    <select id="sortField" class="nav-select" onchange="applySort(true)" title="Sort by">
                        <option value="name">Name</option>
                        <option value="size">Size</option>
                        <option value="date">Date</option>
                        <option value="type">Type</option>
                    </select>
                    <select id="sortDir" class="nav-select" onchange="applySort(true)" title="Sort direction">
                        <option value="asc">↑</option>
                        <option value="desc">↓</option>
                    </select>
                    <button class="btn btn-secondary btn-nav" onclick="toggleSelectAll()" id="selectAllBtn">Select</button>
                    <button class="btn btn-secondary btn-nav" onclick="showNewFolderModal()">+Folder</button>
                    <button class="btn btn-nav" onclick="openFolderPicker()">Upload</button>
                </div>
                <div class="nav-path" id="mainPathBar">📂 /sdcard/__REL_PATH__</div>
            </div>
            <div class="card surface">
                <div class="storage-meter" id="storageMeter">
                    <span>__STORAGE_METER__</span>
                    <span>__STORAGE_PCT__%</span>
                </div>
                <div class="progress-track"><div class="progress-fill" id="storageFill"></div></div>
            </div>
            <div class="card surface">
                <ul id="fileList">
__PARENT_ROW__
__FILE_ROWS__
                    <li id="loadSentinel"></li>
                </ul>
            </div>
        </main>
        <aside class="sidebar">
            <div id="audioBar" class="surface">
                <div id="waveform"></div>
                <div class="audio-row">
                    <button class="audio-btn" id="audioMuteBtn" onclick="toggleMute()" title="Mute"></button>
                    <input type="range" id="audioVolume" class="volume-slider" min="0" max="100" value="80">
                    <span class="vol-label" id="volLabel">80%</span>
                </div>
            </div>

            <div class="card surface">
                <div class="sidebar-dropzone" id="dropzone">
                    <span class="drop-icon" id="dropIcon"></span>
                    <div>Drag &amp; drop files</div>
                    <div style="margin-top: 4px;">
                        <label>browse files
                            <input type="file" id="fileInput" multiple style="display:none"
                                   onchange="uploadSequential(this.files, UPLOAD_DEST || CURRENT_DIR)">
                        </label>
                    </div>
                    <div class="drop-hint" id="dropHint">Loading…</div>
                </div>
                <div id="uploadProgressContainer" style="display:none;">
                    <div class="storage-meter">
                        <span id="uploadStatusText">Uploading... 0%</span>
                        <span id="uploadMetricsText">0 MB/s</span>
                    </div>
                    <div class="progress-track" style="margin-bottom:0;"><div class="progress-fill" id="uploadBar" style="width:0%"></div></div>
                </div>
            </div>

            <div class="card surface" id="selectionCard" style="display: none;">
                <div class="selection-title"><span id="selectionCount">0</span> selected</div>
                <div class="selection-grid">
                    <button class="btn btn-sm" onclick="handleBulkDownloadAction()" id="downloadBtn">Download</button>
                    <button class="btn btn-secondary btn-sm" onclick="handleZipDownload()" id="zipBtn">ZIP</button>
                    <button class="btn btn-secondary btn-sm" onclick="renameItem()" id="renameBtn">Rename</button>
                    <button class="btn btn-secondary btn-sm" onclick="verifyChecksum()" id="checksumBtn">Hash</button>
                    <button class="btn btn-danger btn-sm" onclick="deleteSelected()">Delete</button>
                    <button class="btn btn-secondary btn-sm full-row" onclick="deselectAll()">Deselect all</button>
                </div>
            </div>

            <div id="batchBanner">
                <span class="grow" id="batchMsg">Ready</span>
                <div style="display: flex; gap: 8px;">
                    <button class="btn btn-sm" id="batchNextBtn" onclick="continueBatchDownload()" style="flex: 1;">Next batch</button>
                    <button class="btn btn-secondary btn-sm" onclick="cancelBatchDownload()">Cancel</button>
                </div>
            </div>
        </aside>
    </div>
</div>
<div id="modal" class="modal">
    <div class="modal-content" style="max-width: 460px;">
        <h3 id="modalTitle">Action</h3>
        <input type="text" id="modalInput" placeholder="Enter value..." style="margin-bottom:16px;">
        <div style="display:flex; gap:8px; justify-content:flex-end;">
            <button class="btn btn-secondary" onclick="closeModal('modal')">Cancel</button>
            <button class="btn" id="modalSubmit">Confirm</button>
        </div>
    </div>
</div>
<div id="mediaModal" class="modal">
    <div class="modal-content">
        <h3 id="mediaTitle">Preview</h3>
        <div id="mediaContainer"></div>
        <button class="btn btn-secondary" style="width:100%; margin-top:12px;" onclick="closeMediaModal()">Close</button>
    </div>
</div>
<div id="folderPickerModal" class="modal">
    <div class="modal-content" style="max-height:80vh; display:flex; flex-direction:column; max-width:500px;">
        <h3>Select Destination</h3>
        <div class="nav-path" id="pickerPath" style="margin-bottom:12px; padding:8px 12px; border:1px solid var(--border); border-radius:10px; background:var(--surface-3);">/sdcard</div>
        <div style="flex:1; overflow-y:auto; max-height:42vh; border:1px solid var(--border); border-radius:12px; margin-bottom:14px; background:var(--surface-3);">
            <ul id="pickerList" style="list-style:none; padding:6px; margin:0;"></ul>
        </div>
        <div style="display:flex; gap:8px; justify-content:flex-end;">
            <button class="btn btn-secondary" onclick="closeModal('folderPickerModal')">Cancel</button>
            <button class="btn" onclick="triggerPickerUpload()">Upload Here</button>
            <input type="file" id="pickerFileInput" multiple style="display:none" onchange="startPickerUpload(this.files)">
        </div>
    </div>
</div>
<div id="toastContainer"></div>

<script src="/static/fuse.js/dist/fuse.min.js"></script>
<script src="/static/tus-js-client/dist/tus.min.js"></script>
<script src="/static/dayjs/dayjs.min.js"></script>
<script src="/static/dayjs/plugin/relativeTime.js"></script>
<script src="/static/wavesurfer.js/dist/wavesurfer.min.js"></script>

<script>
const CHUNK_SIZE = __CHUNK_SIZE__;
const UPLOAD_DEST = __UPLOAD_DEST_JS__;
const PARALLEL_CHUNKS = 4;
const MAX_STAGGER = 15;
const RENDER_CHUNK = 40;
const DOWNLOAD_BATCH_SIZE = 10;
const MAX_ZIP_URL_CHARS = 30000;
let CURRENT_DIR = decodeURIComponent(window.location.pathname).replace(/^\//, '').replace(/\/$/, '');
let MUSIC_URL = __MUSIC_URL__;
let searchScope = (CURRENT_DIR === '') ? 'all' : 'here';

const LIBS = {
    fuse: typeof Fuse !== 'undefined',
    tus: typeof tus !== 'undefined' && tus.Upload,
    dayjs: typeof dayjs !== 'undefined',
    wavesurfer: typeof WaveSurfer !== 'undefined',
};
console.log('[defxult] libraries:', LIBS);
if (LIBS.dayjs && window.dayjs_plugin_relativeTime) dayjs.extend(window.dayjs_plugin_relativeTime);

const ICONS = {
    zap: '<svg class="ico-svg" viewBox="0 0 24 24"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>',
    'layout-grid': '<svg class="ico-svg" viewBox="0 0 24 24"><rect width="7" height="7" x="3" y="3" rx="1"/><rect width="7" height="7" x="14" y="3" rx="1"/><rect width="7" height="7" x="14" y="14" rx="1"/><rect width="7" height="7" x="3" y="14" rx="1"/></svg>',
    list: '<svg class="ico-svg" viewBox="0 0 24 24"><line x1="8" x2="21" y1="6" y2="6"/><line x1="8" x2="21" y1="12" y2="12"/><line x1="8" x2="21" y1="18" y2="18"/><line x1="3" x2="3.01" y1="6" y2="6"/><line x1="3" x2="3.01" y1="12" y2="12"/><line x1="3" x2="3.01" y1="18" y2="18"/></svg>',
    moon: '<svg class="ico-svg" viewBox="0 0 24 24"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/></svg>',
    sun: '<svg class="ico-svg" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/></svg>',
    play: '<svg class="ico-svg" viewBox="0 0 24 24"><polygon points="6 3 20 12 6 21 6 3"/></svg>',
    pause: '<svg class="ico-svg" viewBox="0 0 24 24"><rect width="4" height="16" x="6" y="4" rx="1"/><rect width="4" height="16" x="14" y="4" rx="1"/></svg>',
    square: '<svg class="ico-svg" viewBox="0 0 24 24"><rect width="18" height="18" x="3" y="3" rx="2"/></svg>',
    'volume-2': '<svg class="ico-svg" viewBox="0 0 24 24"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/></svg>',
    'volume-x': '<svg class="ico-svg" viewBox="0 0 24 24"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="22" x2="16" y1="9" y2="15"/><line x1="16" x2="22" y1="9" y2="15"/></svg>',
    'volume-1': '<svg class="ico-svg" viewBox="0 0 24 24"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>',
    'file-audio': '<svg class="ico-svg" viewBox="0 0 24 24"><path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"/><polyline points="14 2 14 8 20 8"/><circle cx="10" cy="17" r="2"/><path d="M12 17V9l3-1"/></svg>',
    image: '<svg class="ico-svg" viewBox="0 0 24 24"><rect width="18" height="18" x="3" y="3" rx="2" ry="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/></svg>',
    'cloud-upload': '<svg class="ico-svg" viewBox="0 0 24 24"><path d="M12 13v8"/><path d="M4 14.899A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 2.5 8.242"/><path d="m8 17 4-4 4 4"/></svg>',
    'maximize-2': '<svg class="ico-svg" viewBox="0 0 24 24"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" x2="14" y1="3" y2="10"/><line x1="3" x2="10" y1="21" y2="14"/></svg>',
};

function applyIcons() {
    const bm = document.getElementById('brandMark');
    if (bm) bm.innerHTML = ICONS.zap;
    updateViewIcon();
    updateThemeIcon();
    updateVibeIcon();
    const musicUp = document.querySelector('button[onclick="triggerMusicUpload()"]');
    if (musicUp) musicUp.innerHTML = ICONS['file-audio'];
    const wallUp = document.querySelector('button[onclick="triggerWallpaperUpload()"]');
    if (wallUp) wallUp.innerHTML = ICONS.image;
    const di = document.getElementById('dropIcon');
    if (di) di.innerHTML = ICONS['cloud-upload'];
    updateMuteIcon();
}
function updateViewIcon() {
    const btn = document.getElementById('viewBtn');
    if (!btn) return;
    const isGrid = document.getElementById('fileList').classList.contains('grid');
    btn.innerHTML = isGrid ? ICONS.list : ICONS['layout-grid'];
}
function updateThemeIcon() {
    const btn = document.getElementById('themeBtn');
    if (!btn) return;
    const t = document.documentElement.dataset.theme || 'dark';
    btn.innerHTML = t === 'dark' ? ICONS.moon : ICONS.sun;
}
function updateVibeIcon() {
    const btn = document.getElementById('vibeBtn');
    if (!btn) return;
    const playing = (wsSidebar && wsSidebar.isPlaying && wsSidebar.isPlaying()) ||
                    (customAudio && !customAudio.paused) ||
                    isPlayingBeat;
    btn.innerHTML = playing ? ICONS.square : ICONS.play;
}
function updateMuteIcon() {
    const btn = document.getElementById('audioMuteBtn');
    if (!btn) return;
    if (muted || lastVolume === 0) btn.innerHTML = ICONS['volume-x'];
    else if (lastVolume < 0.5) btn.innerHTML = ICONS['volume-1'];
    else btn.innerHTML = ICONS['volume-2'];
}

function toast(msg, type = 'info', duration = 3200) {
    const el = document.createElement('div');
    el.className = 'toast ' + type;
    const text = document.createElement('span');
    text.className = 'toast-text';
    text.textContent = msg;
    el.appendChild(text);
    const closeBtn = document.createElement('button');
    closeBtn.className = 'toast-close';
    closeBtn.setAttribute('aria-label', 'Dismiss');
    closeBtn.textContent = '✕';
    closeBtn.addEventListener('click', (ev) => { ev.stopPropagation(); dismissToast(el); });
    el.appendChild(closeBtn);
    const container = document.getElementById('toastContainer');
    container.appendChild(el);
    requestAnimationFrame(() => el.classList.add('show'));
    el._timer = setTimeout(() => dismissToast(el), duration);
    return el;
}
function dismissToast(el) {
    if (!el || el._dismissed) return;
    el._dismissed = true;
    if (el._timer) clearTimeout(el._timer);
    el.classList.remove('show');
    setTimeout(() => el.remove(), 450);
}

window.addEventListener('error', e => {
    if (!e.message) return;
    if (/ResizeObserver loop|Script error\.?$/i.test(e.message)) return;
    console.error('[defxult] uncaught:', e.message);
});
window.addEventListener('unhandledrejection', e => {
    const r = e.reason;
    if (r && r._toastShown) return;
    const name = (r && r.name) || '';
    const msg = (r && (r.message || (r.toString && r.toString()))) || 'Unknown error';
    if (name === 'AbortError' || /aborted/i.test(msg)) return;
    console.error('[defxult] unhandled rejection:', r);
});

const _nativeFetch = window.fetch.bind(window);
window.fetch = async function(input, init) {
    let res;
    try {
        res = await _nativeFetch(input, init);
    } catch (err) {
        const name = (err && err.name) || '';
        const msg = (err && err.message) || String(err);
        if (name === 'AbortError' || /aborted|signal is aborted/i.test(msg)) {
            throw err;
        }
        const e = new Error(msg);
        e._toastShown = true;
        console.error('[defxult] network error:', err);
        toast('Network error: ' + msg, 'error', 7000);
        throw e;
    }
    if (!res.ok && res.status !== 401) {
        const clone = res.clone();
        let msg = 'HTTP ' + res.status;
        try {
            const ct = (clone.headers.get('content-type') || '').toLowerCase();
            if (ct.includes('json')) {
                const d = await clone.json();
                if (d && d.error) msg = d.error;
            } else {
                const t = (await clone.text()).trim().slice(0, 200);
                if (t) msg = t;
            }
        } catch (_) {}
        toast(msg, 'error', 7000);
    }
    return res;
};

document.addEventListener('error', e => {
    const t = e.target;
    if (!t || t.tagName !== 'IMG') return;
    if (!t.classList || !t.classList.contains('file-thumb')) return;
    if (!t.parentNode) return;
    const fb = t.getAttribute('data-fallback') || '📄';
    try {
        const span = document.createElement('span');
        span.className = 'file-ico';
        span.textContent = fb;
        t.parentNode.replaceChild(span, t);
    } catch (_) {}
}, true);

function toggleTheme() {
    const cur = document.documentElement.dataset.theme || 'dark';
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('defxult_theme', next); } catch(e) {}
    updateThemeIcon();
    document.querySelector('meta[name="theme-color"]').content = next === 'dark' ? '#0a0b12' : '#eef1f8';
}
(function initTheme() {
    let t = 'dark';
    try { t = localStorage.getItem('defxult_theme') || 'dark'; } catch(e) {}
    document.documentElement.dataset.theme = t;
    document.querySelector('meta[name="theme-color"]').content = t === 'dark' ? '#0a0b12' : '#eef1f8';
})();

function toggleView() {
    const list = document.getElementById('fileList');
    const btn = document.getElementById('viewBtn');
    const isGrid = list.classList.toggle('grid');
    btn.classList.toggle('active', isGrid);
    try { localStorage.setItem('defxult_view', isGrid ? 'grid' : 'list'); } catch(e) {}
    updateViewIcon();
}
(function initView() {
    let v = 'list';
    try { v = localStorage.getItem('defxult_view') || 'list'; } catch(e) {}
    if (v === 'grid') {
        document.getElementById('fileList').classList.add('grid');
        document.getElementById('viewBtn').classList.add('active');
    }
})();

function toggleScope() {
    searchScope = searchScope === 'all' ? 'here' : 'all';
    updateScopeBtn();
    filterFiles();
}
function updateScopeBtn() {
    const btn = document.getElementById('scopeBtn');
    if (searchScope === 'all') { btn.classList.add('active'); btn.title = 'Search: all folders'; }
    else { btn.classList.remove('active'); btn.title = 'Search: this folder'; }
}
updateScopeBtn();

document.addEventListener('click', e => {
    const btn = e.target.closest('.btn');
    if (!btn) return;
    const r = btn.getBoundingClientRect();
    const rip = document.createElement('span');
    rip.className = 'ripple';
    rip.style.left = (e.clientX - r.left) + 'px';
    rip.style.top = (e.clientY - r.top) + 'px';
    btn.appendChild(rip);
    setTimeout(() => rip.remove(), 650);
});

let audioCtx = null;
let customAudio = null;
let isPlayingBeat = false;
let loopInterval = null;
let beatGain = null;
let muted = false;
let lastVolume = parseFloat(localStorage.getItem('defxult_volume') || '0.8');
if (isNaN(lastVolume) || lastVolume < 0) lastVolume = 0.8;
if (lastVolume > 1) lastVolume = 1;

let wsSidebar = null;

function ensureAudioContext() {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === 'suspended') audioCtx.resume();
    return audioCtx;
}
function initSidebarWaveform() {
    if (!LIBS.wavesurfer || !MUSIC_URL) return;
    if (wsSidebar) { try { wsSidebar.destroy(); } catch(e) {} wsSidebar = null; }
    const container = document.getElementById('waveform');
    if (!container) return;
    container.innerHTML = '';
    try {
        wsSidebar = WaveSurfer.create({
            container: container,
            waveColor: 'rgba(100, 210, 255, 0.35)',
            progressColor: '#64d2ff',
            cursorColor: 'transparent',
            height: 44, barWidth: 2, barGap: 1, barRadius: 2,
            normalize: true, interact: true, url: MUSIC_URL,
        });
        wsSidebar.setVolume(muted ? 0 : lastVolume);
        wsSidebar.on('play', () => updateVibeIcon());
        wsSidebar.on('pause', () => updateVibeIcon());
        wsSidebar.on('finish', () => updateVibeIcon());
    } catch(e) { console.warn('wavesurfer init failed', e); wsSidebar = null; }
}
function destroySidebarWaveform() {
    if (wsSidebar) { try { wsSidebar.destroy(); } catch(e) {} wsSidebar = null; }
    const container = document.getElementById('waveform');
    if (container) container.innerHTML = '';
}
function applyVolume(v) {
    lastVolume = v;
    if (wsSidebar) wsSidebar.setVolume(v);
    if (customAudio) customAudio.volume = v;
    if (beatGain) beatGain.gain.value = v;
    try { localStorage.setItem('defxult_volume', String(v)); } catch(e) {}
    updateVolumeUI();
}
function updateVolumeUI() {
    const pct = Math.round(lastVolume * 100);
    const label = document.getElementById('volLabel');
    if (label) label.textContent = pct + '%';
    const slider = document.getElementById('audioVolume');
    if (slider) slider.value = pct;
    updateMuteIcon();
}
function toggleMute() {
    muted = !muted;
    const target = muted ? 0 : lastVolume;
    if (wsSidebar) wsSidebar.setVolume(target);
    if (customAudio) customAudio.volume = target;
    if (beatGain) beatGain.gain.value = target;
    updateVolumeUI();
}
document.getElementById('audioVolume').addEventListener('input', e => {
    const v = parseInt(e.target.value) / 100;
    muted = v === 0;
    applyVolume(v);
});

function stopMusic() {
    if (wsSidebar) { try { wsSidebar.pause(); } catch(e) {} }
    if (customAudio) { customAudio.pause(); customAudio = null; }
    if (loopInterval) clearInterval(loopInterval);
    if (beatGain) { try { beatGain.disconnect(); } catch(e) {} beatGain = null; }
    isPlayingBeat = false;
    updateVibeIcon();
}

function toggleVibe() {
    if (MUSIC_URL && wsSidebar) { wsSidebar.playPause(); return; }
    if (MUSIC_URL && LIBS.wavesurfer) {
        initSidebarWaveform();
        setTimeout(() => { if (wsSidebar) wsSidebar.play(); }, 300);
        return;
    }
    if (MUSIC_URL) {
        if (!customAudio) {
            customAudio = new Audio(MUSIC_URL);
            customAudio.loop = true;
            customAudio.volume = muted ? 0 : lastVolume;
        }
        if (customAudio.paused) {
            customAudio.play().then(updateVibeIcon).catch(err => toast('Playback failed: ' + err.message, 'error'));
        } else { customAudio.pause(); updateVibeIcon(); }
        return;
    }
    if (isPlayingBeat) { stopMusic(); return; }
    ensureAudioContext();
    beatGain = audioCtx.createGain();
    beatGain.gain.value = muted ? 0 : lastVolume;
    beatGain.connect(audioCtx.destination);
    isPlayingBeat = true;
    updateVibeIcon();
    const chords = [[185.00,220.00,277.18],[164.81,207.65,246.94],[130.81,164.81,196.00],[146.83,185.00,220.00]];
    let step = 0, chordIndex = 0;
    loopInterval = setInterval(() => {
        if (!isPlayingBeat || !beatGain) return;
        const t = audioCtx.currentTime;
        if (step % 2 === 0) {
            const osc = audioCtx.createOscillator(); const g = audioCtx.createGain();
            osc.type = 'sine';
            osc.frequency.setValueAtTime(chords[chordIndex][0] / 2, t);
            g.gain.setValueAtTime(0.3, t);
            g.gain.exponentialRampToValueAtTime(0.001, t + 0.6);
            osc.connect(g); g.connect(beatGain); osc.start(t); osc.stop(t + 0.6);
        }
        if (step % 4 === 0) {
            chords[chordIndex].forEach(freq => {
                const osc = audioCtx.createOscillator(); const g = audioCtx.createGain(); const flt = audioCtx.createBiquadFilter();
                osc.type = 'triangle';
                osc.frequency.setValueAtTime(freq, t);
                flt.type = 'lowpass'; flt.frequency.setValueAtTime(450, t);
                g.gain.setValueAtTime(0.001, t);
                g.gain.linearRampToValueAtTime(0.08, t + 0.4);
                g.gain.exponentialRampToValueAtTime(0.0001, t + 1.8);
                osc.connect(flt); flt.connect(g); g.connect(beatGain);
                osc.start(t); osc.stop(t + 2.0);
            });
            if (step % 16 === 0) chordIndex = (chordIndex + 1) % chords.length;
        }
        if (step % 2 !== 0) {
            const bufSize = audioCtx.sampleRate * 0.05;
            const nb = audioCtx.createBuffer(1, bufSize, audioCtx.sampleRate);
            const out = nb.getChannelData(0);
            for (let i = 0; i < bufSize; i++) out[i] = Math.random() * 2 - 1;
            const src = audioCtx.createBufferSource();
            src.buffer = nb;
            const flt = audioCtx.createBiquadFilter();
            flt.type = 'highpass'; flt.frequency.value = 5000;
            const g = audioCtx.createGain();
            g.gain.setValueAtTime(0.03, t);
            g.gain.exponentialRampToValueAtTime(0.001, t + 0.04);
            src.connect(flt); flt.connect(g); g.connect(beatGain);
            src.start(t);
        }
        step++;
    }, 350);
}

function triggerMusicUpload() { document.getElementById('musicInput').click(); }
async function uploadMusic(file) {
    if (!file) return;
    const fd = new FormData();
    fd.append('file', file);
    const res = await fetch('/api/music', {method: 'POST', body: fd});
    if (res.ok) {
        toast('Music saved', 'success');
        stopMusic();
        destroySidebarWaveform();
        await navigateTo(CURRENT_DIR, false);
    }
}

/* ---------- MEDIA PREVIEW ---------- */
let mediaEl = null;
let videoPlaylist = [];
let videoPlaylistIndex = 0;
let audioPlaylist = [];
let audioPlaylistIndex = 0;
let imagePlaylist = [];
let imagePlaylistIndex = 0;
let modalMode = null;
let videoTitleRef = null;
let videoCounterRef = null;
let videoLoadingRef = null;
let musicWasPlaying = false;

function escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
}
function fmtTime(s) {
    if (!isFinite(s) || isNaN(s)) return '0:00';
    const m = Math.floor(s / 60);
    const sec = Math.floor(s % 60);
    return m + ':' + (sec < 10 ? '0' : '') + sec;
}
function updateVmMuteIcon(video, btn) {
    if (!btn) return;
    if (video.muted || video.volume === 0) btn.innerHTML = ICONS['volume-x'];
    else if (video.volume < 0.5) btn.innerHTML = ICONS['volume-1'];
    else btn.innerHTML = ICONS['volume-2'];
}

function previewMedia() {
    const el = firstSelectedEl();
    if (!el) return;
    openPreviewForEl(el);
}

function openPreviewForEl(el) {
    if (!el) return;
    const p = liPath(el);
    if (!p) return;
    const name = liName(el).toLowerCase();
    if (/\.(png|jpg|jpeg|webp|gif|bmp)$/.test(name)) { openImagePreview(p, el); return; }
    if (/\.(mp3|wav|ogg|m4a|flac|opus|aac)$/.test(name)) { openAudioModal(p, el); return; }
    if (/\.(mp4|webm|mov|mkv|m4v|avi|3gp)$/.test(name)) { openVideoModal(p, el); return; }
    toast('Preview not supported for this file type', 'warn');
}

function buildPlaylist(regex, clickedPath, clickedName) {
    const list = [];
    let idx = 0, found = false;
    displayEls.forEach(row => {
        const p = liPath(row);
        const name = liName(row);
        if (p && regex.test(name.toLowerCase())) {
            if (p === clickedPath) { idx = list.length; found = true; }
            list.push({ path: p, name: name });
        }
    });
    if (!found) {
        return { list: [{ path: clickedPath, name: clickedName || clickedPath.split('/').pop() }], idx: 0 };
    }
    return { list: list, idx: Math.max(0, idx) };
}

/* ---------- Image modal ---------- */
function openImagePreview(clickedPath, clickedEl) {
    const { list, idx } = buildPlaylist(
        /\.(png|jpg|jpeg|webp|gif|bmp)$/,
        clickedPath,
        liName(clickedEl)
    );
    imagePlaylist = list;
    imagePlaylistIndex = idx;
    modalMode = 'image';
    renderImageModal();
    openModal('mediaModal');
}

function renderImageModal() {
    const item = imagePlaylist[imagePlaylistIndex];
    if (!item) return;

    if (mediaEl && mediaEl.tagName === 'IMG') {
        try { mediaEl.removeAttribute('src'); } catch(e) {}
    }
    mediaEl = null;

    const container = document.getElementById('mediaContainer');
    container.innerHTML = '';

    const layout = document.createElement('div');
    layout.className = 'img-stage';
    layout.innerHTML =
        '<button class="nav-arrow small" id="imgPrevBtn" title="Previous (←)">‹</button>' +
        '<div class="img-main">' +
            '<div class="img-name" id="imgName"></div>' +
            '<img id="imgViewer" alt="">' +
        '</div>' +
        '<button class="nav-arrow small" id="imgNextBtn" title="Next (→)">›</button>';
    container.appendChild(layout);

    const nameEl = layout.querySelector('#imgName');
    if (nameEl) nameEl.textContent = item.name;
    document.getElementById('mediaTitle').textContent =
        'Image  ·  ' + (imagePlaylistIndex + 1) + ' / ' + imagePlaylist.length;

    const img = layout.querySelector('#imgViewer');
    img.src = item.path;
    img.onerror = () => toast('Image failed to load', 'error');
    mediaEl = img;

    const prevBtn = layout.querySelector('#imgPrevBtn');
    const nextBtn = layout.querySelector('#imgNextBtn');
    const disableNav = imagePlaylist.length < 2;
    prevBtn.disabled = disableNav;
    nextBtn.disabled = disableNav;
    prevBtn.addEventListener('click', () => imageNav(-1));
    nextBtn.addEventListener('click', () => imageNav(1));
}

function imageNav(delta) {
    if (imagePlaylist.length < 2) return;
    imagePlaylistIndex = (imagePlaylistIndex + delta + imagePlaylist.length) % imagePlaylist.length;
    renderImageModal();
}

/* ---------- Video modal — native <video> + floating glass controls ---------- */
function pauseSidebarMusicForVideo() {
    if (wsSidebar && wsSidebar.isPlaying && wsSidebar.isPlaying()) {
        musicWasPlaying = true;
        try { wsSidebar.pause(); } catch(e) {}
    } else if (customAudio && !customAudio.paused) {
        musicWasPlaying = true;
        customAudio.pause();
    }
}
function resumeSidebarMusicIfWasPlaying() {
    if (!musicWasPlaying) return;
    musicWasPlaying = false;
    if (wsSidebar) { try { wsSidebar.play(); } catch(e) {} }
    else if (customAudio) { try { customAudio.play(); } catch(e) {} }
}

function openVideoModal(path, el) {
    const { list, idx } = buildPlaylist(/\.(mp4|webm|mov|mkv|m4v|avi|3gp)$/, path, liName(el));
    videoPlaylist = list;
    videoPlaylistIndex = idx;
    modalMode = 'video';
    pauseSidebarMusicForVideo();
    renderVideoModal();
    openModal('mediaModal');
}

function renderVideoModal() {
    const item = videoPlaylist[videoPlaylistIndex];
    if (!item) return;

    if (mediaEl && mediaEl.tagName === 'VIDEO') {
        try { mediaEl.pause(); mediaEl.removeAttribute('src'); mediaEl.load(); } catch(e) {}
    }
    mediaEl = null;

    const container = document.getElementById('mediaContainer');
    container.innerHTML = '';

    const stage = document.createElement('div');
    stage.className = 'vm-stage';
    stage.innerHTML =
        '<button class="vm-arrow" id="vmPrevBtn" title="Previous (←)">‹</button>' +
        '<div class="vm-column">' +
            '<div class="vm-frame" id="vmFrame">' +
                '<video class="vm-video" id="vmVideo" playsinline preload="auto"></video>' +
                '<div class="vm-glass-top">' +
                    '<span class="vm-title" id="vmTitle"></span>' +
                    '<span class="vm-counter" id="vmCounter"></span>' +
                '</div>' +
                '<button class="vm-center-play" id="vmCenterPlay" title="Play/Pause">' + ICONS.play + '</button>' +
                '<div class="vm-glass-bottom" id="vmBottom">' +
                    '<div class="vm-seek-row">' +
                        '<input type="range" class="vm-seek" id="vmSeek" min="0" max="1000" value="0" step="1">' +
                        '<span class="vm-time" id="vmTime">0:00 / 0:00</span>' +
                    '</div>' +
                    '<div class="vm-btn-row">' +
                        '<button class="vm-glass-btn" id="vmBack10" title="Back 10s (J)">⏪</button>' +
                        '<button class="vm-glass-btn" id="vmFwd10" title="Forward 10s (L)">⏩</button>' +
                        '<div class="vm-spacer"></div>' +
                        '<div class="vm-vol-wrap">' +
                            '<button class="vm-glass-btn" id="vmMute" title="Mute">' + ICONS['volume-2'] + '</button>' +
                            '<input type="range" class="vm-vol" id="vmVol" min="0" max="100" value="' +
                                Math.round((muted ? 0 : lastVolume) * 100) + '">' +
                        '</div>' +
                        '<select class="vm-speed" id="vmSpeed" title="Playback speed">' +
                            '<option value="0.5">0.5×</option>' +
                            '<option value="0.75">0.75×</option>' +
                            '<option value="1" selected>1×</option>' +
                            '<option value="1.25">1.25×</option>' +
                            '<option value="1.5">1.5×</option>' +
                            '<option value="2">2×</option>' +
                        '</select>' +
                        '<button class="vm-glass-btn" id="vmFs" title="Fullscreen">' + ICONS['maximize-2'] + '</button>' +
                    '</div>' +
                '</div>' +
                '<div class="vm-loading" id="vmLoading"><div class="vm-loading-ring"></div></div>' +
            '</div>' +
        '</div>' +
        '<button class="vm-arrow" id="vmNextBtn" title="Next (→)">›</button>';
    container.appendChild(stage);

    const frame = stage.querySelector('#vmFrame');
    const video = stage.querySelector('#vmVideo');
    const loadingEl = stage.querySelector('#vmLoading');
    const titleEl = stage.querySelector('#vmTitle');
    const counterEl = stage.querySelector('#vmCounter');
    const prevBtn = stage.querySelector('#vmPrevBtn');
    const nextBtn = stage.querySelector('#vmNextBtn');
    const centerPlayBtn = stage.querySelector('#vmCenterPlay');
    const seek = stage.querySelector('#vmSeek');
    const timeEl = stage.querySelector('#vmTime');
    const muteBtn = stage.querySelector('#vmMute');
    const volSlider = stage.querySelector('#vmVol');
    const speedSel = stage.querySelector('#vmSpeed');
    const fsBtn = stage.querySelector('#vmFs');

    videoTitleRef = titleEl;
    videoCounterRef = counterEl;
    videoLoadingRef = loadingEl;
    mediaEl = video;

    video.volume = muted ? 0 : lastVolume;
    video.muted = muted;
    updateVmMuteIcon(video, muteBtn);

    const disableNav = videoPlaylist.length < 2;
    prevBtn.disabled = disableNav;
    nextBtn.disabled = disableNav;
    prevBtn.addEventListener('click', () => videoNav(-1));
    nextBtn.addEventListener('click', () => videoNav(1));

    updateVideoHeader();

    let controlsTimer = null;
    const showControlsTransient = () => {
        frame.classList.add('controls-visible');
        if (controlsTimer) clearTimeout(controlsTimer);
        if (!video.paused) {
            controlsTimer = setTimeout(() => {
                frame.classList.remove('controls-visible');
            }, 2500);
        }
    };
    const showControlsPersistent = () => {
        frame.classList.add('controls-visible');
        if (controlsTimer) clearTimeout(controlsTimer);
    };
    frame.addEventListener('mousemove', showControlsTransient);
    frame.addEventListener('touchstart', showControlsTransient, {passive: true});
    showControlsPersistent();

    centerPlayBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        if (video.paused) video.play().catch(() => {}); else video.pause();
    });
    seek.addEventListener('input', () => {
        if (!isFinite(video.duration) || video.duration === 0) return;
        video.currentTime = (seek.value / 1000) * video.duration;
    });
    muteBtn.addEventListener('click', () => {
        video.muted = !video.muted;
        updateVmMuteIcon(video, muteBtn);
    });
    volSlider.addEventListener('input', () => {
        const val = parseInt(volSlider.value, 10) / 100;
        video.volume = val;
        video.muted = val === 0;
        updateVmMuteIcon(video, muteBtn);
    });
    speedSel.addEventListener('change', () => {
        video.playbackRate = parseFloat(speedSel.value);
    });
    fsBtn.addEventListener('click', () => {
        if (document.fullscreenElement) document.exitFullscreen?.();
        else if (video.requestFullscreen) video.requestFullscreen().catch(() => {});
        else if (video.webkitEnterFullscreen) video.webkitEnterFullscreen();
    });
    stage.querySelector('#vmBack10').addEventListener('click', () => {
        video.currentTime = Math.max(0, video.currentTime - 10);
    });
    stage.querySelector('#vmFwd10').addEventListener('click', () => {
        const d = isFinite(video.duration) ? video.duration : video.currentTime + 10;
        video.currentTime = Math.min(d, video.currentTime + 10);
    });

    video.addEventListener('play', () => {
        centerPlayBtn.innerHTML = ICONS.pause;
        centerPlayBtn.classList.add('is-playing');
        showControlsTransient();
    });
    video.addEventListener('pause', () => {
        centerPlayBtn.innerHTML = ICONS.play;
        centerPlayBtn.classList.remove('is-playing');
        showControlsPersistent();
    });
    video.addEventListener('ended', () => {
        if (videoPlaylistIndex < videoPlaylist.length - 1) videoNav(1);
    });
    video.addEventListener('timeupdate', () => {
        if (video !== mediaEl) return;
        if (!isFinite(video.duration) || video.duration === 0) return;
        if (document.activeElement !== seek) {
            seek.value = (video.currentTime / video.duration) * 1000;
        }
        timeEl.textContent = fmtTime(video.currentTime) + ' / ' + fmtTime(video.duration);
    });
    video.addEventListener('loadedmetadata', () => {
        timeEl.textContent = fmtTime(video.currentTime) + ' / ' + fmtTime(video.duration);
    });
    video.addEventListener('volumechange', () => {
        volSlider.value = Math.round((video.muted ? 0 : video.volume) * 100);
        updateVmMuteIcon(video, muteBtn);
    });
    video.addEventListener('click', () => {
        if (video.paused) video.play().catch(() => {}); else video.pause();
    });

    let transcodeAttempted = false;
    let watchdogDone = false;

    const showTranscode = () => {
        if (transcodeAttempted) return;
        transcodeAttempted = true;
        console.log('[defxult] native play failed — falling back to transcode:', item.name);
        try { video.pause(); } catch(e) {}
        if (loadingEl) loadingEl.classList.add('show');
        video.src = '/api/transcode?path=' + encodeURIComponent(item.path) + '&_n=' + Date.now();
        video.load();
        const p = video.play();
        if (p && p.catch) p.catch(() => {});
    };

    const clearLoading = () => {
        if (loadingEl) loadingEl.classList.remove('show');
    };
    video.addEventListener('loadeddata', clearLoading);
    video.addEventListener('canplay', clearLoading);
    video.addEventListener('playing', () => {
        clearLoading();
        if (watchdogDone) return;
        setTimeout(() => {
            if (watchdogDone) return;
            if (video !== mediaEl) return;
            if (video.videoWidth > 0) { watchdogDone = true; return; }
            if (video.currentTime < 0.1) return;
            watchdogDone = true;
            console.warn('[defxult] videoWidth still 0 after 800ms — transcode');
            showTranscode();
        }, 800);
    });
    video.addEventListener('error', () => {
        if (video !== mediaEl) return;
        showTranscode();
    });

    video.src = item.path;
    loadingEl.classList.add('show');
    const p = video.play();
    if (p && p.catch) p.catch(() => { try { video.load(); } catch(e) {} });
}

function updateVideoHeader() {
    const item = videoPlaylist[videoPlaylistIndex];
    if (!item) return;
    if (videoTitleRef) videoTitleRef.textContent = item.name;
    if (videoCounterRef) {
        videoCounterRef.textContent = (videoPlaylistIndex + 1) + ' / ' + videoPlaylist.length;
    }
}

function videoNav(delta) {
    if (videoPlaylist.length < 2) return;
    const n = videoPlaylist.length;
    videoPlaylistIndex = (videoPlaylistIndex + delta + n) % n;
    renderVideoModal();
}

/* ---------- Audio modal — native <audio> ---------- */
function openAudioModal(path, el) {
    const { list, idx } = buildPlaylist(/\.(mp3|wav|ogg|m4a|flac|opus|aac)$/, path, liName(el));
    audioPlaylist = list;
    audioPlaylistIndex = idx;
    modalMode = 'audio';
    pauseSidebarMusicForVideo();
    renderAudioModal();
    openModal('mediaModal');
}

function renderAudioModal() {
    const item = audioPlaylist[audioPlaylistIndex];
    if (!item) return;

    if (mediaEl && mediaEl.tagName === 'AUDIO') {
        try { mediaEl.pause(); mediaEl.removeAttribute('src'); mediaEl.load(); } catch(e) {}
    }
    mediaEl = null;

    const container = document.getElementById('mediaContainer');
    container.innerHTML = '';

    const layout = document.createElement('div');
    layout.className = 'audio-stream-layout';
    layout.innerHTML =
        '<button class="nav-arrow small" id="audPrevBtn" title="Previous (←)">‹</button>' +
        '<div class="audio-stream-main">' +
            '<div class="audio-stream-name" id="audName"></div>' +
            '<audio id="audPlayer" controls preload="auto"></audio>' +
        '</div>' +
        '<button class="nav-arrow small" id="audNextBtn" title="Next (→)">›</button>';
    container.appendChild(layout);

    layout.querySelector('#audName').textContent = item.name;
    document.getElementById('mediaTitle').textContent =
        'Now Playing  ·  ' + (audioPlaylistIndex + 1) + ' / ' + audioPlaylist.length;

    const audio = layout.querySelector('#audPlayer');
    audio.volume = muted ? 0 : lastVolume;
    audio.src = item.path;
    mediaEl = audio;

    audio.addEventListener('ended', () => {
        if (audioPlaylistIndex < audioPlaylist.length - 1) audioNav(1);
    });
    audio.addEventListener('error', () => {
        console.warn('[defxult] audio error, falling back to transcode');
        audio.src = '/api/transcode?path=' + encodeURIComponent(item.path) + '&_n=' + Date.now();
        audio.load();
        audio.play().catch(() => {});
    });

    audio.play().catch(() => {});

    const prevBtn = layout.querySelector('#audPrevBtn');
    const nextBtn = layout.querySelector('#audNextBtn');
    const disableNav = audioPlaylist.length < 2;
    prevBtn.disabled = disableNav;
    nextBtn.disabled = disableNav;
    prevBtn.addEventListener('click', () => audioNav(-1));
    nextBtn.addEventListener('click', () => audioNav(1));
}

function audioNav(delta) {
    if (audioPlaylist.length < 2) return;
    audioPlaylistIndex = (audioPlaylistIndex + delta + audioPlaylist.length) % audioPlaylist.length;
    renderAudioModal();
}

function closeMediaModal() {
    if (mediaEl) {
        try { mediaEl.pause(); } catch (e) {}
        try {
            if (mediaEl.tagName === 'VIDEO' || mediaEl.tagName === 'AUDIO') {
                mediaEl.removeAttribute('src');
                mediaEl.load();
            }
        } catch(e) {}
    }
    mediaEl = null;
    videoTitleRef = null;
    videoCounterRef = null;
    videoLoadingRef = null;
    document.getElementById('mediaContainer').innerHTML = '';
    if (modalMode === 'video' || modalMode === 'audio') resumeSidebarMusicIfWasPlaying();
    modalMode = null;
    closeModal('mediaModal');
}

document.addEventListener('keydown', e => {
    const modal = document.getElementById('mediaModal');
    if (!modal || !modal.classList.contains('show')) return;
    if (e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT')) return;

    if (e.key === 'ArrowLeft') {
        if (modalMode === 'video') { e.preventDefault(); videoNav(-1); }
        else if (modalMode === 'audio') { e.preventDefault(); audioNav(-1); }
        else if (modalMode === 'image') { e.preventDefault(); imageNav(-1); }
    } else if (e.key === 'ArrowRight') {
        if (modalMode === 'video') { e.preventDefault(); videoNav(1); }
        else if (modalMode === 'audio') { e.preventDefault(); audioNav(1); }
        else if (modalMode === 'image') { e.preventDefault(); imageNav(1); }
    } else if (e.key === ' ' || e.key === 'k') {
        if (!mediaEl || !mediaEl.play) return;
        e.preventDefault();
        if (mediaEl.paused) mediaEl.play().catch(() => {}); else mediaEl.pause();
    } else if (e.key === 'j') {
        if (!mediaEl || typeof mediaEl.currentTime !== 'number') return;
        e.preventDefault();
        mediaEl.currentTime = Math.max(0, mediaEl.currentTime - 10);
    } else if (e.key === 'l') {
        if (!mediaEl || typeof mediaEl.currentTime !== 'number') return;
        e.preventDefault();
        mediaEl.currentTime = Math.min(mediaEl.duration || 0, mediaEl.currentTime + 10);
    } else if (e.key === 'Escape') {
        closeMediaModal();
    }
});

/* ---------- Hover preview — one persistent native <video> ---------- */
const HOVER_PREVIEW_DELAY = 550;
const HOVER_PREVIEW_SPEED = 8;
const HOVER_PREVIEW_W = 640;
const HOVER_PREVIEW_H = 360;

let hoverVideo = null;
let hoverBox = null;
let hoverLabelName = null;
let hoverPreviewDelayTimer = null;

function ensureHoverPreview() {
    if (hoverVideo && hoverBox) return;
    hoverBox = document.createElement('div');
    hoverBox.className = 'hover-preview';
    hoverBox.style.width = HOVER_PREVIEW_W + 'px';
    hoverBox.style.height = HOVER_PREVIEW_H + 'px';

    hoverVideo = document.createElement('video');
    hoverVideo.muted = true;
    hoverVideo.setAttribute('playsinline', '');
    hoverVideo.setAttribute('preload', 'metadata');
    hoverVideo.playbackRate = HOVER_PREVIEW_SPEED;
    hoverVideo.defaultPlaybackRate = HOVER_PREVIEW_SPEED;
    hoverBox.appendChild(hoverVideo);

    const label = document.createElement('div');
    label.className = 'hover-preview-label';
    const nameEl = document.createElement('span');
    nameEl.className = 'hpl-name';
    const speedEl = document.createElement('span');
    speedEl.className = 'hpl-speed';
    speedEl.textContent = HOVER_PREVIEW_SPEED + '×';
    label.appendChild(nameEl);
    label.appendChild(speedEl);
    hoverBox.appendChild(label);
    hoverLabelName = nameEl;

    document.body.appendChild(hoverBox);
}

function attachHoverPreview(li, videoPath, videoName) {
    li.addEventListener('mouseenter', () => {
        if (hoverPreviewDelayTimer) clearTimeout(hoverPreviewDelayTimer);
        hoverPreviewDelayTimer = setTimeout(() => {
            showHoverPreview(videoPath, videoName);
        }, HOVER_PREVIEW_DELAY);
    });
    li.addEventListener('mouseleave', () => {
        if (hoverPreviewDelayTimer) { clearTimeout(hoverPreviewDelayTimer); hoverPreviewDelayTimer = null; }
        hideHoverPreview();
    });
}

function showHoverPreview(path, name) {
    ensureHoverPreview();
    if (hoverLabelName) hoverLabelName.textContent = name;

    const modal = document.getElementById('mediaModal');
    if (modal && modal.classList.contains('show')) return;

    try { hoverVideo.pause(); } catch(e) {}
    try { hoverVideo.removeAttribute('src'); } catch(e) {}
    try { hoverVideo.src = path; } catch(e) {}
    try { hoverVideo.playbackRate = HOVER_PREVIEW_SPEED; } catch(e) {}

    requestAnimationFrame(() => {
        if (hoverBox) hoverBox.classList.add('show');
    });
    const p = hoverVideo.play();
    if (p && p.catch) p.catch(() => {});
}

function hideHoverPreview() {
    if (!hoverVideo || !hoverBox) return;
    try { hoverVideo.pause(); } catch(e) {}
    hoverBox.classList.remove('show');
}

function attachHoverPreviews() {
    document.querySelectorAll('#fileList li').forEach(li => {
        if (li._hoverInit) return;
        if (li.id === 'loadSentinel' || li.classList.contains('empty-row')) return;
        const cb = li.querySelector('.file-chk'); if (!cb) return;
        const path = cb.dataset.path;
        const name = cb.dataset.name || '';
        if (!path || !/\.(mp4|webm|mov|mkv|m4v|avi|3gp)$/i.test(name)) return;
        li._hoverInit = true;
        attachHoverPreview(li, path, name);
    });
}
window.addEventListener('scroll', hideHoverPreview, {passive: true});

function openModal(id) {
    hideHoverPreview();
    document.getElementById('mainContainer').classList.add('blur-bg');
    const m = document.getElementById(id);
    m.style.display = 'flex';
    setTimeout(() => m.classList.add('show'), 10);
}
function closeModal(id) {
    document.getElementById('mainContainer').classList.remove('blur-bg');
    const m = document.getElementById(id);
    m.classList.remove('show');
    setTimeout(() => m.style.display = 'none', 350);
}

const selectedPaths = new Set();

function liPath(li) {
    if (!li) return null;
    const cb = li.querySelector && li.querySelector('.file-chk');
    return (cb && cb.dataset && cb.dataset.path) ? cb.dataset.path : null;
}
function liName(li) {
    if (!li) return '';
    const cb = li.querySelector && li.querySelector('.file-chk');
    if (cb && cb.dataset && cb.dataset.name) return cb.dataset.name;
    return li.dataset.name || '';
}
function firstSelectedEl() {
    const path = selectedPaths.values().next().value;
    if (!path) return null;
    return allRowEls.find(r => liPath(r) === path) ||
           displayEls.find(r => liPath(r) === path) ||
           null;
}
function applySelectionToCheckbox(cb) {
    if (!cb.dataset.path) return;
    cb.checked = selectedPaths.has(cb.dataset.path);
}
function attachCheckboxListeners() {
    document.querySelectorAll('.file-chk').forEach(c => {
        if (c._listenerAttached) { applySelectionToCheckbox(c); return; }
        c._listenerAttached = true;
        c.addEventListener('change', e => {
            const path = e.target.dataset.path;
            if (!path) return;
            if (e.target.checked) selectedPaths.add(path);
            else selectedPaths.delete(path);
            updateActionBar();
        });
        applySelectionToCheckbox(c);
    });
}
function updateActionBar() {
    const count = selectedPaths.size;
    const card = document.getElementById('selectionCard');
    const countEl = document.getElementById('selectionCount');

    if (countEl) countEl.textContent = count;

    if (count > 0) {
        card.style.display = 'block';
        const el = firstSelectedEl();
        const name = el ? liName(el) : '';
        document.getElementById('checksumBtn').style.display =
            (count === 1 && !name.endsWith('/')) ? 'inline-flex' : 'none';
        document.getElementById('renameBtn').style.display =
            count === 1 ? 'inline-flex' : 'none';
        document.getElementById('downloadBtn').textContent =
            count === 1 ? 'Download' : `Download ${count}`;
        document.getElementById('zipBtn').textContent =
            count === 1 ? 'ZIP' : `ZIP ${count}`;
    } else {
        card.style.display = 'none';
    }
}
function toggleSelectAll() {
    const files = allRowEls.filter(el => liPath(el));
    if (files.length === 0) { toast('Nothing to select here', 'warn'); return; }
    const allSelected = files.every(el => selectedPaths.has(liPath(el)));
    if (allSelected) { files.forEach(el => selectedPaths.delete(liPath(el))); }
    else { files.forEach(el => selectedPaths.add(liPath(el))); }
    document.querySelectorAll('.file-chk').forEach(cb => {
        if (cb.dataset.path && cb.style.visibility !== 'hidden') {
            cb.checked = selectedPaths.has(cb.dataset.path);
        }
    });
    updateActionBar();
    updateSelectAllBtn();
}
function updateSelectAllBtn() {
    const files = allRowEls.filter(el => liPath(el));
    const btn = document.getElementById('selectAllBtn');
    if (!btn || files.length === 0) return;
    const allSelected = files.every(el => selectedPaths.has(liPath(el)));
    btn.textContent = allSelected ? 'Deselect' : 'Select';
}
function deselectAll() {
    selectedPaths.clear();
    document.querySelectorAll('.file-chk:checked').forEach(c => { c.checked = false; });
    updateActionBar();
    updateSelectAllBtn();
}

let allRowEls = [];
let displayEls = [];
let renderedCount = 0;
let sentinelObserver = null;
let scrollRaf = null;
let fuseIndex = null;

function parseRowHTML(html) {
    const temp = document.createElement('div');
    temp.innerHTML = html;
    return Array.from(temp.children);
}
function rebuildFuse() {
    if (!LIBS.fuse) { fuseIndex = null; return; }
    fuseIndex = new Fuse(allRowEls.map(el => ({
        el: el, name: el.dataset.name || '', type: el.dataset.type || '',
    })), { keys: ['name', 'type'], threshold: 0.4, ignoreLocation: true, minMatchCharLength: 2 });
}
function setupSentinelObserver() {
    if (sentinelObserver) { sentinelObserver.disconnect(); sentinelObserver = null; }
    const sentinel = document.getElementById('loadSentinel');
    if (!sentinel) return;
    sentinelObserver = new IntersectionObserver(entries => {
        for (const e of entries) {
            if (e.isIntersecting && renderedCount < displayEls.length) appendMoreRows();
        }
    }, {rootMargin: '800px 0px', threshold: 0});
    sentinelObserver.observe(sentinel);
}
window.addEventListener('scroll', () => {
    if (scrollRaf) return;
    scrollRaf = requestAnimationFrame(() => {
        scrollRaf = null;
        if (renderedCount >= displayEls.length) return;
        const sentinel = document.getElementById('loadSentinel');
        if (!sentinel) return;
        const rect = sentinel.getBoundingClientRect();
        if (rect.top < window.innerHeight + 800) appendMoreRows();
    });
}, {passive: true});

function appendMoreRows() {
    const list = document.getElementById('fileList');
    const sentinel = document.getElementById('loadSentinel');
    if (renderedCount >= displayEls.length) return;
    const end = Math.min(renderedCount + RENDER_CHUNK, displayEls.length);
    const frag = document.createDocumentFragment();
    for (let i = renderedCount; i < end; i++) frag.appendChild(displayEls[i]);
    if (sentinel) list.insertBefore(frag, sentinel);
    else list.appendChild(frag);
    renderedCount = end;
    attachCheckboxListeners();
    attachHoverPreviews();
    if (sentinel) {
        sentinel.style.display = (renderedCount >= displayEls.length) ? 'none' : 'block';
    }
}
function renderRows(sorted) {
    displayEls = sorted;
    renderedCount = 0;
    const list = document.getElementById('fileList');
    const sentinel = document.getElementById('loadSentinel');
    Array.from(list.children).forEach(ch => {
        if (ch.id === 'loadSentinel') return;
        if (ch.dataset.name === '..') return;
        ch.remove();
    });
    if (displayEls.length === 0) {
        const empty = document.createElement('li');
        empty.className = 'empty-row';
        empty.innerHTML = '<span style="color:var(--text-muted);width:100%;text-align:center;padding:22px;">No matches</span>';
        if (sentinel) list.insertBefore(empty, sentinel);
        else list.appendChild(empty);
        if (sentinel) sentinel.style.display = 'none';
        updateSelectAllBtn();
        return;
    }
    appendMoreRows();
    updateSelectAllBtn();
}

function applySort(save) {
    const field = document.getElementById('sortField').value;
    const dir = document.getElementById('sortDir').value;
    if (save) {
        try {
            localStorage.setItem('defxult_sort_field', field);
            localStorage.setItem('defxult_sort_dir', dir);
        } catch(e) {}
    }
    let source = allRowEls;
    const q = (document.getElementById('searchBox').value || '').trim();
    if (q && searchScope === 'here') {
        if (LIBS.fuse && fuseIndex) source = fuseIndex.search(q).map(r => r.item.el);
        else {
            const ql = q.toLowerCase();
            source = source.filter(el => (el.dataset.name || '').includes(ql));
        }
    }
    const sorted = source.slice().sort((a, b) => {
        const aDir = a.dataset.type === 'dir' ? 0 : 1;
        const bDir = b.dataset.type === 'dir' ? 0 : 1;
        if (aDir !== bDir) return aDir - bDir;
        let va, vb;
        switch (field) {
            case 'name': va = a.dataset.name || ''; vb = b.dataset.name || ''; break;
            case 'size': va = parseInt(a.dataset.size) || 0; vb = parseInt(b.dataset.size) || 0; break;
            case 'date': va = parseFloat(a.dataset.mtime) || 0; vb = parseFloat(b.dataset.mtime) || 0; break;
            case 'type':
                va = (a.dataset.type || '').toLowerCase();
                vb = (b.dataset.type || '').toLowerCase();
                if (va === vb) { va = a.dataset.name || ''; vb = b.dataset.name || ''; }
                break;
            default: return 0;
        }
        let cmp = typeof va === 'string' ? va.localeCompare(vb) : (va - vb);
        return dir === 'asc' ? cmp : -cmp;
    });
    renderRows(sorted);
    rebuildFuse();
    applyRelativeTimes();
}
(function initSort() {
    let f = 'name', d = 'asc';
    try {
        f = localStorage.getItem('defxult_sort_field') || 'name';
        d = localStorage.getItem('defxult_sort_dir') || 'asc';
    } catch(e) {}
    document.getElementById('sortField').value = f;
    document.getElementById('sortDir').value = d;
})();

function applyRelativeTimes() {
    if (!LIBS.dayjs) return;
    document.querySelectorAll('#fileList li[data-mtime]').forEach(li => {
        if (li.dataset.name === '..') return;
        const meta = li.querySelector('.file-meta');
        if (!meta) return;
        const mt = parseFloat(li.dataset.mtime) || 0;
        if (!mt) return;
        let rel;
        try { rel = dayjs(mt * 1000).fromNow(); } catch(e) { rel = ''; }
        if (!rel) return;
        let sizeEl = meta.querySelector('.size-part');
        if (!sizeEl) {
            const size = meta.textContent || '';
            meta.innerHTML = '';
            sizeEl = document.createElement('span');
            sizeEl.className = 'size-part';
            sizeEl.textContent = size;
            meta.appendChild(sizeEl);
        }
        let relEl = meta.querySelector('.rel-part');
        if (!relEl) {
            relEl = document.createElement('span');
            relEl.className = 'rel-part';
            relEl.style.marginLeft = '0.6em';
            relEl.style.opacity = '0.75';
            meta.appendChild(relEl);
        }
        relEl.textContent = '· ' + rel;
    });
}

let searchDebounce = null;
function filterFiles() {
    const q = document.getElementById('searchBox').value.trim();
    if (!q) {
        if (searchDebounce) { clearTimeout(searchDebounce); searchDebounce = null; }
        applySort(false);
        return;
    }
    if (searchScope === 'all') {
        if (searchDebounce) clearTimeout(searchDebounce);
        searchDebounce = setTimeout(() => runRecursiveSearch(q), 280);
    } else {
        applySort(false);
    }
}
async function runRecursiveSearch(q) {
    if (document.getElementById('searchBox').value.trim() !== q) return;
    const params = new URLSearchParams({q: q, scope: 'all', dir: CURRENT_DIR});
    const res = await fetch('/api/search?' + params.toString());
    if (document.getElementById('searchBox').value.trim() !== q) return;
    const data = await res.json();
    if (!data.results || data.results.length === 0) { renderRows([]); return; }
    let results = data.results;
    if (LIBS.fuse) {
        const fx = new Fuse(results, { keys: ['name'], threshold: 0.4, ignoreLocation: true });
        const refined = fx.search(q).map(r => r.item);
        if (refined.length > 0) results = refined;
    }
    results.sort((a, b) => {
        const ad = a.is_dir ? 0 : 1; const bd = b.is_dir ? 0 : 1;
        if (ad !== bd) return ad - bd;
        return (a.name || '').localeCompare(b.name || '');
    });
    const els = results.map((item, idx) => buildResultRow(item, idx));
    renderRows(els);
    if (data.truncated) toast('Showing first 500 matches', 'info', 4000);
}

function iconFor(name, isDir) {
    if (isDir) return '📁';
    const ext = (name.match(/\.[^.]+$/) || [''])[0].toLowerCase();
    if (['.mp4','.webm','.mov','.mkv','.avi','.m4v','.3gp'].includes(ext)) return '🎬';
    if (['.png','.jpg','.jpeg','.webp','.gif','.bmp'].includes(ext)) return '🖼️';
    if (['.mp3','.wav','.ogg','.m4a','.flac','.opus','.aac'].includes(ext)) return '🎵';
    if (['.zip','.tar','.gz','.7z','.rar'].includes(ext)) return '🗜️';
    if (ext === '.pdf') return '📕';
    if (['.txt','.md','.log'].includes(ext)) return '📝';
    return '📄';
}
function formatSize(s) {
    if (!s) return '';
    const units = ['B', 'KB', 'MB', 'GB'];
    let n = s;
    for (const u of units) {
        if (n < 1024) return n.toFixed(1) + ' ' + u;
        n /= 1024;
    }
    return n.toFixed(1) + ' TB';
}
function buildResultRow(item, idx) {
    const li = document.createElement('li');
    const relUrl = '/' + item.rel.split('/').map(encodeURIComponent).join('/') + (item.is_dir ? '/' : '');
    const ftype = item.is_dir ? 'dir' : ((item.name.match(/\.([^.]+)$/) || ['', ''])[1] || '').toLowerCase();
    li.dataset.name = item.name.toLowerCase();
    li.dataset.size = item.size;
    li.dataset.mtime = Math.floor(item.mtime || 0);
    li.dataset.type = ftype;
    li.style.setProperty('--i', Math.min(idx + 1, MAX_STAGGER));
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.className = 'file-chk';
    cb.dataset.path = relUrl;
    cb.dataset.name = item.name;
    cb._listenerAttached = true;
    cb.addEventListener('change', e => {
        if (e.target.checked) selectedPaths.add(relUrl);
        else selectedPaths.delete(relUrl);
        updateActionBar();
    });
    cb.checked = selectedPaths.has(relUrl);
    const a = document.createElement('a');
    a.className = 'file-info';
    a.href = relUrl;
    const ico = document.createElement('span');
    ico.className = 'file-ico';
    ico.textContent = iconFor(item.name, item.is_dir);
    const nameSpan = document.createElement('span');
    nameSpan.className = 'file-name';
    nameSpan.textContent = item.is_dir ? item.name + '/' : item.name;
    const meta = document.createElement('span');
    meta.className = 'file-meta';
    const parentPath = item.rel.includes('/') ? item.rel.substring(0, item.rel.lastIndexOf('/')) : '';
    meta.textContent = (parentPath ? parentPath + ' · ' : '') + (item.is_dir ? '' : formatSize(item.size));
    a.appendChild(ico); a.appendChild(nameSpan); a.appendChild(meta);
    li.appendChild(cb); li.appendChild(a);
    return li;
}

let navSeq = 0;
function relUrlForDir(relPath) {
    if (!relPath) return '/';
    return '/' + relPath.split('/').map(encodeURIComponent).join('/') + '/';
}
function applyBgOverride(bgOverride) {
    const style = document.getElementById('wallpaperStyle');
    style.textContent = bgOverride
        ? 'body { background-image: ' + bgOverride + ' var(--bg-gradient); }'
        : '';
}
function goHome() {
    if (CURRENT_DIR === '') navigateTo('', false);
    else navigateTo('', true);
}
async function navigateTo(relPath, pushState = true) {
    const mySeq = ++navSeq;
    const list = document.getElementById('fileList');

    try { closeMediaModal(); } catch(e) {}
    hideHoverPreview();

    selectedPaths.clear();
    updateActionBar();
    cancelBatchDownload();

    list.classList.add('nav-out');
    const fadePromise = new Promise(r => setTimeout(r, 90));
    const fetchPromise = fetch('/api/list?dir=' + encodeURIComponent(relPath));

    let data;
    try {
        const [_, res] = await Promise.all([fadePromise, fetchPromise]);
        if (!res.ok) { list.classList.remove('nav-out'); return; }
        data = await res.json();
    } catch (e) {
        list.classList.remove('nav-out');
        return;
    }
    if (mySeq !== navSeq) return;

    list.innerHTML = '';
    if (data.parent_row) list.appendChild(parseRowHTML(data.parent_row)[0]);
    const sentinel = document.createElement('li');
    sentinel.id = 'loadSentinel';
    list.appendChild(sentinel);
    setupSentinelObserver();

    const rows = parseRowHTML(data.file_rows);
    if (rows.length === 1 && rows[0].classList.contains('empty-row')) {
        list.insertBefore(rows[0], sentinel);
        allRowEls = [];
        displayEls = [];
        renderedCount = 0;
        if (sentinel) sentinel.style.display = 'none';
    } else {
        allRowEls = rows;
        applySort(false);
    }

    document.getElementById('mainPathBar').textContent = '📂 /sdcard/' + data.rel_path;
    const sm = document.getElementById('storageMeter');
    sm.innerHTML = '<span>' + data.storage_meter + '</span>' +
                   '<span>' + data.storage_pct.toFixed(1) + '%</span>';
    document.getElementById('storageFill').style.width = data.storage_pct + '%';
    applyBgOverride(data.bg_override);

    const musicChanged = (data.music_url !== MUSIC_URL);
    if (musicChanged) {
        stopMusic();
        destroySidebarWaveform();
        MUSIC_URL = data.music_url;
    }
    const bar = document.getElementById('audioBar');
    if (MUSIC_URL) {
        bar.classList.add('show');
        updateVolumeUI();
        if (musicChanged || !wsSidebar) setTimeout(initSidebarWaveform, 50);
    } else {
        bar.classList.remove('show');
        destroySidebarWaveform();
    }

    CURRENT_DIR = relPath;
    document.getElementById('searchBox').value = '';
    if (searchDebounce) { clearTimeout(searchDebounce); searchDebounce = null; }

    attachCheckboxListeners();

    if (pushState) history.pushState({dir: relPath}, '', relUrlForDir(relPath));
    void list.offsetWidth;
    list.classList.remove('nav-out');
    list.classList.add('nav-in');
    setTimeout(() => list.classList.remove('nav-in'), 300);
    window.scrollTo({top: 0, behavior: 'smooth'});
}

(function initHistory() {
    const rel = decodeURIComponent(window.location.pathname).replace(/^\//, '').replace(/\/$/, '');
    history.replaceState({dir: rel}, '', window.location.pathname);
})();

document.addEventListener('click', e => {
    if (e.target.closest('.file-chk')) return;
    const link = e.target.closest('a.file-info');
    if (!link) return;
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;

    const href = link.getAttribute('href');
    if (!href) return;

    if (href.endsWith('/')) {
        e.preventDefault();
        const relPath = decodeURIComponent(href.replace(/^\//, '').replace(/\/$/, ''));
        navigateTo(relPath);
        return;
    }

    const li = link.closest('li');
    if (!li) return;
    const name = liName(li).toLowerCase();
    const isMedia =
        /\.(png|jpg|jpeg|webp|gif|bmp)$/i.test(name) ||
        /\.(mp3|wav|ogg|m4a|flac|opus|aac)$/i.test(name) ||
        /\.(mp4|webm|mov|mkv|m4v|avi|3gp)$/i.test(name);

    if (!isMedia) return;

    e.preventDefault();
    openPreviewForEl(li);
}, false);

window.addEventListener('popstate', e => {
    let rel;
    if (e.state && e.state.dir != null) rel = e.state.dir;
    else rel = decodeURIComponent(window.location.pathname).replace(/^\//, '').replace(/\/$/, '');
    navigateTo(rel, false);
});

function triggerWallpaperUpload() { document.getElementById('wallpaperInput').click(); }
async function uploadWallpaper(file) {
    if (!file) return;
    const fd = new FormData();
    fd.append('file', file);
    const res = await fetch('/api/wallpaper', {method: 'POST', body: fd});
    if (res.ok) {
        toast('Wallpaper updated', 'success');
        await navigateTo(CURRENT_DIR, false);
    }
}

function randomId() {
    if (crypto.randomUUID) return crypto.randomUUID().replace(/-/g, '');
    const a = new Uint8Array(16);
    crypto.getRandomValues(a);
    return Array.from(a, b => b.toString(16).padStart(2, '0')).join('');
}
function uploadFileTus(file, targetPath, onProgress) {
    return new Promise((resolve, reject) => {
        const upload = new tus.Upload(file, {
            endpoint: '/api/tus',
            retryDelays: [0, 1000, 3000, 5000, 10000],
            chunkSize: CHUNK_SIZE,
            metadata: { path: targetPath, filename: file.name },
            removeFingerprintOnSuccess: true,
            onError: (err) => reject(new Error(err.message || String(err))),
            onProgress: (sent, total) => onProgress(sent),
            onSuccess: () => resolve(),
        });
        upload.start();
    });
}
async function uploadFileParallel(file, targetPath, onProgress) {
    const totalChunks = Math.max(1, Math.ceil(file.size / CHUNK_SIZE));
    const uid = randomId();
    const progress = new Map();
    let nextChunk = 0;
    let firstError = null;
    function reportProgress() {
        let sum = 0;
        for (const v of progress.values()) sum += v;
        onProgress(sum);
    }
    async function worker() {
        while (true) {
            if (firstError) return;
            const i = nextChunk++;
            if (i >= totalChunks) return;
            const start = i * CHUNK_SIZE;
            const end = Math.min(start + CHUNK_SIZE, file.size);
            const blob = file.slice(start, end);
            const chunkSize = end - start;
            progress.set(i, 0);
            let attempts = 0;
            const maxAttempts = 4;
            while (true) {
                if (firstError) return;
                try {
                    await new Promise((resolve, reject) => {
                        const q = `id=${encodeURIComponent(uid)}&index=${i}&total=${totalChunks}` +
                                  `&path=${encodeURIComponent(targetPath)}`;
                        const xhr = new XMLHttpRequest();
                        xhr.open('POST', `/api/upload/chunk?${q}`, true);
                        xhr.upload.onprogress = e => {
                            if (!e.lengthComputable) return;
                            progress.set(i, e.loaded);
                            reportProgress();
                        };
                        xhr.onload = () => xhr.status === 200 ? resolve() : reject(new Error(`HTTP ${xhr.status}`));
                        xhr.onerror = () => reject(new Error('network error'));
                        xhr.send(blob);
                    });
                    progress.set(i, chunkSize);
                    reportProgress();
                    break;
                } catch (err) {
                    attempts++;
                    if (attempts >= maxAttempts) {
                        firstError = new Error(`Chunk ${i + 1}/${totalChunks} failed: ${err.message}`);
                        return;
                    }
                    progress.set(i, 0);
                    reportProgress();
                    await new Promise(r => setTimeout(r, 400 * attempts));
                }
            }
        }
    }
    await Promise.all(Array.from({length: Math.min(PARALLEL_CHUNKS, totalChunks)}, () => worker()));
    if (firstError) throw firstError;
    const res = await fetch(`/api/upload/finalize?id=${encodeURIComponent(uid)}`, {method: 'POST'});
    if (!res.ok) throw new Error(`Finalize failed: HTTP ${res.status}`);
}
async function uploadSequential(files, targetDir) {
    if (!files.length) return;
    const container = document.getElementById('uploadProgressContainer');
    container.style.display = 'block';
    const arr = Array.from(files);
    const totalSize = arr.reduce((a, f) => a + f.size, 0);
    let doneBytes = 0;
    const startTime = Date.now();
    for (let i = 0; i < arr.length; i++) {
        const f = arr[i];
        const safeTarget = targetDir === '/' ? '' : targetDir;
        const relPath = safeTarget + '/' + (f.webkitRelativePath || f.name);
        try {
            const progressFn = loaded => {
                const overall = doneBytes + loaded;
                const pct = totalSize ? (overall / totalSize) * 100 : 0;
                document.getElementById('uploadBar').style.width = pct + '%';
                const elapsed = (Date.now() - startTime) / 1000;
                const mbps = elapsed > 0 ? (overall / (1024*1024)) / elapsed : 0;
                const eta = mbps > 0 && totalSize > overall
                    ? Math.ceil(((totalSize - overall) / (1024*1024)) / mbps) + 's' : '--';
                document.getElementById('uploadStatusText').innerText = `${i + 1}/${arr.length}: ${pct.toFixed(0)}%`;
                document.getElementById('uploadMetricsText').innerText = `${mbps.toFixed(1)} MB/s · ETA ${eta}`;
            };
            if (LIBS.tus) await uploadFileTus(f, relPath, progressFn);
            else await uploadFileParallel(f, relPath, progressFn);
            doneBytes += f.size;
            toast(`✓ ${f.name}`, 'success', 2000);
        } catch (err) {
            toast(`✗ ${f.name}: ${err.message}`, 'error', 6000);
            break;
        }
    }
    container.style.display = 'none';
    document.getElementById('uploadBar').style.width = '0%';
    await navigateTo(CURRENT_DIR, false);
}
const dropzone = document.getElementById('dropzone');
dropzone.ondragover = e => { e.preventDefault(); dropzone.classList.add('drag'); };
dropzone.ondragleave = e => { e.preventDefault(); dropzone.classList.remove('drag'); };
dropzone.ondrop = e => {
    e.preventDefault();
    dropzone.classList.remove('drag');
    if (e.dataTransfer.files.length) {
        uploadSequential(e.dataTransfer.files, UPLOAD_DEST || CURRENT_DIR);
    }
};

async function verifyChecksum() {
    const el = firstSelectedEl();
    if (!el) return;
    const p = liPath(el); if (!p) return;
    toast('Computing SHA-256…', 'info', 1500);
    const res = await fetch('/api/checksum?path=' + encodeURIComponent(p));
    const data = await res.json();
    toast(`${liName(el)}: ${data.checksum.slice(0, 24)}…`, 'info', 8000);
    console.log('SHA-256', liName(el), data.checksum);
}

let pendingBatch = [];
function triggerNativeDownload(url, filename) {
    const a = document.createElement('a');
    a.href = url;
    if (filename) a.download = filename;
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
}
function pathToName(path) {
    const clean = path.replace(/\/$/, '');
    const parts = clean.split('/').filter(Boolean);
    return decodeURIComponent(parts[parts.length - 1] || 'download');
}
function handleBulkDownloadAction() {
    const paths = Array.from(selectedPaths);
    if (paths.length === 0) return;
    if (paths.length === 1) {
        triggerNativeDownload(paths[0], pathToName(paths[0]));
        toast(`Downloading ${pathToName(paths[0])}`, 'info', 2500);
        return;
    }
    pendingBatch = paths.slice();
    continueBatchDownload(true);
}
function continueBatchDownload(isFirst) {
    if (pendingBatch.length === 0) { cancelBatchDownload(); return; }
    const batch = pendingBatch.splice(0, DOWNLOAD_BATCH_SIZE);
    batch.forEach(path => triggerNativeDownload(path, pathToName(path)));
    const banner = document.getElementById('batchBanner');
    if (pendingBatch.length === 0) {
        document.getElementById('batchMsg').textContent = `${batch.length} sent · done`;
        document.getElementById('batchNextBtn').style.display = 'none';
        banner.classList.add('show');
        setTimeout(cancelBatchDownload, 2500);
    } else {
        document.getElementById('batchNextBtn').style.display = '';
        document.getElementById('batchMsg').textContent = `${batch.length} sent · ${pendingBatch.length} pending`;
        banner.classList.add('show');
    }
}
function cancelBatchDownload() {
    pendingBatch = [];
    document.getElementById('batchBanner').classList.remove('show');
    document.getElementById('batchNextBtn').style.display = '';
}
function handleZipDownload() {
    const paths = Array.from(selectedPaths);
    if (paths.length === 0) return;
    const params = new URLSearchParams();
    paths.forEach(p => params.append('file', p));
    const url = '/download_zip?' + params.toString();
    if (url.length > MAX_ZIP_URL_CHARS) {
        toast(`Too many files for a single ZIP (${paths.length}). Select fewer.`, 'warn', 6000);
        return;
    }
    triggerNativeDownload(url, 'defxult_transfer.zip');
    toast(`Zipping ${paths.length} file${paths.length === 1 ? '' : 's'}…`, 'info', 3500);
}

let pickerCurrentDir = '';
function makeFolderRow(label, onClick) {
    const li = document.createElement('li');
    li.className = 'folder-list-item';
    li.textContent = '📁 ' + label;
    li.addEventListener('click', onClick);
    return li;
}
async function loadFolderPicker(dir) {
    const res = await fetch('/api/folders?dir=' + encodeURIComponent(dir));
    const data = await res.json();
    pickerCurrentDir = data.current;
    document.getElementById('pickerPath').textContent = '/sdcard' + (data.current ? '/' + data.current : '');
    const list = document.getElementById('pickerList');
    list.innerHTML = '';
    if (data.current !== '') {
        const parent = data.current.split('/').slice(0, -1).join('/');
        list.appendChild(makeFolderRow('.. (Go Back)', () => loadFolderPicker(parent)));
    }
    data.folders.forEach(f => {
        const next = data.current ? data.current + '/' + f : f;
        list.appendChild(makeFolderRow(f, () => loadFolderPicker(next)));
    });
}
function openFolderPicker() {
    loadFolderPicker(CURRENT_DIR);
    openModal('folderPickerModal');
}
function triggerPickerUpload() { document.getElementById('pickerFileInput').click(); }
function startPickerUpload(files) {
    closeModal('folderPickerModal');
    uploadSequential(files, '/' + pickerCurrentDir);
}
function showNewFolderModal() {
    document.getElementById('modalTitle').innerText = 'Create New Folder';
    const input = document.getElementById('modalInput');
    input.value = ''; input.placeholder = 'Folder name...';
    document.getElementById('modalSubmit').onclick = async () => {
        if (!input.value) return;
        const url = '/api/mkdir?dir=' + encodeURIComponent(CURRENT_DIR) + '&name=' + encodeURIComponent(input.value);
        const r = await fetch(url, {method: 'POST'});
        if (r.ok) {
            closeModal('modal');
            toast('Folder created', 'success');
            await navigateTo(CURRENT_DIR, false);
        }
    };
    openModal('modal');
}
function renameItem() {
    const el = firstSelectedEl();
    if (!el) return;
    const p = liPath(el); if (!p) return;
    document.getElementById('modalTitle').innerText = 'Rename Item';
    const input = document.getElementById('modalInput');
    input.value = liName(el);
    document.getElementById('modalSubmit').onclick = async () => {
        if (!input.value) return;
        const url = `/api/rename?old=${encodeURIComponent(p)}&new=${encodeURIComponent(input.value)}`;
        const r = await fetch(url, {method: 'POST'});
        if (r.ok) {
            closeModal('modal');
            toast('Renamed', 'success');
            selectedPaths.clear();
            updateActionBar();
            await navigateTo(CURRENT_DIR, false);
        }
    };
    openModal('modal');
}
async function deleteSelected() {
    const paths = Array.from(selectedPaths);
    if (paths.length === 0) return;
    const params = paths.map(p => 'path=' + encodeURIComponent(p)).join('&');
    const r = await fetch('/api/delete?' + params, {method: 'POST'});
    if (r.ok) {
        const data = await r.json().catch(() => ({}));
        toast(`Deleted ${data.deleted ?? ''} item(s)`.trim(), 'success');
        selectedPaths.clear();
        updateActionBar();
        await navigateTo(CURRENT_DIR, false);
    }
}

(function init() {
    applyIcons();
    const list = document.getElementById('fileList');
    const sentinel = document.getElementById('loadSentinel');
    const allLi = Array.from(list.querySelectorAll('li'));
    const dataRows = allLi.filter(li =>
        li.id !== 'loadSentinel' &&
        li.dataset.name !== '..' &&
        !li.classList.contains('empty-row')
    );
    const emptyRows = allLi.filter(li => li.classList.contains('empty-row'));
    dataRows.forEach(li => li.remove());
    allRowEls = dataRows;
    setupSentinelObserver();
    if (dataRows.length === 0 && emptyRows.length > 0) {
        if (sentinel) sentinel.style.display = 'none';
        displayEls = [];
        renderedCount = 0;
    } else {
        applySort(false);
    }
    if (MUSIC_URL) {
        document.getElementById('audioBar').classList.add('show');
        updateVolumeUI();
        setTimeout(initSidebarWaveform, 100);
    } else {
        updateVolumeUI();
    }
    const hint = document.getElementById('dropHint');
    if (hint) {
        const bits = [];
        if (LIBS.tus) bits.push('tus resumable');
        else bits.push('chunked');
        if (__HAS_FFMPEG__) bits.push('ffmpeg');
        hint.textContent = bits.join(' · ');
    }

    ensureHoverPreview();
})();
</script>
</body>
</html>
"""


# ---------- server lifecycle (called by Android via Chaquopy, or by main()) ----------

_servers = []
_servers_lock = threading.Lock()


def start_server(preferred_port=DEFAULT_PORT):
    """
    Start the HTTP server in a daemon thread. Returns [actual_port, auth_token].
    Called from the Android app via Chaquopy; also called from main() on Termux.
    """
    if not os.path.isdir(ROOT_DIR):
        raise RuntimeError(f"{ROOT_DIR} does not exist")

    cleanup_old_uploads()
    cleanup_transcode_cache()

    port = preferred_port
    httpd = None
    attempts = 0
    while attempts < 10:
        try:
            httpd = HighSpeedHTTPServer(("0.0.0.0", port), ModernHandler)
            break
        except OSError:
            port += 1
            attempts += 1

    if httpd is None:
        raise RuntimeError(
            f"could not bind to any port in {preferred_port}-{preferred_port + 9}"
        )

    t = threading.Thread(target=httpd.serve_forever, daemon=True,
                         name="defxult-server")
    t.start()
    with _servers_lock:
        _servers.append(httpd)

    print(f"[server] listening on 0.0.0.0:{port}")
    return [port, AUTH_TOKEN]


def stop_server():
    """Shut down all running servers. Idempotent."""
    with _servers_lock:
        for s in list(_servers):
            try:
                s.shutdown()
                s.server_close()
            except Exception as e:
                print(f"[server] shutdown error: {e}")
        _servers.clear()
    print("[server] stopped")


def main():
    if not os.path.isdir(ROOT_DIR):
        print(f"Error: {ROOT_DIR} does not exist. Run 'termux-setup-storage' first.")
        return

    try:
        port, token = start_server(DEFAULT_PORT)
    except RuntimeError as e:
        print(f"Error: {e}")
        return

    ip = get_local_ip()
    base_url = f"http://{ip}:{port}"
    authed_url = f"{base_url}/?t={token}"

    wall = find_wallpaper_file()
    music = find_music_file()

    print()
    print("  defxult wifi transfer")
    print("  " + "-" * 44)
    print(f"  URL:      {authed_url}")
    print(f"  PIN:      {token}")
    print()
    print(f"  If the URL doesn't work, open this instead:")
    print(f"    {base_url}")
    print(f"  ...and when prompted, type the PIN: {token}")
    print()
    print(f"  Token file: {TOKEN_FILE}")
    print(f"  Wallpaper:  {'yes - ' + os.path.basename(wall) if wall else 'none set'}")
    print(f"  Music:      {'yes - ' + os.path.basename(music) if music else 'none set (generated beat)'}")
    print(f"  ffmpeg:     {'yes' if FFMPEG else 'no'}")
    print(f"  sendfile:   {'yes' if HAS_SENDFILE else 'no'}")
    print(f"  Upload dest: {UPLOAD_DEST if UPLOAD_DEST else '(current folder)'}")
    print()
    print("  Scan the QR code below to connect.")
    print()
    print_qr_code(authed_url)
    print()

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nShutting down.")
        stop_server()


if __name__ == "__main__":
    main()