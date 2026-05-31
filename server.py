#!/usr/bin/env python3
"""
Redirect Server
===============
Ein einfacher Webserver der HTTP-Weiterleitungen basierend auf
einer YAML-Konfigurationsdatei durchführt.

Verwendung:
    python server.py [--config config.yml]
"""

import argparse
import logging
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

import yaml

# ── Logging Setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("redirect-server")


# ── Config Loader ──────────────────────────────────────────────────────────────
def load_config(path: str) -> dict:
    """Lädt und validiert die YAML-Konfigurationsdatei."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except FileNotFoundError:
        log.error(f"Konfigurationsdatei nicht gefunden: {path}")
        sys.exit(1)
    except yaml.YAMLError as e:
        log.error(f"Fehler beim Parsen der Konfiguration: {e}")
        sys.exit(1)

    # Redirect-Map aufbauen: {"/pfad": {"target": "...", "code": 301}}
    redirect_map: dict[str, dict] = {}
    for entry in cfg.get("redirects", []):
        path_key = entry.get("path", "").rstrip("/") or "/"
        target = entry.get("target", "")
        code = int(entry.get("code", 302))

        if not target:
            log.warning(f"Kein Ziel für Pfad '{path_key}' angegeben – übersprungen.")
            continue
        if code not in (301, 302, 307, 308):
            log.warning(f"Ungültiger HTTP-Code {code} für '{path_key}' – verwende 302.")
            code = 302

        redirect_map[path_key] = {"target": target, "code": code}
        log.info(f"  Redirect: {path_key}  →  {target}  [{code}]")

    default_target = cfg.get("server", {}).get("default") or cfg.get("default")
    return {
        "host": cfg.get("server", {}).get("host", "0.0.0.0"),
        "port": int(cfg.get("server", {}).get("port", 8080)),
        "redirects": redirect_map,
        "default": default_target,
    }


# ── Request Handler ────────────────────────────────────────────────────────────
def make_handler(config: dict):
    """Erzeugt einen Handler mit eingebetteter Konfiguration."""

    class RedirectHandler(BaseHTTPRequestHandler):
        _config = config

        def do_GET(self):
            self._handle()

        def do_HEAD(self):
            self._handle()

        def do_POST(self):
            self._handle()

        def _handle(self):
            # Pfad ohne Query-String normalisieren
            raw_path = urlparse(self.path).path.rstrip("/") or "/"
            redirects = self._config["redirects"]

            # Exakter Treffer
            if raw_path in redirects:
                rule = redirects[raw_path]
                self._redirect(rule["target"], rule["code"])
                return

            # Kein Treffer → Default oder 404
            default = self._config.get("default")
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
                f"<html><head><title>404 – Nicht gefunden</title></head>"
                f"<body><h1>404 – Pfad nicht konfiguriert</h1>"
                f"<p>Kein Redirect für <code>{path}</code> definiert.</p></body></html>"
            ).encode("utf-8")
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass  # Standard-Logging deaktiviert, wir nutzen unser eigenes

    return RedirectHandler


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="HTTP Redirect Server")
    parser.add_argument(
        "--config",
        default="config.yml",
        help="Pfad zur YAML-Konfigurationsdatei (Standard: config.yml)",
    )
    args = parser.parse_args()

    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log.info("  Redirect Server  –  startet …")
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    config = load_config(args.config)

    host = config["host"]
    port = config["port"]

    log.info(f"Konfiguriert: {len(config['redirects'])} Weiterleitungen")
    if config["default"]:
        log.info(f"Standard-Weiterleitung: {config['default']}")

    server = HTTPServer((host, port), make_handler(config))
    log.info(f"Server läuft auf  http://{host}:{port}  – Ctrl+C zum Beenden")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Server gestoppt.")
        server.server_close()


if __name__ == "__main__":
    main()
