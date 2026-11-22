"""scripts/smoke_test_deployed.py: friendly token errors, token cleaning, waiting for the seed, no tracebacks. Uses the real app in-process."""
import importlib.util
import pathlib

import pytest
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


smoke, seed = load("smoke_test_deployed"), load("seed_demo")
GOOD = "test-token"  # the API_TOKEN configured for the test app in conftest


@pytest.fixture
def factory():
    from app.main import app
    with TestClient(app) as c:
        def make(headers):
            return TestClient(app, base_url="http://testserver", headers=headers or {})
        make.seed = lambda: [c.post("/api/v1/destinations", json=r, headers={"Authorization": f"Bearer {GOOD}", "Idempotency-Key": f"seed-{r['id']}"}) for r in seed.CATALOGUE]
        yield make


def run(factory, token, **kw):
    lines = []
    code = smoke.run("http://testserver", token, client_factory=factory, sleep=kw.pop("sleep", lambda s: None), out=lines.append, **kw)
    return code, "\n".join(lines)


def test_full_success_when_catalogue_is_seeded(factory):
    factory.seed()
    code, out = run(factory, GOOD)
    assert code == 0 and "ALL PASSED" in out and "FAIL" not in out
    assert GOOD not in out  # the token is never printed


def test_wrong_token_gives_a_clear_message_not_a_keyerror(factory):
    code, out = run(factory, "wrong-token-value-1234567890")
    assert code == 2
    assert "FAIL  token check" in out and "rejected the token" in out and "CALLBACK_SIGNING_SECRET" in out
    assert "KeyError" not in out and "Traceback" not in out
    assert "wrong-token-value" not in out  # the (wrong) token is not echoed either
    assert "no further checks were run" in out


def test_token_with_spaces_quotes_and_brackets_is_cleaned(factory):
    factory.seed()
    code, out = run(factory, f'  "<{GOOD}>" \n')
    assert code == 0 and "removed" in out


def test_short_token_triggers_a_warning(factory):
    code, out = run(factory, "abc")
    assert code == 2 and "only 3 characters" in out


def test_waits_for_the_seed_after_a_cold_start(factory):
    calls = []

    def sleep(s):
        calls.append(s)
        if len(calls) == 2:  # the seed finishes while the script is waiting
            factory.seed()
    code, out = run(factory, GOOD, sleep=sleep)
    assert code == 0 and "waiting for the demo seed" in out and "12/12" in out


def test_unseeded_catalogue_fails_cleanly_with_a_hint(factory):
    code, out = run(factory, GOOD, catalogue_wait=0.0)
    assert code == 1
    assert "FAIL  demo catalogue present" in out and "0/12" in out and "Render logs" in out
    assert "Traceback" not in out  # later checks that need the catalogue fail as FAIL lines, not crashes


def test_main_usage_and_env_var(monkeypatch, capsys):
    assert smoke.main(["smoke"]) == 2 and "smoke_test_deployed.py" in capsys.readouterr().out
    assert smoke.main(["smoke", "not-a-url"]) == 2
    monkeypatch.delenv("INCUBRIX_API_TOKEN", raising=False)
    monkeypatch.setattr(smoke.sys.stdin, "isatty", lambda: False)
    assert smoke.main(["smoke", "https://example.invalid"]) == 2  # no token anywhere and no terminal: refuses instead of hanging
    seen = {}
    monkeypatch.setenv("INCUBRIX_API_TOKEN", "  from-env-token-0123456789 ")
    monkeypatch.setattr(smoke, "run", lambda base, token, **k: seen.update(base=base, token=token) or 0)
    assert smoke.main(["smoke", "https://example.invalid"]) == 0 and seen["token"] == "  from-env-token-0123456789 "  # cleaned inside run()


# ---- cold start: connection resets and 502/503/504 are retried; an unreachable service ends in a message, not a traceback ----
def make_flaky(n, mode):
    import httpx

    from app.main import app
    state = {"left": n}

    class Flaky(TestClient):
        def get(self, url, **kw):
            if state["left"] > 0:
                state["left"] -= 1
                if mode == "reset":
                    raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
                return httpx.Response(503, request=httpx.Request("GET", f"http://testserver{url}"), json={"error": "starting"})
            return super().get(url, **kw)

    return lambda headers: Flaky(app, base_url="http://testserver", headers=headers or {})


@pytest.mark.parametrize("mode", ["reset", "503"])
def test_rides_out_a_cold_start(factory, mode):
    factory.seed()
    lines = []
    code = smoke.run("http://testserver", GOOD, client_factory=make_flaky(4, mode), sleep=lambda s: None, out=lines.append)
    out = "\n".join(lines)
    assert code == 0 and "ALL PASSED" in out and "waiting for it to start" in out


def test_unreachable_service_gives_clear_failures_not_a_traceback(factory):
    lines = []
    code = smoke.run("http://testserver", GOOD, client_factory=make_flaky(10 ** 6, "reset"), sleep=lambda s: None, startup_wait=6, out=lines.append)
    out = "\n".join(lines)
    assert code == 1
    assert "FAIL  GET /health" in out and "FAIL  could not reach the API" in out and "no further checks were run" in out
    assert "Traceback" not in out


def test_main_never_prints_a_traceback(monkeypatch, capsys):
    monkeypatch.setattr(smoke, "run", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    assert smoke.main(["smoke", "https://example.invalid", "x" * 20]) == 1
    assert "unexpected error (ValueError)" in capsys.readouterr().out
