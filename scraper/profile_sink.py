#!/usr/bin/env python3
import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonicalize_out_path(path: str) -> str:
    p = os.path.abspath(path)
    tail = os.path.join("scraper", "site_profiles.json")
    if p.endswith(os.sep + tail):
        p = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(p)), "site_profiles.json"))
    return p


def atomic_write_json(path: str, data: dict) -> None:
    path = os.path.abspath(path)
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".site_profiles.", suffix=".json", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except Exception:
            pass


def read_existing_site_overrides(path: str) -> dict:
    """Read existing sink-format file and return siteOverrides dict (or {})."""
    try:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        if isinstance(data, dict) and isinstance(data.get("siteOverrides"), dict):
            return data["siteOverrides"]
    except Exception:
        pass
    return {}


class Handler(BaseHTTPRequestHandler):
    server_version = "ImotiAIProfileSink/1.0"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        p = urlparse(self.path).path
        if p not in ("/", "/profile"):
            self.send_response(404)
            self._cors()
            self.end_headers()
            self.wfile.write(b'{"ok":false,"error":"not_found"}')
            return

        out_path = self.server.out_path  # type: ignore[attr-defined]
        if not os.path.exists(out_path):
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(b'{"ok":true,"siteOverrides":{}}')
            return

        with open(out_path, "rb") as f:
            body = f.read()

        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        p = urlparse(self.path).path
        if p != "/profile":
            self.send_response(404)
            self._cors()
            self.end_headers()
            self.wfile.write(b'{"ok":false,"error":"not_found"}')
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length > 0 else b"{}"

        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            self.send_response(400)
            self._cors()
            self.end_headers()
            self.wfile.write(b'{"ok":false,"error":"invalid_json"}')
            return

        if isinstance(payload, dict) and "siteOverrides" in payload:
            site_overrides_in = payload.get("siteOverrides")
        else:
            site_overrides_in = payload

        if not isinstance(site_overrides_in, dict):
            self.send_response(400)
            self._cors()
            self.end_headers()
            self.wfile.write(b'{"ok":false,"error":"siteOverrides_must_be_object"}')
            return

        out_path = self.server.out_path  # type: ignore[attr-defined]

        # ✅ MERGE instead of replace
        existing = read_existing_site_overrides(out_path)
        merged = dict(existing)
        for host, ov in site_overrides_in.items():
            if isinstance(host, str) and isinstance(ov, dict):
                merged[host] = ov

        out = {
            "siteOverrides": merged,
            "updatedAt": utc_now_iso(),
            "source": (payload.get("source") if isinstance(payload, dict) else None) or "unknown",
        }

        try:
            atomic_write_json(out_path, out)
        except Exception as e:
            self.send_response(500)
            self._cors()
            self.end_headers()
            msg = json.dumps({"ok": False, "error": f"write_failed: {e}"})
            self.wfile.write(msg.encode("utf-8"))
            return

        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, fmt, *args):
        return


def main():
    ap = argparse.ArgumentParser(description="Local sink that writes site_profiles.json from extension POSTs.")
    ap.add_argument("--host", default=os.getenv("PROFILE_SINK_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.getenv("PROFILE_SINK_PORT", "8788")))
    ap.add_argument(
        "--out",
        default=os.getenv("PROFILE_SINK_OUT", "site_profiles.json"),
        help="Output JSON file path (bind-mount a folder if running in Docker).",
    )
    args = ap.parse_args()

    httpd = HTTPServer((args.host, args.port), Handler)
    httpd.out_path = canonicalize_out_path(args.out)  # type: ignore[attr-defined]

    print(f"[profile_sink] listening on http://{args.host}:{args.port}/profile")
    print(f"[profile_sink] writing -> {httpd.out_path}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
