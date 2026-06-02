#!/usr/bin/env python3
"""
LEONIE Redirect Server
======================
Redirect-Server(en) + Admin-Panel.
Mehrere Redirect-Server auf verschiedenen Ports verwaltbar über das Admin-Panel.
"""

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

import yaml

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("LEONIE")

DATA_FILE = os.environ.get("DATA_FILE", "/data/redirects.json")

# ── Shared State ───────────────────────────────────────────────────────────────
_lock = threading.Lock()
# {port: {"redirects": {"/path": {"target": "...", "code": 301}}, "default": str|None}}
_servers: dict[int, dict] = {}
# {port: HTTPServer}
_running: dict[int, HTTPServer] = {}
_host: str = "0.0.0.0"
_main_port: int = 8080


def _cfg(port: int) -> dict:
    """Thread-safe copy of a server's config."""
    with _lock:
        s = _servers.get(port, {})
        return {"redirects": dict(s.get("redirects", {})), "default": s.get("default")}


# ── Persistence ────────────────────────────────────────────────────────────────
def _save() -> None:
    try:
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        with _lock:
            data = {
                "servers": {
                    str(p): {"redirects": c["redirects"], "default": c["default"]}
                    for p, c in _servers.items()
                }
            }
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)
    except Exception as e:
        log.warning(f"Speichern fehlgeschlagen: {e}")


def _load_file() -> dict | None:
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning(f"Datei laden fehlgeschlagen: {e}")
        return None


# ── Server Management ──────────────────────────────────────────────────────────
def _make_redirect_handler(port: int):
    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self):  self._handle()
        def do_HEAD(self): self._handle()
        def do_POST(self): self._handle()

        def _handle(self):
            raw_path = urlparse(self.path).path.rstrip("/") or "/"
            cfg = _cfg(port)
            redirects = cfg["redirects"]
            default = cfg["default"]

            if raw_path in redirects:
                r = redirects[raw_path]
                self._redirect(r["target"], r["code"])
                return
            if default:
                self._redirect(default, 302)
                return
            self._not_found(raw_path)

        def _redirect(self, target: str, code: int):
            log.info(f":{port}  {self.client_address[0]}  {self.command} {self.path}  →  {code} {target}")
            self.send_response(code)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _not_found(self, path: str):
            log.warning(f":{port}  {self.client_address[0]}  {self.command} {self.path}  →  404")
            body = (
                "<html><head><title>404</title></head>"
                f"<body><h1>404</h1><p>Kein Redirect für <code>{path}</code>.</p></body></html>"
            ).encode("utf-8")
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass

    return RedirectHandler


def _spawn_server(port: int) -> tuple[bool, str]:
    """Start an HTTPServer on the given port. Returns (success, error_message)."""
    try:
        srv = HTTPServer((_host, port), _make_redirect_handler(port))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        with _lock:
            _running[port] = srv
        return True, ""
    except OSError as e:
        return False, str(e)


def add_server(port: int) -> tuple[bool, str]:
    with _lock:
        if port in _servers:
            return False, "Port läuft bereits"
    ok, err = _spawn_server(port)
    if not ok:
        return False, f"Port {port} nicht verfügbar: {err}"
    with _lock:
        _servers[port] = {"redirects": {}, "default": None}
    _save()
    log.info(f"Admin: Neuer Redirect-Server auf Port {port}")
    return True, ""


def remove_server(port: int) -> tuple[bool, str]:
    if port == _main_port:
        return False, "Haupt-Server kann nicht gelöscht werden"
    with _lock:
        if port not in _servers:
            return False, "Port nicht gefunden"
        srv = _running.pop(port, None)
        del _servers[port]
    if srv:
        threading.Thread(target=srv.shutdown, daemon=True).start()
    _save()
    log.info(f"Admin: Server Port {port} gestoppt")
    return True, ""


