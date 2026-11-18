"""Spike #57 dev API (stdlib; W1 will be FastAPI). Run: uv run python spike/57-forms/server.py"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parent))
from postprocess import postprocess_schema, provenance, restore  # noqa: E402

from luregen.config import LureConfig, load_config  # noqa: E402
from luregen.config.resolve import load_materials, resolve  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
VARIANT = ROOT / "spec/designs/glide/variants/striper_175_petg.toml"


def _relpaths(d):
    """Show repo-relative paths in the UI (load_layered makes them absolute)."""
    for k in ("image", "top_image", "preset"):
        v = d["profile"].get(k)
        if v:
            d["profile"][k] = str(Path(v).relative_to(ROOT))
    return d


def _abspaths(d):
    for k in ("image", "top_image", "preset"):
        v = (d.get("profile") or {}).get(k)
        if v and not Path(v).is_absolute():
            d["profile"][k] = str(ROOT / v)
    return d


def _errors(exc: ValidationError):
    out = []
    for e in exc.errors():
        loc = [p for p in e["loc"] if not (isinstance(p, str) and p.startswith("function-"))]
        out.append({"path": loc, "message": e["msg"].removeprefix("Value error, ")})
    return out


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/schema":
            schema, ui = postprocess_schema(
                LureConfig.model_json_schema(), load_materials(ROOT / "spec")
            )
            return self._send(200, {"schema": schema, "uiSchema": ui})
        if self.path == "/api/config":
            cfg = load_config(VARIANT)
            return self._send(
                200,
                {
                    "value": restore(_relpaths(cfg.model_dump(mode="json"))),
                    "provenance": provenance(VARIANT),
                },
            )
        self._send(404, {})

    def do_POST(self):
        if self.path != "/api/validate":
            return self._send(404, {})
        data = json.loads(self.rfile.read(int(self.headers["content-length"])))
        try:
            cfg = LureConfig.model_validate(_abspaths(restore(data)))
            cfg = resolve(cfg, ROOT / "spec")
        except ValidationError as exc:
            return self._send(200, {"ok": False, "errors": _errors(exc)})
        except ValueError as exc:  # ConfigError from resolve()
            return self._send(200, {"ok": False, "errors": [{"path": [], "message": str(exc)}]})
        return self._send(200, {"ok": True, "resolved": _relpaths(cfg.model_dump(mode="json"))})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8757), H).serve_forever()
