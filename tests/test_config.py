from pathlib import Path

from agent_surf import config


def test_defaults():
    cfg = config.load({})
    assert cfg.cdp_url == "http://127.0.0.1:9222"
    assert cfg.home == Path("~/.agent-surf").expanduser()
    assert cfg.model == "claude-sonnet-5-5"
    assert cfg.db_path == cfg.home / "surf.db"
    assert cfg.maps_dir == cfg.home / "maps"
    assert cfg.chrome_profile == cfg.home / "chrome-profile"


def test_overrides(tmp_path):
    cfg = config.load({
        "AGENT_SURF_CDP_URL": "http://127.0.0.1:9333",
        "AGENT_SURF_HOME": str(tmp_path),
        "AGENT_SURF_MODEL": "claude-opus-5-5",
    })
    assert cfg.cdp_url == "http://127.0.0.1:9333"
    assert cfg.home == tmp_path
    assert cfg.model == "claude-opus-5-5"
    cfg.ensure_dirs()
    assert cfg.maps_dir.is_dir()


def test_secrets_not_on_config():
    env = {"ANTHROPIC_API_KEY": "sk-test-not-real", "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}
    cfg = config.load(env)
    assert "sk-test-not-real" not in repr(cfg)
    assert config.anthropic_api_key(env) == "sk-test-not-real"
    assert config.telegram_credentials(env) == ("t", "c")
    assert config.telegram_credentials({"TELEGRAM_BOT_TOKEN": "t"}) is None
    assert config.anthropic_api_key({}) is None
