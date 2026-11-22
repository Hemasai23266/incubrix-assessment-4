"""Regenerate openapi.yaml from the app so the file always matches the service exactly."""
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.main import app  # noqa: E402

if __name__ == "__main__":
    spec = app.openapi()
    assert spec["openapi"].startswith("3.1"), spec["openapi"]
    (ROOT / "openapi.yaml").write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print("openapi.yaml written, version", spec["openapi"])
