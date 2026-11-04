"""HTTP server that shares files with an iPhone (or any phone) over the local network.

The phone opens the page in Safari (via QR code), downloads shared files and can
upload files back. No app is needed on the phone.
"""
import html
import json
import mimetypes
import os
import secrets
import socket
import threading
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CHUNK = 1024 * 1024
DEFAULT_PORT = 8765


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 80))  # no packet is sent; this just picks the outgoing interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def default_receive_dir():
    # Inside a strict snap $HOME points at ~/snap/<name>/<rev>; SNAP_REAL_HOME is the real one.
    home = os.environ.get("SNAP_REAL_HOME") or os.path.expanduser("~")
    return Path(home) / "Downloads" / "From iPhone"


def unique_path(folder, name):
    name = os.path.basename(name.replace("\\", "/")).strip() or "file"
    if name in (".", ".."):
        name = "file"
    target = folder / name
    stem, suffix = target.stem, target.suffix
    i = 1
    while target.exists() or target.with_name(target.name + ".part").exists():
        target = folder / f"{stem} ({i}){suffix}"
        i += 1
    return target


class SharedFiles:
    """Thread-safe, mutable list of files being shared. Each file gets a stable id."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items = {}  # id -> (display_name, Path)
        self._next_id = 0
        self.version = 0

    def add(self, path):
        """Add a file or folder (recursively). Returns number of files added."""
        p = Path(path).expanduser().resolve()
        if p.is_file():
            entries = [(p.name, p)]
        elif p.is_dir():
            entries = [(f"{p.name}/{f.relative_to(p)}", f)
                       for f in sorted(p.rglob("*")) if f.is_file()]
        else:
            raise FileNotFoundError(str(p))
        added = 0
        with self._lock:
            existing = {path for _, path in self._items.values()}
            for name, f in entries:
                if f in existing:
                    continue
                self._items[self._next_id] = (name, f)
                self._next_id += 1
                added += 1
            if added:
                self.version += 1
        return added

    def remove(self, file_id):
        with self._lock:
            if self._items.pop(file_id, None):
                self.version += 1

    def clear(self):
        with self._lock:
            if self._items:
                self._items.clear()
                self.version += 1

    def get(self, file_id):
        with self._lock:
            return self._items.get(file_id)

    def snapshot(self):
        """List of (id, name, path, size) for files that still exist."""
        with self._lock:
            items = list(self._items.items())
        out = []
        for fid, (name, path) in items:
            try:
                out.append((fid, name, path, path.stat().st_size))
            except OSError:
                pass
        return out


PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wi-Fi Drop · {host}</title>
<style>
 :root {{ color-scheme: light dark; --accent:#0a84ff; }}
 body {{ font: 17px -apple-system, system-ui, sans-serif; margin:0; padding:20px; max-width:640px; margin-inline:auto; }}
 h1 {{ font-size:22px; margin:4px 0 2px; }} .sub {{ opacity:.6; font-size:14px; margin-bottom:18px; }}
 h2 {{ font-size:15px; text-transform:uppercase; letter-spacing:.04em; opacity:.6; margin:26px 0 8px; }}
 .card {{ border-radius:14px; background:rgba(127,127,127,.12); overflow:hidden; }}
 .row {{ display:flex; align-items:center; gap:12px; padding:12px 14px; border-bottom:1px solid rgba(127,127,127,.18); }}
 .row:last-child {{ border-bottom:0; }}
 .name {{ flex:1; min-width:0; overflow-wrap:anywhere; }} .size {{ font-size:13px; opacity:.6; }}
 a.btn, .btn {{ background:var(--accent); color:#fff; border:0; border-radius:10px; padding:9px 14px;
   font:inherit; font-size:15px; font-weight:600; text-decoration:none; white-space:nowrap; display:inline-block; }}
 a.ghost {{ background:transparent; color:var(--accent); padding:9px 6px; }}
 .all {{ display:block; text-align:center; margin-top:12px; }}
 .thumb {{ width:44px; height:44px; object-fit:cover; border-radius:8px; }}
 .icon {{ width:44px; height:44px; border-radius:8px; background:rgba(127,127,127,.2); display:flex;
   align-items:center; justify-content:center; font-size:11px; font-weight:700; opacity:.8; }}
 .hint, .empty {{ font-size:13px; opacity:.6; margin-top:8px; }} .empty {{ padding:16px; text-align:center; font-size:15px; }}
 #status {{ margin-top:10px; font-size:15px; }}
 progress {{ width:100%; margin-top:8px; display:none; }}
 input[type=file] {{ display:none; }}
</style></head><body>
<h1>📲 Wi-Fi Drop</h1><div class="sub">connected to {host}</div>
<h2>Files for you ({count})</h2>
{files}
<h2>Send to PC</h2>
<div class="card"><div class="row">
 <div class="name">Upload photos or files to this computer<div class="size">saved to {recv_dir}</div></div>
 <label><input id="f" type="file" multiple><span class="btn">Choose…</span></label>
</div></div>
<progress id="bar" max="1" value="0"></progress>
<div id="status"></div>
<script>
const f = document.getElementById('f'), st = document.getElementById('status'), bar = document.getElementById('bar');
let busy = false, version = {version};
function send(file) {{
  return new Promise((ok, fail) => {{
    const x = new XMLHttpRequest();
    x.open('POST', 'upload');
    x.setRequestHeader('X-Filename', encodeURIComponent(file.name));
    x.upload.onprogress = e => {{ if (e.lengthComputable) bar.value = e.loaded / e.total; }};
    x.onload = () => x.status == 200 ? ok() : fail(x.status);
    x.onerror = () => fail('network error');
    x.send(file);
  }});
}}
f.onchange = async () => {{
  const files = [...f.files]; busy = true; bar.style.display = 'block';
  try {{
    for (let i = 0; i < files.length; i++) {{
      st.textContent = `Uploading ${{i+1}}/${{files.length}}: ${{files[i].name}}…`; bar.value = 0;
      await send(files[i]);
    }}
    st.textContent = `✅ Sent ${{files.length}} file(s) to the PC.`;
  }} catch (e) {{ st.textContent = `❌ Upload failed (${{e}})`; }}
  busy = false; bar.style.display = 'none'; f.value = '';
}};
// Refresh automatically when files are added/removed on the PC.
setInterval(async () => {{
  if (busy || document.hidden) return;
  try {{
    const s = await (await fetch('state', {{cache:'no-store'}})).json();
    if (s.v !== version) location.reload();
  }} catch (e) {{}}
}}, 2000);
</script>
</body></html>"""


