"""Spike #57: resolved config -> form value -> LureConfig round-trip checks."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from postprocess import postprocess_schema, restore  # noqa: E402

from luregen.config import LureConfig, load_config  # noqa: E402
from luregen.config.resolve import load_materials  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
cfg = load_config(ROOT / "spec/designs/glide/variants/striper_175_petg.toml")
schema, _ = postprocess_schema(LureConfig.model_json_schema(), load_materials(ROOT / "spec"))

form_value = restore(cfg.model_dump(mode="json"))  # what the client gets (no nulls)
back = LureConfig.model_validate(restore(json.loads(json.dumps(form_value))))
assert back == cfg, "round-trip mismatch"
print("python round-trip OK")

if len(sys.argv) > 1:  # also check a form-output file written by the browser
    out = json.loads(Path(sys.argv[1]).read_text())
    LureConfig.model_validate(restore(out))
    print("browser output validates")
