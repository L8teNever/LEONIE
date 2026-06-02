#!/usr/bin/env python3
"""
LEONIE Redirect Server
======================
Redirect-Server (PORT) + Admin-Panel (ADMIN_PORT).
Weiterleitungen werden in DATA_FILE gespeichert und überleben Neustarts.
"""

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

import yaml

# ── Logging Setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("LEONIE")

DATA_FILE = os.environ.get("DATA_FILE", "/data/redirects.json")

# ── Shared Config (thread-safe) ────────────────────────────────────────────────
_lock = threading.Lock()
_redirects: dict[str, dict] = {}
_default_target: str | None = None


def get_config() -> tuple[dict, str | None]:
    with _lock:
        return dict(_redirects), _default_target


def set_config(redirects: dict, default: str | None, persist: bool = True) -> None:
    global _redirects, _default_target
    with _lock:
        _redirects = redirects
        _default_target = default
    if persist:
        _save()


# ── Persistence ────────────────────────────────────────────────────────────────
def _save() -> None:
    try:
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"redirects": _redirects, "default": _default_target}, f, indent=2, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)
    except Exception as e:
        log.warning(f"Speichern fehlgeschlagen: {e}")


def _load_from_file() -> tuple[dict, str | None] | None:
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("redirects", {}), data.get("default")
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning(f"Datei konnte nicht geladen werden: {e}")
        return None


# ── Config Loader ──────────────────────────────────────────────────────────────
def load_config() -> dict:
    # Server-Einstellungen: Umgebungsvariablen haben Vorrang vor CONFIG_YAML
    cfg: dict = {}
    config_yaml = os.environ.get("CONFIG_YAML", "")
    if config_yaml:
        try:
            cfg = yaml.safe_load(config_yaml) or {}
            log.info("CONFIG_YAML geladen.")
        except yaml.YAMLError as e:
            log.warning(f"CONFIG_YAML Fehler (ignoriert): {e}")

    host = os.environ.get("HOST", cfg.get("server", {}).get("host", "0.0.0.0"))
    port = int(os.environ.get("PORT", cfg.get("server", {}).get("port", 8080)))
    admin_port = int(os.environ.get("ADMIN_PORT", cfg.get("server", {}).get("admin_port", 8081)))

    # Redirects: Datei hat Vorrang (Änderungen via Admin-Panel)
    file_data = _load_from_file()
    if file_data is not None:
        redirect_map, default_target = file_data
        log.info(f"Redirects aus Datei geladen: {len(redirect_map)} Einträge")
    else:
        # Erster Start: aus CONFIG_YAML befüllen (als Startwerte)
        redirect_map = {}
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
        if redirect_map or default_target:
            log.info(f"Startwerte aus CONFIG_YAML übernommen: {len(redirect_map)} Einträge")

    for path, rule in redirect_map.items():
        log.info(f"  Redirect: {path}  →  {rule['target']}  [{rule['code']}]")

    return {
        "host": host,
        "port": port,
        "admin_port": admin_port,
        "redirects": redirect_map,
        "default": default_target,
    }