# ── Config Loader ──────────────────────────────────────────────────────────────
def load_config() -> dict:
    cfg: dict = {}
    config_yaml = os.environ.get("CONFIG_YAML", "")
    if config_yaml:
        try:
            cfg = yaml.safe_load(config_yaml) or {}
        except yaml.YAMLError as e:
            log.warning(f"CONFIG_YAML Fehler (ignoriert): {e}")

    host = os.environ.get("HOST", cfg.get("server", {}).get("host", "0.0.0.0"))
    port = int(os.environ.get("PORT", cfg.get("server", {}).get("port", 8080)))
    admin_port = int(os.environ.get("ADMIN_PORT", cfg.get("server", {}).get("admin_port", 8081)))

    # Aus Datei laden (hat Vorrang)
    file_data = _load_file()
    if file_data:
        servers_raw = file_data.get("servers", {})
        with _lock:
            for p_str, s in servers_raw.items():
                p = int(p_str)
                _servers[p] = {
                    "redirects": s.get("redirects", {}),
                    "default": s.get("default"),
                }
        log.info(f"Aus Datei geladen: {len(_servers)} Server")
    else:
        # Erster Start: Startwerte aus CONFIG_YAML
        redirect_map: dict[str, dict] = {}
        for entry in cfg.get("redirects", []):
            path_key = entry.get("path", "").rstrip("/") or "/"
            target = entry.get("target", "")
            code = int(entry.get("code", 302))
            if not target:
                continue
            if code not in (301, 302, 307, 308):
                code = 302
            redirect_map[path_key] = {"target": target, "code": code}
        default_target = cfg.get("server", {}).get("default") or cfg.get("default")
        with _lock:
            _servers[port] = {"redirects": redirect_map, "default": default_target}

    return {"host": host, "port": port, "admin_port": admin_port}