def _files_html(snapshot):
    if not snapshot:
        return '<div class="card"><div class="empty">Nothing shared yet — drag files onto the app on your PC.</div></div>'
    rows = []
    for fid, name, _path, size in snapshot:
        mime = mimetypes.guess_type(name)[0] or ""
        if mime.startswith("image/"):
            lead = f'<img class="thumb" src="f/{fid}?inline=1" loading="lazy">'
        else:
            ext = html.escape(Path(name).suffix[1:5].upper() or "FILE")
            lead = f'<div class="icon">{ext}</div>'
        viewable = mime.startswith(("image/", "video/", "audio/", "text/")) or mime == "application/pdf"
        view = f'<a class="btn ghost" href="f/{fid}?inline=1">Open</a>' if viewable else ""
        rows.append(
            f'<div class="row">{lead}<div class="name">{html.escape(name)}'
            f'<div class="size">{human(size)}</div></div>'
            f'{view}<a class="btn" href="f/{fid}" download>Save</a></div>')
    out = f'<div class="card">{"".join(rows)}</div>'
    if len(snapshot) > 1:
        total = human(sum(s for *_, s in snapshot))
        out += f'<a class="btn all" href="all.zip">Download all as ZIP ({total})</a>'
    out += ('<div class="hint">Tip: <b>Save</b> puts files in the Files app → Downloads. For photos/videos, '
            'tap <b>Open</b>, then Share → <b>Save Image / Save Video</b> to put them in Photos.</div>')
    return out


