"""Tests for rssignal.config."""

import pytest

from rssignal.config import Config, ConfigError, get_config, load_dotenv


def test_load_dotenv_parses_pairs(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# a comment\n"
        "\n"
        "RSSIGNAL_ACCOUNT=+31600000000\n"
        'RSSIGNAL_RECIPIENT="+31611111111"\n'
        "export EXPORTED=value\n"
    )
    monkeypatch.delenv("RSSIGNAL_ACCOUNT", raising=False)
    monkeypatch.delenv("RSSIGNAL_RECIPIENT", raising=False)
    monkeypatch.delenv("EXPORTED", raising=False)

    parsed = load_dotenv(str(env_file))

    assert parsed == {
        "RSSIGNAL_ACCOUNT": "+31600000000",
        "RSSIGNAL_RECIPIENT": "+31611111111",
        "EXPORTED": "value",
    }
    import os

    assert os.environ["RSSIGNAL_ACCOUNT"] == "+31600000000"
    assert os.environ["RSSIGNAL_RECIPIENT"] == "+31611111111"


def test_load_dotenv_missing_file_returns_empty():
    assert load_dotenv("/nonexistent/path/.env") == {}


def test_load_dotenv_existing_env_wins(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("RSSIGNAL_ACCOUNT=+from_file\n")
    monkeypatch.setenv("RSSIGNAL_ACCOUNT", "+from_env")

    load_dotenv(str(env_file))

    import os

    assert os.environ["RSSIGNAL_ACCOUNT"] == "+from_env"


def test_load_dotenv_override(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("RSSIGNAL_ACCOUNT=+from_file\n")
    monkeypatch.setenv("RSSIGNAL_ACCOUNT", "+from_env")

    load_dotenv(str(env_file), override=True)

    import os

    assert os.environ["RSSIGNAL_ACCOUNT"] == "+from_file"


def test_get_config_reads_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RSSIGNAL_ACCOUNT", "+31600000000")
    monkeypatch.setenv("RSSIGNAL_RECIPIENT", "+31611111111")

    config = get_config(env_file=str(tmp_path / "missing.env"))

    assert config == Config(account="+31600000000", recipient="+31611111111")


def test_get_config_recipient_optional(tmp_path, monkeypatch):
    monkeypatch.setenv("RSSIGNAL_ACCOUNT", "+31600000000")
    monkeypatch.delenv("RSSIGNAL_RECIPIENT", raising=False)

    config = get_config(env_file=str(tmp_path / "missing.env"))

    assert config.recipient is None


def test_get_config_missing_account_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("RSSIGNAL_ACCOUNT", raising=False)

    with pytest.raises(ConfigError):
        get_config(env_file=str(tmp_path / "missing.env"))
