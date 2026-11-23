"""Integration Standard point 1/6: .env.example must list EVERY setting the application reads, with safe example values only."""
import pathlib
import re

from app.config import Settings

ROOT = pathlib.Path(__file__).resolve().parents[1]


def example_keys():
    return {m.group(1): m.group(2) for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
            if (m := re.match(r"^([A-Z][A-Z0-9_]+)=(.*)$", line))}


def test_every_settings_field_is_listed_in_env_example():
    keys = set(example_keys())
    missing = sorted({f.upper() for f in Settings.model_fields} - keys)
    assert not missing, f".env.example is missing settings the code reads: {missing}"


def test_every_environment_variable_read_directly_in_app_is_listed():
    """os.environ / os.getenv with a literal name anywhere in app/ (settings fields are checked above)."""
    keys = set(example_keys())
    found = set()
    for f in (ROOT / "app").rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        found |= set(re.findall(r"os\.environ(?:\.get)?\(\s*[\"']([A-Z][A-Z0-9_]+)[\"']", text))
        found |= set(re.findall(r"os\.getenv\(\s*[\"']([A-Z][A-Z0-9_]+)[\"']", text))
    assert not (found - keys), f"env vars read in app/ but not in .env.example: {sorted(found - keys)}"


def test_defaults_in_the_example_match_the_code_defaults_for_safety_switches():
    ex = example_keys()
    defaults = Settings()
    assert ex["SIMULATION_MODE"].split("#")[0].strip() == "true" and defaults.simulation_mode is True
    for k in ("CALLBACK_ALLOW_PRIVATE", "MEDIA_FETCH_ALLOW_PRIVATE", "CALLBACK_ALLOW_HTTP"):
        assert ex[k].split("#")[0].strip() == "false", f"{k} must default to false in the example"
    assert defaults.callback_allow_private is False and defaults.media_fetch_allow_private is False


def test_example_contains_only_placeholders_and_local_defaults():
    secrets = {"API_TOKEN": "change-me-admin-token", "CALLBACK_SIGNING_SECRET": "change-me-callback-secret"}
    ex = example_keys()
    for k, v in secrets.items():
        assert ex[k].split("#")[0].strip() == v
    for k in ("DISCOURSE_API_KEY", "LEMMY_PASSWORD", "DEVTO_API_KEY", "GHOST_ADMIN_KEY", "WEBHOOK_SECRET", "CREDENTIALS_DIR"):
        assert ex[k].split("#")[0].strip() == "", f"{k} must be empty in the example"
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert not re.search(r"ghp_|sk-[A-Za-z0-9]{20}|AKIA[0-9A-Z]{16}|BEGIN (RSA|PRIVATE)|[0-9a-f]{24}:[0-9a-f]{64}", text)