class DropServer:
    """Runs the HTTP server in a background thread.

    on_event(kind, name, detail) is called from server threads with kind in
    {"download", "zip", "received"}.
    """

    def __init__(self, files=None, receive_dir=None, port=DEFAULT_PORT, on_event=None):
        self.files = files or SharedFiles()
        self.receive_dir = Path(receive_dir) if receive_dir else default_receive_dir()
        self.token = secrets.token_urlsafe(6)
        self.on_event = on_event or (lambda kind, name, detail: None)
        handler = self._make_handler()
        try:
            self.httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
        except OSError:
            self.httpd = ThreadingHTTPServer(("0.0.0.0", 0), handler)
        self.httpd.daemon_threads = True
        self._thread = None

    @property
    def port(self):
        return self.httpd.server_address[1]

    def url(self):
        return f"http://{lan_ip()}:{self.port}/{self.token}/"

    def start(self):
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def serve_forever(self):
        self.httpd.serve_forever()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _make_handler(self):
        srv = self
        host = socket.gethostname()

        class Handler(BaseHTTPRequestHandler):
            server_version = "wifi-drop/1.0"

            def log_message(self, fmt, *args):
                pass

            def _route(self):
                parsed = urllib.parse.urlsplit(self.path)
                prefix = f"/{srv.token}/"
                if not parsed.path.startswith(prefix):
                    return None, None
                return parsed.path[len(prefix):], urllib.parse.parse_qs(parsed.query)

            def _send_bytes(self, body, ctype):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                route, query = self._route()
                if route is None:
                    return self.send_error(404)
                if route == "":
                    snap = srv.files.snapshot()
                    body = PAGE.format(host=html.escape(host), count=len(snap), files=_files_html(snap),
                                       recv_dir=html.escape(str(srv.receive_dir)),
                                       version=srv.files.version).encode()
                    self._send_bytes(body, "text/html; charset=utf-8")
                elif route == "state":
                    self._send_bytes(json.dumps({"v": srv.files.version}).encode(), "application/json")
                elif route.startswith("f/"):
                    try:
                        entry = srv.files.get(int(route[2:]))
                    except ValueError:
                        entry = None
                    if not entry:
                        return self.send_error(404)
                    self._send_file(*entry, inline="inline" in query)
                elif route == "all.zip":
                    self._send_zip()
                else:
                    self.send_error(404)

            def _send_file(self, name, path, inline):
                try:
                    size = path.stat().st_size
                except OSError:
                    return self.send_error(404)
                start, end = 0, size - 1
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes=") and "," not in rng:
                    a, _, b = rng[6:].partition("-")
                    try:
                        if a:
                            start = int(a)
                            end = min(int(b), size - 1) if b else size - 1
                        else:
                            start = max(size - int(b), 0)
                    except ValueError:
                        start, end = 0, size - 1
                    if start > end or start >= size:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.end_headers()
                        return
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                else:
                    rng = None
                    self.send_response(200)
                fname = os.path.basename(name)
                disp = "inline" if inline else "attachment"
                self.send_header("Content-Type", mimetypes.guess_type(fname)[0] or "application/octet-stream")
                self.send_header("Content-Disposition", f"{disp}; filename*=UTF-8''{urllib.parse.quote(fname)}")
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                if not rng:
                    srv.on_event("download", name, "")
                remaining = end - start + 1
                try:
                    with open(path, "rb") as fh:
                        fh.seek(start)
                        while remaining > 0:
                            chunk = fh.read(min(CHUNK, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _send_zip(self):
                snap = srv.files.snapshot()
                if not snap:
                    return self.send_error(404)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", 'attachment; filename="wifi-drop.zip"')
                self.end_headers()
                srv.on_event("zip", f"{len(snap)} files", "")
                try:
                    with zipfile.ZipFile(self.wfile, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
                        for _fid, name, path, _size in snap:
                            zf.write(path, name)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_POST(self):
                route, _ = self._route()
                if route != "upload":
                    return self.send_error(404)
                try:
                    length = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    return self.send_error(411)
                name = urllib.parse.unquote(self.headers.get("X-Filename", "file"))
                try:
                    srv.receive_dir.mkdir(parents=True, exist_ok=True)
                    target = unique_path(srv.receive_dir, name)
                except OSError:
                    return self.send_error(500)
                tmp = target.with_name(target.name + ".part")
                remaining = length
                try:
                    with open(tmp, "wb") as out:
                        while remaining > 0:
                            chunk = self.rfile.read(min(CHUNK, remaining))
                            if not chunk:
                                raise ConnectionError("upload interrupted")
                            out.write(chunk)
                            remaining -= len(chunk)
                    tmp.rename(target)
                except (ConnectionError, OSError):
                    tmp.unlink(missing_ok=True)
                    return
                srv.on_event("received", target.name, str(target))
                self._send_bytes(b"ok", "text/plain")

        return Handler
