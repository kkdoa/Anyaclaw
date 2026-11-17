"""Configuration loading, overrides and validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from anyaclaw.config import (
    DEFAULT_CONFIG_TEMPLATE,
    Config,
    ConfigError,
    load_config,
    read_config_file,
    write_default_config,
)


def _write_toml(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_are_valid() -> None:
    config = Config()
    config.validate()
    assert config.provider.model
    assert config.security.tool_policy == "confirm"


def test_load_toml_sections(tmp_path: Path) -> None:
    path = _write_toml(
        tmp_path / "anyaclaw.toml",
        """
[provider]
api_key = "sk-test"
model = "deepseek-chat"
base_url = "https://api.deepseek.com/v1"
timeout = 30

[agent]
max_iterations = 5

[security]
workspace = "work"
tool_policy = "allow"
""",
    )
    config = load_config(path, env={})
    assert config.provider.api_key == "sk-test"
    assert config.provider.timeout == 30.0
    assert config.agent.max_iterations == 5
    assert config.workspace == (tmp_path / "work").resolve()


def test_load_json(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"provider": {"model": "m1"}}), encoding="utf-8")
    config = load_config(path, env={})
    assert config.provider.model == "m1"


def test_env_overrides_double_underscore(tmp_path: Path) -> None:
    path = _write_toml(tmp_path / "anyaclaw.toml", '[provider]\nmodel = "file-model"\n')
    config = load_config(
        path,
        env={
            "ANYACLAW_PROVIDER__MODEL": "env-model",
            "ANYACLAW_WEB__PORT": "9099",
            "ANYACLAW_LOG_LEVEL": "DEBUG",
        },
    )
    assert config.provider.model == "env-model"
    assert config.web.port == 9099
    assert config.log_level == "DEBUG"


def test_conventional_api_key_env(tmp_path: Path) -> None:
    path = _write_toml(tmp_path / "empty.toml", "")
    config = load_config(path, env={"OPENAI_API_KEY": "sk-openai"})
    assert config.provider.api_key == "sk-openai"


def test_env_reference_interpolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_KEY", "sk-from-env")
    path = _write_toml(tmp_path / "anyaclaw.toml", '[provider]\napi_key = "${MY_KEY}"\n')
    config = load_config(path, env={"MY_KEY": "sk-from-env"})
    assert config.provider.api_key == "sk-from-env"


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    path = _write_toml(tmp_path / "anyaclaw.toml", '[provider]\nnot_a_field = 1\n')
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(path, env={})


def test_invalid_type_is_rejected(tmp_path: Path) -> None:
    path = _write_toml(tmp_path / "anyaclaw.toml", '[agent]\nmax_iterations = "many"\n')
    with pytest.raises(ConfigError):
        load_config(path, env={})


def test_invalid_toml_is_rejected(tmp_path: Path) -> None:
    path = _write_toml(tmp_path / "anyaclaw.toml", "[provider\nbroken = 1")
    with pytest.raises(ConfigError, match="invalid config"):
        load_config(path, env={})


def test_validation_errors(tmp_path: Path) -> None:
    path = _write_toml(tmp_path / "anyaclaw.toml", "[web]\nport = 70000\n")
    with pytest.raises(ConfigError, match="web.port"):
        load_config(path, env={})


def test_public_dict_masks_secrets(tmp_path: Path) -> None:
    path = _write_toml(
        tmp_path / "anyaclaw.toml",
        '[provider]\napi_key = "sk-secret"\n\n[channels.feishu]\napp_secret = "s3cr3t"\n',
    )
    config = load_config(path, env={})
    public = config.public_dict()
    assert public["provider"]["api_key"] == "***"
    assert public["channels"]["feishu"]["app_secret"] == "***"
    assert "sk-secret" not in json.dumps(public)


def test_default_template_parses(tmp_path: Path) -> None:
    path = write_default_config(tmp_path / "anyaclaw.toml")
    data = read_config_file(path)
    assert "provider" in data and "agent" in data
    config = load_config(path, env={})
    assert config.web.port == 8080


def test_write_default_config_refuses_overwrite(tmp_path: Path) -> None:
    path = write_default_config(tmp_path / "anyaclaw.toml")
    with pytest.raises(ConfigError, match="already exists"):
        write_default_config(path)
    assert write_default_config(path, overwrite=True).is_file()


def test_template_matches_shipped_example() -> None:
    example = Path(__file__).resolve().parents[1] / "config.example.toml"
    if not example.is_file():  # pragma: no cover - packaged installs
        pytest.skip("config.example.toml not shipped")
    assert example.read_text(encoding="utf-8").strip() == DEFAULT_CONFIG_TEMPLATE.strip()