# ── Admin UI ───────────────────────────────────────────────────────────────────
ADMIN_HTML = """\
<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LEONIE Admin</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,sans-serif;background:#0a0a0a;color:#d4d4d4;min-height:100vh}
header{background:#111;border-bottom:1px solid #1e1e1e;padding:14px 28px;display:flex;align-items:center;gap:10px}
header h1{font-size:1.05rem;font-weight:700;color:#fff;letter-spacing:-.02em}
.badge{background:#2563eb;color:#fff;padding:2px 8px;border-radius:4px;font-size:.65rem;font-weight:700;letter-spacing:.06em}
main{max-width:860px;margin:28px auto;padding:0 20px;display:flex;flex-direction:column;gap:16px}

/* Tabs */
.tabs{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.tab{padding:7px 16px;border-radius:7px;border:1px solid #1e1e1e;background:#111;color:#666;cursor:pointer;font-size:.8rem;font-weight:500;transition:all .15s}
.tab:hover{border-color:#2a2a2a;color:#999}
.tab.active{background:#1a2744;border-color:#2563eb;color:#60a5fa}
.tab-del{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;border-radius:3px;background:transparent;color:#555;cursor:pointer;font-size:.7rem;margin-left:5px;border:none;padding:0;line-height:1}
.tab-del:hover{background:#3f1212;color:#f87171}
.tab-add{padding:7px 14px;border-radius:7px;border:1px dashed #252525;background:transparent;color:#444;cursor:pointer;font-size:.8rem;transition:all .15s}
.tab-add:hover{border-color:#3b82f6;color:#3b82f6}

/* Cards */
.card{background:#111;border:1px solid #1e1e1e;border-radius:10px;padding:18px 20px}
.card-title{font-size:.68rem;font-weight:700;color:#444;text-transform:uppercase;letter-spacing:.08em;margin-bottom:12px}
label{display:block;font-size:.7rem;color:#505050;margin-bottom:4px}
input,select{width:100%;background:#0d0d0d;border:1px solid #222;border-radius:6px;padding:7px 10px;color:#d4d4d4;font-size:.875rem;transition:border-color .15s}
input:focus,select:focus{outline:none;border-color:#3b82f6}
input::placeholder{color:#333}
.g-add{display:grid;grid-template-columns:140px 1fr auto;gap:8px;align-items:end}
.g-default{display:grid;grid-template-columns:1fr auto;gap:8px;align-items:end}
.g-server{display:grid;grid-template-columns:140px auto;gap:8px;align-items:end}
button{padding:7px 15px;border:none;border-radius:6px;cursor:pointer;font-size:.8rem;font-weight:500;transition:background .15s;white-space:nowrap}
.btn-blue{background:#2563eb;color:#fff}.btn-blue:hover{background:#1d4ed8}
.btn-ghost{background:#181818;color:#888;border:1px solid #222}.btn-ghost:hover{background:#1f1f1f}

/* Table */
table{width:100%;border-collapse:collapse}
th{text-align:left;font-size:.65rem;color:#3d3d3d;text-transform:uppercase;letter-spacing:.08em;padding:0 8px 8px 0;border-bottom:1px solid #181818}
td{padding:9px 8px 9px 0;border-bottom:1px solid #141414;font-size:.83rem;vertical-align:middle}
tr:last-child td{border-bottom:none}
.mono{font-family:ui-monospace,monospace;font-size:.8rem}
.path{color:#a78bfa}.target{color:#34d399;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:300px;display:block}
.chip{display:inline-block;background:#172554;color:#60a5fa;padding:1px 7px;border-radius:4px;font-size:.68rem;font-family:ui-monospace,monospace}
.empty{text-align:center;color:#2a2a2a;padding:26px 0;font-size:.83rem}
.btn-row-del{background:transparent;border:1px solid #2a1414;color:#7f1d1d;padding:3px 9px;font-size:.72rem;border-radius:5px}
.btn-row-del:hover{background:#1a0808;border-color:#7f1d1d;color:#fca5a5}

/* Add-server modal inline */
#newServerForm{display:none;margin-top:12px;padding-top:12px;border-top:1px solid #1a1a1a}

/* Toast */
.toast{position:fixed;bottom:18px;right:18px;padding:9px 16px;border-radius:8px;font-size:.78rem;display:none;z-index:99;border:1px solid}
</style>
</head>
<body>
<header>
  <h1>LEONIE</h1>
  <span class="badge">ADMIN</span>
</header>
<main>

  <!-- Server-Tabs -->
  <div class="card">
    <div class="card-title">Redirect-Server</div>
    <div class="tabs" id="tabs"></div>
    <div id="newServerForm">
      <div class="g-server">
        <div>
          <label>Port</label>
          <input type="number" id="newPort" placeholder="z.B. 9090" min="1" max="65535">
        </div>
        <button class="btn-blue" onclick="doAddServer()">Starten</button>
      </div>
    </div>
  </div>

  <!-- Neue Weiterleitung -->
  <div class="card">
    <div class="card-title">Neue Weiterleitung <span id="forPort" style="color:#3b82f6"></span></div>
    <div class="g-add">
      <div><label>Pfad</label><input type="text" id="newPath" class="mono" placeholder="/test"></div>
      <div style="grid-column:span 2"><label>Ziel-URL</label><input type="url" id="newTarget" placeholder="https://..."></div>
      <button class="btn-blue" onclick="addRedirect()">Hinzufügen</button>
    </div>
  </div>

  <!-- Standard -->
  <div class="card">
    <div class="card-title">Standard-Weiterleitung <span style="font-weight:400;color:#2d2d2d">— wenn kein Pfad passt</span></div>
    <div class="g-default">
      <div>
        <label>Ziel-URL (leer = 404)</label>
        <input type="url" id="defaultTarget" placeholder="https://meine-startseite.de">
      </div>
      <button class="btn-ghost" onclick="saveDefault()">Speichern</button>
    </div>
  </div>

  <!-- Tabelle -->
  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <div class="card-title" style="margin-bottom:0">Weiterleitungen <span id="forPort2" style="color:#3b82f6;font-weight:400"></span></div>
      <span id="count" style="font-size:.68rem;color:#333"></span>
    </div>
    <table>
      <thead><tr><th>Pfad</th><th>Ziel</th><th>Code</th><th></th></tr></thead>
      <tbody id="redirectTable"><tr><td colspan="4" class="empty">Lädt …</td></tr></tbody>
    </table>
  </div>

</main>
<div class="toast" id="toast"></div>
<script>
let _allServers = [];
let _selected = null;

async function loadAll() {
  const res = await fetch('/api/servers');
  _allServers = await res.json();
  renderTabs();
  if (_selected === null || !_allServers.find(s => s.port === _selected)) {
    _selected = _allServers[0]?.port ?? null;
  }
  renderSelected();
}

function renderTabs() {
  const tabs = document.getElementById('tabs');
  const isMain = p => p === _allServers[0]?.port;
  tabs.innerHTML = _allServers.map(s =>
    '<button class="tab' + (s.port === _selected ? ' active' : '') + '" onclick="selectServer(' + s.port + ')">' +
    ':' + s.port +
    (isMain(s.port) ? '' : '<span class="tab-del" onclick="event.stopPropagation();delServer(' + s.port + ')" title="Stoppen">✕</span>') +
    '</button>'
  ).join('') +
  '<button class="tab-add" onclick="toggleNewServer()">+ Server</button>';
}

function renderSelected() {
  if (_selected === null) return;
  const s = _allServers.find(x => x.port === _selected);
  if (!s) return;

  document.getElementById('forPort').textContent = 'auf Port ' + s.port;
  document.getElementById('forPort2').textContent = 'auf Port ' + s.port;
  document.getElementById('defaultTarget').value = s.default || '';
  document.getElementById('count').textContent = s.redirects.length ? s.redirects.length + ' Einträge' : '';

  const tbody = document.getElementById('redirectTable');
  if (!s.redirects.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty">Noch keine Weiterleitungen</td></tr>';
    return;
  }
  tbody.innerHTML = s.redirects.map(r =>
    '<tr>' +
    '<td><span class="path mono">' + esc(r.path) + '</span></td>' +
    '<td><span class="target">' + esc(r.target) + '</span></td>' +
    '<td><span class="chip">' + r.code + '</span></td>' +
    '<td><button class="btn-row-del" onclick="delRedirect(\'' + esc2(r.path) + '\')">Löschen</button></td>' +
    '</tr>'
  ).join('');
}

function selectServer(port) {
  _selected = port;
  renderTabs();
  renderSelected();
}

function toggleNewServer() {
  const f = document.getElementById('newServerForm');
  f.style.display = f.style.display === 'none' ? 'block' : 'none';
  if (f.style.display === 'block') document.getElementById('newPort').focus();
}

async function doAddServer() {
  const port = parseInt(document.getElementById('newPort').value);
  if (!port || port < 1 || port > 65535) { toast('Ungültiger Port', 1); return; }
  const res = await post('/api/servers/add', { port });
  const data = await res.json();
  if (!data.ok) { toast(data.error, 1); return; }
  document.getElementById('newPort').value = '';
  document.getElementById('newServerForm').style.display = 'none';
  _selected = port;
  toast('Server auf Port ' + port + ' gestartet');
  loadAll();
}

async function delServer(port) {
  const res = await post('/api/servers/remove', { port });
  const data = await res.json();
  if (!data.ok) { toast(data.error, 1); return; }
  if (_selected === port) _selected = null;
  toast('Server Port ' + port + ' gestoppt');
  loadAll();
}

async function addRedirect() {
  const path   = document.getElementById('newPath').value.trim();
  const target = document.getElementById('newTarget').value.trim();
  const code   = 302;
  if (!path)            { toast('Pfad fehlt', 1); return; }
  if (!path.startsWith('/')) { toast('Pfad muss mit / beginnen', 1); return; }
  if (!target)          { toast('Ziel-URL fehlt', 1); return; }
  const res = await post('/api/redirects/add', { port: _selected, path, target, code });
  const data = await res.json();
  if (!data.ok) { toast(data.error, 1); return; }
  document.getElementById('newPath').value = '';
  document.getElementById('newTarget').value = '';
  toast('Weiterleitung gespeichert');
  loadAll();
}

async function delRedirect(path) {
  await post('/api/redirects/remove', { port: _selected, path });
  toast('Gelöscht');
  loadAll();
}

async function saveDefault() {
  const target = document.getElementById('defaultTarget').value.trim() || null;
  const res = await post('/api/default', { port: _selected, target });
  const data = await res.json();
  if (!data.ok) { toast(data.error, 1); return; }
  toast(target ? 'Standard gesetzt' : 'Standard entfernt');
  loadAll();
}

function post(url, body) {
  return fetch(url, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) });
}

function toast(msg, err) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.background   = err ? '#1a0505' : '#0d1f12';
  t.style.color        = err ? '#fca5a5' : '#86efac';
  t.style.borderColor  = err ? '#7f1d1d' : '#166534';
  t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', 2200);
}

function esc(s)  { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function esc2(s) { return String(s).replace(/\\/g,'\\\\').replace(/'/g,"\\'"); }

loadAll();
</script>
</body>
</html>
"""


