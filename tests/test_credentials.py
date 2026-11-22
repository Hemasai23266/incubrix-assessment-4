"""Credential resolution from the local credentials volume (CREDENTIALS_DIR) and its safety properties."""
import logging

from app.config import reset_settings
from app.runtime import read_secret
from tests.conftest import H, job, make_dest, publish


def test_file_in_credentials_dir_wins_over_env(tmp_path, monkeypatch):
    (tmp_path / "MY_REF").write_text("from-file\n")
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    monkeypatch.setenv("MY_REF", "from-env")
    reset_settings()
    assert read_secret("MY_REF") == "from-file"


def test_falls_back_to_env_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    monkeypatch.setenv("ONLY_ENV", "env-value")
    reset_settings()
    assert read_secret("ONLY_ENV") == "env-value"
    assert read_secret("MISSING_EVERYWHERE") == ""


def test_path_traversal_refs_are_rejected(tmp_path, monkeypatch):
    secret_dir = tmp_path / "creds"
    secret_dir.mkdir()
    (tmp_path / "outside").write_text("top-secret")
    monkeypatch.setenv("CREDENTIALS_DIR", str(secret_dir))
    reset_settings()
    for bad in ["../outside", "..\\outside", "a/../../outside", "/etc/passwd", "x y", ""]:
        assert read_secret(bad) == "", bad


def test_credential_from_volume_used_for_publish_but_never_logged_or_returned(client, tmp_path, monkeypatch, caplog):
    (tmp_path / "VOLUME_REF").write_text("volume-secret-zz91\n")
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    reset_settings()
    caplog.set_level(logging.INFO)
    from app.logging_setup import JsonFormatter
    d = make_dest(client, "discourse", credentials_ref="VOLUME_REF")
    j = job(client, publish(client, d["id"]).json()["job_id"])
    assert j["status"] == "succeeded"
    blob = str(j) + client.get(f"/api/v1/destinations/{d['id']}", headers=H).text
    blob += "\n".join(JsonFormatter().format(r) for r in caplog.records)
    assert "volume-secret-zz91" not in blob


def test_expired_credential_in_volume_maps_to_auth_expired(client, tmp_path, monkeypatch):
    (tmp_path / "EXP_REF").write_text("expired-token-1")
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    reset_settings()
    d = make_dest(client, "discourse", credentials_ref="EXP_REF")
    assert job(client, publish(client, d["id"]).json()["job_id"])["error"]["code"] == "CONNECTION_AUTH_EXPIRED"


def test_ready_reports_mode(client):
    assert client.get("/ready").json()["mode"] == "simulation"