# ── Redirect Handler ───────────────────────────────────────────────────────────
def make_redirect_handler():
    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self):  self._handle()
        def do_HEAD(self): self._handle()
        def do_POST(self): self._handle()

        def _handle(self):
            raw_path = urlparse(self.path).path.rstrip("/") or "/"
            redirects, default = get_config()

            if raw_path in redirects:
                rule = redirects[raw_path]
                self._redirect(rule["target"], rule["code"])
                return

            if default:
                self._redirect(default, 302)
                return

            self._not_found(raw_path)

        def _redirect(self, target: str, code: int):
            log.info(f"{self.client_address[0]}  {self.command} {self.path}  →  {code} {target}")
            self.send_response(code)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _not_found(self, path: str):
            log.warning(f"{self.client_address[0]}  {self.command} {self.path}  →  404")
            body = (
                "<html><head><title>404 – Nicht gefunden</title></head>"
                "<body><h1>404 – Pfad nicht konfiguriert</h1>"
                f"<p>Kein Redirect für <code>{path}</code> definiert.</p></body></html>"
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


# ── Admin UI ───────────────────────────────────────────────────────────────────
ADMIN_HTML = """\
<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LEONIE</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,sans-serif;background:#0a0a0a;color:#d4d4d4;min-height:100vh}
header{background:#111;border-bottom:1px solid #1e1e1e;padding:16px 32px;display:flex;align-items:center;gap:10px}
header h1{font-size:1.1rem;font-weight:700;color:#fff;letter-spacing:-.02em}
.badge{background:#2563eb;color:#fff;padding:2px 8px;border-radius:4px;font-size:.68rem;font-weight:600;letter-spacing:.05em}
main{max-width:820px;margin:32px auto;padding:0 20px;display:flex;flex-direction:column;gap:18px}
.card{background:#111;border:1px solid #1e1e1e;border-radius:10px;padding:20px 22px}
.card-title{font-size:.72rem;font-weight:600;color:#555;text-transform:uppercase;letter-spacing:.07em;margin-bottom:14px}
label{display:block;font-size:.72rem;color:#555;margin-bottom:4px}
input,select{width:100%;background:#0d0d0d;border:1px solid #252525;border-radius:6px;padding:8px 10px;color:#d4d4d4;font-size:.875rem;transition:border-color .15s}
input:focus,select:focus{outline:none;border-color:#3b82f6}
input::placeholder{color:#383838}
.grid-add{display:grid;grid-template-columns:150px 1fr 150px auto;gap:10px;align-items:end}
.grid-default{display:grid;grid-template-columns:1fr auto;gap:10px;align-items:end}
button{padding:8px 16px;border:none;border-radius:6px;cursor:pointer;font-size:.825rem;font-weight:500;transition:background .15s;white-space:nowrap}
.btn-blue{background:#2563eb;color:#fff}.btn-blue:hover{background:#1d4ed8}
.btn-ghost{background:#1c1c1c;color:#999}.btn-ghost:hover{background:#252525}
.btn-red{background:transparent;border:1px solid #3f1212;color:#f87171;padding:4px 10px;font-size:.75rem}.btn-red:hover{background:#1f0a0a;border-color:#7f1d1d}
table{width:100%;border-collapse:collapse}
th{text-align:left;font-size:.68rem;color:#444;text-transform:uppercase;letter-spacing:.07em;padding:0 10px 10px 0;border-bottom:1px solid #1a1a1a}
td{padding:10px 10px 10px 0;border-bottom:1px solid #161616;font-size:.85rem;vertical-align:middle}
tr:last-child td{border-bottom:none}
.mono{font-family:ui-monospace,monospace;font-size:.82rem}
.path{color:#a78bfa}.target{color:#34d399;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:320px;display:block}
.chip{display:inline-block;background:#172554;color:#60a5fa;padding:2px 8px;border-radius:4px;font-size:.68rem;font-family:ui-monospace,monospace}
.empty{text-align:center;color:#333;padding:30px 0;font-size:.85rem}
.toast{position:fixed;bottom:20px;right:20px;padding:10px 18px;border-radius:8px;font-size:.8rem;display:none;z-index:99;border:1px solid}
.hint{font-size:.75rem;color:#444;margin-top:7px}
</style>
</head>
<body>
<header>
  <h1>LEONIE</h1>
  <span class="badge">ADMIN</span>
</header>
<main>

  <div class="card">
    <div class="card-title">Neue Weiterleitung</div>
    <div class="grid-add">
      <div>
        <label>Pfad</label>
        <input type="text" id="newPath" class="mono" placeholder="/test">
      </div>
      <div>
        <label>Ziel-URL</label>
        <input type="url" id="newTarget" placeholder="https://meine-seite.de">
      </div>
      <div>
        <label>Typ</label>
        <select id="newCode">
          <option value="302">302 – Temporär</option>
          <option value="301">301 – Permanent</option>
          <option value="307">307 – Temporär</option>
          <option value="308">308 – Permanent</option>
        </select>
      </div>
      <button class="btn-blue" onclick="addRedirect()">Hinzufügen</button>
    </div>
    <div class="hint">Beispiel: Pfad <span class="mono" style="color:#666">/discord</span> → <span class="mono" style="color:#666">https://discord.gg/...</span></div>
  </div>

  <div class="card">
    <div class="card-title">Standard-Weiterleitung <span style="font-weight:400;color:#3a3a3a">— wenn kein Pfad passt</span></div>
    <div class="grid-default">
      <div>
        <label>Ziel-URL (leer lassen = 404 anzeigen)</label>
        <input type="url" id="defaultTarget" placeholder="https://meine-startseite.de">
      </div>
      <button class="btn-ghost" onclick="saveDefault()">Speichern</button>
    </div>
  </div>

  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
      <div class="card-title" style="margin-bottom:0">Aktive Weiterleitungen</div>
      <span id="count" style="font-size:.72rem;color:#444"></span>
    </div>
    <table>
      <thead><tr><th>Pfad</th><th>Ziel</th><th>Code</th><th></th></tr></thead>
      <tbody id="redirectTable"><tr><td colspan="4" class="empty">Lädt …</td></tr></tbody>
    </table>
  </div>

</main>
<div class="toast" id="toast"></div>
<script>
let _data = { redirects: [], default: null };

async function load() {
  const res = await fetch('/api/redirects');
  _data = await res.json();
  document.getElementById('defaultTarget').value = _data.default || '';
  const tbody = document.getElementById('redirectTable');
  const count = document.getElementById('count');
  count.textContent = _data.redirects.length ? _data.redirects.length + ' Einträge' : '';
  if (!_data.redirects.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty">Noch keine Weiterleitungen — füge oben eine hinzu</td></tr>';
    return;
  }
  tbody.innerHTML = _data.redirects.map(r =>
    '<tr>' +
    '<td><span class="path mono">' + esc(r.path) + '</span></td>' +
    '<td><span class="target">' + esc(r.target) + '</span></td>' +
    '<td><span class="chip">' + r.code + '</span></td>' +
    '<td><button class="btn-red" onclick="del(\'' + r.path.replace(/'/g,"\\'") + '\')">Löschen</button></td>' +
    '</tr>'
  ).join('');
}

async function addRedirect() {
  const path   = document.getElementById('newPath').value.trim();
  const target = document.getElementById('newTarget').value.trim();
  const code   = parseInt(document.getElementById('newCode').value);
  if (!path)   { toast('Pfad fehlt', 1); return; }
  if (!target) { toast('Ziel-URL fehlt', 1); return; }
  if (!path.startsWith('/')) { toast('Pfad muss mit / beginnen', 1); return; }
  await post('/api/redirects', { path, target, code });
  document.getElementById('newPath').value = '';
  document.getElementById('newTarget').value = '';
  toast('Weiterleitung gespeichert');
  load();
}

async function del(path) {
  await post('/api/redirects/delete', { path });
  toast('Gelöscht');
  load();
}

async function saveDefault() {
  const target = document.getElementById('defaultTarget').value.trim() || null;
  await post('/api/default', { target });
  toast(target ? 'Standard gesetzt' : 'Standard entfernt');
  load();
}

function post(url, body) {
  return fetch(url, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
}

function toast(msg, err) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.background = err ? '#1a0505' : '#0d1f12';
  t.style.color      = err ? '#fca5a5' : '#86efac';
  t.style.borderColor= err ? '#7f1d1d' : '#166534';
  t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', 2200);
}

function esc(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

load();
</script>
</body>
</html>
"""


# ── Admin Handler ──────────────────────────────────────────────────────────────
def make_admin_handler():
    class AdminHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/admin"):
                self._serve(200, "text/html; charset=utf-8", ADMIN_HTML.encode())
            elif self.path == "/api/redirects":
                self._api_list()
            else:
                self._serve(404, "text/plain", b"Not found")

        def do_POST(self):
            if self.path == "/api/redirects":
                self._api_add()
            elif self.path == "/api/redirects/delete":
                self._api_delete()
            elif self.path == "/api/default":
                self._api_set_default()
            else:
                self._serve(404, "text/plain", b"Not found")

        def _serve(self, code: int, ctype: str, body: bytes):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, data):
            self._serve(code, "application/json", json.dumps(data).encode())

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length))

        def _api_list(self):
            redirects, default = get_config()
            self._json(200, {
                "redirects": [
                    {"path": k, "target": v["target"], "code": v["code"]}
                    for k, v in sorted(redirects.items())
                ],
                "default": default,
            })

        def _api_add(self):
            data = self._read_json()
            path = data.get("path", "").rstrip("/") or "/"
            target = data.get("target", "").strip()
            code = int(data.get("code", 302))

            if not target:
                self._json(400, {"error": "target erforderlich"})
                return
            if code not in (301, 302, 307, 308):
                code = 302

            redirects, default = get_config()
            redirects[path] = {"target": target, "code": code}
            set_config(redirects, default)
            log.info(f"Admin: + {path}  →  {target}  [{code}]")
            self._json(200, {"ok": True})

        def _api_delete(self):
            data = self._read_json()
            path = data.get("path", "").rstrip("/") or "/"
            redirects, default = get_config()
            if path in redirects:
                del redirects[path]
                set_config(redirects, default)
                log.info(f"Admin: - {path} gelöscht")
            self._json(200, {"ok": True})

        def _api_set_default(self):
            data = self._read_json()
            target = data.get("target", "").strip() or None
            redirects, _ = get_config()
            set_config(redirects, target)
            log.info(f"Admin: Standard → {target}")
            self._json(200, {"ok": True})

        def log_message(self, fmt, *args):
            pass

    return AdminHandler


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log.info("  LEONIE Redirect Server  –  startet …")
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    config = load_config()
    set_config(config["redirects"], config["default"], persist=False)

    host = config["host"]
    port = config["port"]
    admin_port = config["admin_port"]

    log.info(f"Konfiguriert: {len(config['redirects'])} Weiterleitungen")
    if config["default"]:
        log.info(f"Standard-Weiterleitung: {config['default']}")

    admin_server = HTTPServer((host, admin_port), make_admin_handler())
    threading.Thread(target=admin_server.serve_forever, daemon=True).start()
    log.info(f"Admin-Panel:     http://{host}:{admin_port}")

    redirect_server = HTTPServer((host, port), make_redirect_handler())
    log.info(f"Redirect-Server: http://{host}:{port}  – Ctrl+C zum Beenden")

    try:
        redirect_server.serve_forever()
    except KeyboardInterrupt:
        log.info("Server gestoppt.")
        redirect_server.server_close()
        admin_server.server_close()


if __name__ == "__main__":
    main()