# ── Admin Handler ──────────────────────────────────────────────────────────────
def make_admin_handler():
    class AdminHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            routes = {
                "/":       self._serve_html,
                "/admin":  self._serve_html,
                "/api/servers": self._api_servers,
            }
            h = routes.get(self.path)
            if h:
                h()
            else:
                self._serve(404, "text/plain", b"Not found")

        def do_POST(self):
            routes = {
                "/api/servers/add":      self._api_add_server,
                "/api/servers/remove":   self._api_remove_server,
                "/api/redirects/add":    self._api_add_redirect,
                "/api/redirects/remove": self._api_remove_redirect,
                "/api/default":          self._api_set_default,
            }
            h = routes.get(self.path)
            if h:
                h()
            else:
                self._serve(404, "text/plain", b"Not found")

        def _serve(self, code: int, ctype: str, body: bytes):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_html(self):
            self._serve(200, "text/html; charset=utf-8", ADMIN_HTML.encode())

        def _json(self, code: int, data):
            self._serve(code, "application/json", json.dumps(data).encode())

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n))

        def _api_servers(self):
            with _lock:
                result = [
                    {
                        "port": p,
                        "default": c["default"],
                        "redirects": [
                            {"path": k, "target": v["target"], "code": v["code"]}
                            for k, v in sorted(c["redirects"].items())
                        ],
                    }
                    for p, c in sorted(_servers.items())
                ]
            self._json(200, result)

        def _api_add_server(self):
            data = self._body()
            port = int(data.get("port", 0))
            if not (1 <= port <= 65535):
                self._json(400, {"ok": False, "error": "Ungültiger Port"})
                return
            ok, err = add_server(port)
            self._json(200 if ok else 400, {"ok": ok, "error": err})

        def _api_remove_server(self):
            data = self._body()
            port = int(data.get("port", 0))
            ok, err = remove_server(port)
            self._json(200 if ok else 400, {"ok": ok, "error": err})

        def _api_add_redirect(self):
            data = self._body()
            port = int(data.get("port", 0))
            path = data.get("path", "").rstrip("/") or "/"
            target = data.get("target", "").strip()
            code = int(data.get("code", 302))

            if not target:
                self._json(400, {"ok": False, "error": "Ziel-URL fehlt"})
                return
            if code not in (301, 302, 307, 308):
                code = 302

            with _lock:
                if port not in _servers:
                    self._json(400, {"ok": False, "error": "Server nicht gefunden"})
                    return
                _servers[port]["redirects"][path] = {"target": target, "code": code}
            _save()
            log.info(f"Admin: :{port}  + {path}  →  {target}  [{code}]")
            self._json(200, {"ok": True})

        def _api_remove_redirect(self):
            data = self._body()
            port = int(data.get("port", 0))
            path = data.get("path", "").rstrip("/") or "/"

            with _lock:
                if port in _servers and path in _servers[port]["redirects"]:
                    del _servers[port]["redirects"][path]
            _save()
            log.info(f"Admin: :{port}  - {path}")
            self._json(200, {"ok": True})

        def _api_set_default(self):
            data = self._body()
            port = int(data.get("port", 0))
            target = data.get("target", "").strip() or None

            with _lock:
                if port not in _servers:
                    self._json(400, {"ok": False, "error": "Server nicht gefunden"})
                    return
                _servers[port]["default"] = target
            _save()
            log.info(f"Admin: :{port}  Standard → {target}")
            self._json(200, {"ok": True})

        def log_message(self, fmt, *args):
            pass

    return AdminHandler


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    global _host, _main_port

    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log.info("  LEONIE Redirect Server  –  startet …")
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    config = load_config()
    _host = config["host"]
    _main_port = config["port"]
    admin_port = config["admin_port"]

    # Alle gespeicherten Server starten
    with _lock:
        ports = list(_servers.keys())
    for p in ports:
        ok, err = _spawn_server(p) if p not in _running else (True, "")
        if ok:
            log.info(f"Redirect-Server: http://{_host}:{p}")
        else:
            log.warning(f"Port {p} nicht verfügbar: {err}")

    # Sicherstellen dass Haupt-Port läuft
    if _main_port not in _running:
        with _lock:
            if _main_port not in _servers:
                _servers[_main_port] = {"redirects": {}, "default": None}
        ok, err = _spawn_server(_main_port)
        if not ok:
            log.error(f"Haupt-Port {_main_port} nicht verfügbar: {err}")
            return
        log.info(f"Redirect-Server: http://{_host}:{_main_port}")

    # Admin-Server
    admin_server = HTTPServer((_host, admin_port), make_admin_handler())
    threading.Thread(target=admin_server.serve_forever, daemon=True).start()
    log.info(f"Admin-Panel:     http://{_host}:{admin_port}")

    log.info("Ctrl+C zum Beenden")
    try:
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        log.info("Server gestoppt.")
        for srv in _running.values():
            srv.shutdown()
        admin_server.shutdown()


if __name__ == "__main__":
    main()
