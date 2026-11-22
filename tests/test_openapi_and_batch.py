import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_openapi_yaml_matches_running_app(client):
    from app.main import app
    on_disk = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
    live = yaml.safe_load(yaml.safe_dump(app.openapi(), sort_keys=False))
    assert on_disk["openapi"].startswith("3.1")
    assert on_disk == live, "openapi.yaml is stale: run `python scripts/export_openapi.py`"
    assert all(p.startswith(("/api/v1/", "/connector/v1/", "/health", "/ready")) for p in on_disk["paths"])


def test_batch_runner_runs_samples_in_process(client, tmp_path, monkeypatch):
    """Runs the real CLI against the in-process app (no network)."""
    import httpx

    from cli import batch_runner

    real_client = httpx.Client

    def fake_client(*a, **kw):
        base = kw.pop("base_url", "")
        return _Wrapper(client, base, kw.get("headers", {}))

    class _Wrapper:
        def __init__(self, c, base, headers):
            self.c, self.headers = c, headers

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, **kw):
            return self.c.post(url, headers={**self.headers, **kw.pop("headers", {})}, **kw)

        def get(self, url, **kw):
            return self.c.get(url, headers=self.headers, **kw)

    monkeypatch.setattr(batch_runner.httpx, "Client", fake_client)
    rc = batch_runner.main([str(ROOT / "samples"), "--out", str(tmp_path), "--token", "test-token"])
    assert rc == 0
    import json
    idx = json.loads((tmp_path / "index.json").read_text())
    assert idx["total"] == 12 and idx["passed"] == 12
