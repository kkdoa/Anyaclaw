"""Configuration model, loader and validator.

Design goals
------------
* **One authoritative load.** The original project re-read the config file inside
  the agent factory, so callers that injected a config got a different object and
  environment overrides were applied twice. Here a ``Config`` is built exactly once
  and passed down; :func:`load_config` is only called by the entry points.
* **Layered precedence.** defaults < ``anyaclaw.toml`` < environment.
* **Secrets never in git.** ``api_key = "env:OPENAI_API_KEY"`` and ``${VAR}``
  interpolation are supported, and :meth:`Config.public_dict` masks values for
  status endpoints.
* **Fail loudly.** Unknown keys are an error, not a silent typo.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import tomllib
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Union, get_args, get_origin, get_type_hints

from anyaclaw.core.errors import ConfigError

ENV_PREFIX = "ANYACLAW_"
CONFIG_FILENAMES = ("anyaclaw.toml", "config.toml", "anyaclaw.json", "config.json")
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ProviderConfig:
    """OpenAI-compatible endpoint settings."""

    api_key: str = ""
    base_url: str = "https://api.siliconflow.cn/v1"
    model: str = "deepseek-ai/DeepSeek-V3.2"
    subagent_model: str = ""
    cheap_model: str = ""
    temperature: float | None = 0.7
    top_p: float | None = None
    max_tokens: int | None = None
    timeout: float = 120.0
    connect_timeout: float = 15.0
    max_retries: int = 3
    retry_backoff: float = 0.8
    thinking: bool = True
    reasoning_effort: str = ""
    reasoning_budget: int = 0
    stream_usage: bool = True
    send_reasoning: bool = False
    extra_headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgentConfig:
    """Agent loop budgets and behaviour."""

    max_iterations: int = 32
    max_tool_calls: int = 64
    tool_timeout: float = 300.0
    max_history_messages: int = 80
    context_token_budget: int = 24000
    summarize: bool = True
    identity_file: str = "identity.md"
    skills_dir: str = "skills"
    loop_warn_after: int = 4
    loop_abort_after: int = 10
    loop_window: int = 40
    max_tool_output_chars: int = 20000
    parallel_tool_calls: bool = True
    show_reasoning: bool = True
    send_reasoning: bool = False
    stream: bool = True


@dataclass(slots=True)
class SecurityConfig:
    """Workspace boundary, tool policy and sender allow-lists."""

    workspace: str = "."
    tool_policy: str = "confirm"
    tool_policies: dict[str, str] = field(default_factory=dict)
    auto_approve: bool = False
    shell_enabled: bool = True
    allowed_commands: list[str] = field(default_factory=list)
    deny_patterns: list[str] = field(default_factory=list)
    enforce_sender_whitelist: bool = False
    allowed_senders: dict[str, list[str]] = field(default_factory=dict)
    max_file_bytes: int = 5_000_000
    web_auth_token: str = ""


@dataclass(slots=True)
class StorageConfig:
    """Where sessions, tasks, memory and generated files live."""

    data_dir: str = ".anyaclaw"
    session_backend: str = "sqlite"


@dataclass(slots=True)
class WebConfig:
    """Built-in HTTP/SSE server."""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8080
    cors_origins: list[str] = field(default_factory=list)
    sse_heartbeat: float = 15.0
    chat_timeout: float = 1800.0


@dataclass(slots=True)
class CliConfig:
    enabled: bool = True
    prompt: str = "you"


@dataclass(slots=True)
class QQConfig:
    enabled: bool = False
    app_id: str = ""
    app_secret: str = ""
    token: str = ""
    sandbox: bool = False
    webhook_secret: str = ""
    default_target: str = ""


@dataclass(slots=True)
class FeishuConfig:
    enabled: bool = False
    app_id: str = ""
    app_secret: str = ""
    verification_token: str = ""
    encrypt_key: str = ""
    default_target: str = ""


@dataclass(slots=True)
class DingTalkConfig:
    enabled: bool = False
    client_id: str = ""
    client_secret: str = ""
    webhook_url: str = ""
    webhook_keyword: str = ""
    default_target: str = ""


@dataclass(slots=True)
class ChannelsConfig:
    cli: CliConfig = field(default_factory=CliConfig)
    qq: QQConfig = field(default_factory=QQConfig)
    feishu: FeishuConfig = field(default_factory=FeishuConfig)
    dingtalk: DingTalkConfig = field(default_factory=DingTalkConfig)


@dataclass(slots=True)
class ToolsConfig:
    """Per-tool-group switches."""

    files: bool = True
    shell: bool = True
    web: bool = True
    memory: bool = True
    subagent: bool = True
    documents: bool = True
    mcp: bool = True
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    user_agent: str = "Anyaclaw/0.1 (+https://github.com/kkdoa/Anyaclaw)"
    max_web_chars: int = 40000


@dataclass(slots=True)
class Config:
    """Root configuration object."""

    provider: ProviderConfig = field(default_factory=ProviderConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    web: WebConfig = field(default_factory=WebConfig)
    channels: ChannelsConfig = field(default_factory=ChannelsConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    log_level: str = "INFO"
    log_json: bool = True
    config_path: str = ""

    # -- derived paths -----------------------------------------------------
    @property
    def base_dir(self) -> Path:
        """Directory that relative paths in the config are resolved against."""
        if self.config_path:
            return Path(self.config_path).resolve().parent
        return Path.cwd()

    @property
    def workspace(self) -> Path:
        path = Path(self.security.workspace).expanduser()
        return (path if path.is_absolute() else self.base_dir / path).resolve()

    @property
    def data_dir(self) -> Path:
        path = Path(self.storage.data_dir).expanduser()
        return (path if path.is_absolute() else self.base_dir / path).resolve()

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    def require_api_key(self) -> str:
        if not self.provider.api_key:
            raise ConfigError(
                "provider.api_key is empty. Set it in anyaclaw.toml, or export "
                "ANYACLAW_PROVIDER__API_KEY / OPENAI_API_KEY."
            )
        return self.provider.api_key

    def public_dict(self) -> dict[str, Any]:
        """Config dump safe to expose on a status endpoint."""
        data = to_dict(self)
        provider = data.get("provider", {})
        if provider.get("api_key"):
            provider["api_key"] = "***"
        channels = data.get("channels", {})
        for section in channels.values():
            for key in ("app_secret", "client_secret", "token", "webhook_secret", "encrypt_key"):
                if section.get(key):
                    section[key] = "***"
        security = data.get("security", {})
        if security.get("web_auth_token"):
            security["web_auth_token"] = "***"
        return data

    # -- validation --------------------------------------------------------
    def validate(self) -> None:
        if not self.provider.base_url.startswith(("http://", "https://")):
            raise ConfigError("provider.base_url must start with http:// or https://")
        if not self.provider.model:
            raise ConfigError("provider.model must not be empty")
        if self.provider.timeout <= 0:
            raise ConfigError("provider.timeout must be > 0")
        if self.provider.max_retries < 0:
            raise ConfigError("provider.max_retries must be >= 0")
        if self.agent.max_iterations < 1:
            raise ConfigError("agent.max_iterations must be >= 1")
        if self.agent.max_tool_calls < 0:
            raise ConfigError("agent.max_tool_calls must be >= 0")
        if not 1 <= self.web.port <= 65535:
            raise ConfigError("web.port must be between 1 and 65535")
        if self.security.tool_policy not in {"allow", "confirm", "deny"}:
            raise ConfigError("security.tool_policy must be allow, confirm or deny")
        for name, mode in self.security.tool_policies.items():
            if mode not in {"allow", "confirm", "deny"}:
                raise ConfigError(
                    f"security.tool_policies.{name} must be allow, confirm or deny"
                )
        if self.storage.session_backend not in {"sqlite", "jsonl"}:
            raise ConfigError("storage.session_backend must be sqlite or jsonl")


# --------------------------------------------------------------------------- #
# Loader
# --------------------------------------------------------------------------- #
def find_config_file(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    """Locate the configuration file using CLI arg, env var, then conventional names."""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        return path.resolve()

    env_path = os.environ.get(f"{ENV_PREFIX}CONFIG")
    if env_path:
        path = Path(env_path).expanduser()
        if not path.is_file():
            raise ConfigError(f"ANYACLAW_CONFIG points to a missing file: {path}")
        return path.resolve()

    for name in CONFIG_FILENAMES:
        candidate = Path.cwd() / name
        if candidate.is_file():
            return candidate.resolve()
    return None


def read_config_file(path: Path) -> dict[str, Any]:
    """Read a TOML or JSON config file into a plain dict."""
    try:
        raw = path.read_bytes()
    except OSError as exc:  # pragma: no cover - filesystem dependent
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc

    try:
        if path.suffix.lower() == ".json":
            data = json.loads(raw.decode("utf-8"))
        else:
            data = tomllib.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"invalid config file {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"config file {path} must contain a table/object at the top level")
    return data


def load_config(
    path: str | os.PathLike[str] | None = None,
    *,
    env: dict[str, str] | None = None,
) -> Config:
    """Build a validated :class:`Config` from file + environment."""
    env = os.environ if env is None else env
    config_file = find_config_file(path)

    data: dict[str, Any] = {}
    if config_file is not None:
        data = read_config_file(config_file)

    data = _apply_env_overrides(data, env)
    config = _build(Config, data, "config")
    config.config_path = str(config_file) if config_file else ""

    # Conventional API-key environment variables as a last resort.
    if not config.provider.api_key:
        for name in ("ANYACLAW_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "SILICONFLOW_API_KEY"):
            value = env.get(name, "").strip()
            if value:
                config.provider.api_key = value
                break

    config.validate()
    return config


# --------------------------------------------------------------------------- #
# Env overrides, e.g. ANYACLAW_PROVIDER__API_KEY, ANYACLAW_WEB__PORT
# --------------------------------------------------------------------------- #
def _apply_env_overrides(data: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    result = json.loads(json.dumps(data))  # deep copy of JSON-ish config data
    for key, raw in env.items():
        if not key.startswith(ENV_PREFIX) or key in {f"{ENV_PREFIX}CONFIG", f"{ENV_PREFIX}API_KEY"}:
            continue
        remainder = key[len(ENV_PREFIX) :]
        if "__" not in remainder:
            # Allow both ANYACLAW_LOG_LEVEL and ANYACLAW_LOG__LEVEL.
            remainder = remainder.lower()
        parts = [part.lower() for part in remainder.split("__") if part]
        if not parts:
            continue
        value = _parse_scalar(raw)
        cursor = result
        for part in parts[:-1]:
            node = cursor.get(part)
            if not isinstance(node, dict):
                node = {}
                cursor[part] = node
            cursor = node
        cursor[parts[-1]] = value
    return result


def _parse_scalar(raw: str) -> Any:
    text = raw.strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


# --------------------------------------------------------------------------- #
# Dataclass construction
# --------------------------------------------------------------------------- #
def _build(cls: type, data: Any, path: str) -> Any:
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must be a table/object, got {type(data).__name__}")

    hints = get_type_hints(cls)
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(
            f"unknown key(s) in {path}: {', '.join(unknown)}"
            if path != "config"
            else f"unknown top-level key(s): {', '.join(unknown)}"
        )

    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        if f.name not in data:
            continue
        kwargs[f.name] = _coerce(data[f.name], hints[f.name], f"{path}.{f.name}")

    try:
        return cls(**kwargs)
    except TypeError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"cannot build {path}: {exc}") from exc


def _coerce(value: Any, hint: Any, path: str) -> Any:
    if hint is Any:
        return value

    origin = get_origin(hint)
    args = get_args(hint)

    if origin in (Union, types.UnionType):
        if value is None and type(None) in args:
            return None
        candidates = [arg for arg in args if arg is not type(None)]
        if len(candidates) == 1:
            return _coerce(value, candidates[0], path)
        errors = []
        for candidate in candidates:
            try:
                return _coerce(value, candidate, path)
            except ConfigError as exc:
                errors.append(str(exc))
        raise ConfigError(f"{path}: value {value!r} does not match any allowed type")

    if dataclasses.is_dataclass(hint) and isinstance(hint, type):
        return _build(hint, value, path)

    if origin is list:
        if not isinstance(value, list):
            raise ConfigError(f"{path} must be a list")
        item_hint = args[0] if args else Any
        return [_coerce(item, item_hint, f"{path}[{i}]") for i, item in enumerate(value)]

    if origin is dict:
        if not isinstance(value, dict):
            raise ConfigError(f"{path} must be a table/object")
        key_hint, value_hint = (args + (Any, Any))[:2]
        out: dict[Any, Any] = {}
        for key, item in value.items():
            coerced_key = _coerce(key, key_hint, f"{path}.<key>")
            out[coerced_key] = _coerce(item, value_hint, f"{path}.{key}")
        return out

    if hint is bool:
        return _coerce_bool(value, path)
    if hint is int:
        if isinstance(value, bool):
            raise ConfigError(f"{path} must be an integer, got boolean")
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{path} must be an integer, got {value!r}") from exc
    if hint is float:
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{path} must be a number, got {value!r}") from exc
    if hint is str:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ConfigError(f"{path} must be a string, got {value!r}")
        return _resolve_env_refs(str(value))
    if hint is Path:
        return Path(str(_resolve_env_refs(str(value))))

    return value


def _coerce_bool(value: Any, path: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off", ""}:
            return False
    raise ConfigError(f"{path} must be a boolean, got {value!r}")


def _resolve_env_refs(value: str) -> str:
    """Resolve ``env:VAR`` and ``${VAR}`` / ``${VAR:-default}`` references."""
    if value.startswith("env:"):
        name = value[4:].strip()
        return os.environ.get(name, "")
    return _ENV_PATTERN.sub(
        lambda m: os.environ.get(m.group(1), m.group(2) or ""),
        value,
    )


def to_dict(value: Any) -> Any:
    """Recursively convert dataclasses/Paths into JSON-friendly values."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: to_dict(getattr(value, f.name))
            for f in dataclasses.fields(value)
            if f.name != "config_path"
        }
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): to_dict(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_dict(v) for v in value]
    return value


#: Template written by ``anyaclaw init``; kept in sync with ``config.example.toml``.
DEFAULT_CONFIG_TEMPLATE = """\
# Anyaclaw configuration. Copy to `anyaclaw.toml` and fill in your keys.
# Every value can also be set via environment variables:
#   ANYACLAW_PROVIDER__API_KEY=sk-...   (section__key)

[provider]
api_key = "${ANYACLAW_API_KEY}"
base_url = "https://api.siliconflow.cn/v1"
model = "deepseek-ai/DeepSeek-V3.2"
subagent_model = ""
cheap_model = ""
temperature = 0.7
timeout = 120.0
max_retries = 3
thinking = true
reasoning_effort = ""
reasoning_budget = 0

[agent]
max_iterations = 32
max_tool_calls = 64
tool_timeout = 300.0
context_token_budget = 24000
summarize = true
identity_file = "identity.md"
skills_dir = "skills"

[storage]
data_dir = ".anyaclaw"
session_backend = "sqlite"

[security]
workspace = "."
tool_policy = "confirm"
auto_approve = false
shell_enabled = true
allowed_commands = []
deny_patterns = ["rm -rf /", "del /f /s /q c:\\\\", "format c:"]

[web]
enabled = true
host = "127.0.0.1"
port = 8080

[channels.cli]
enabled = true
prompt = "you"

[channels.qq]
enabled = false
app_id = ""
app_secret = ""

[channels.feishu]
enabled = false
app_id = ""
app_secret = ""

[channels.dingtalk]
enabled = false
client_id = ""
client_secret = ""
webhook_url = ""
webhook_keyword = ""

[tools]
files = true
shell = true
web = true
memory = true
subagent = true
documents = true
mcp = true
"""


def write_default_config(path: str | os.PathLike[str], *, overwrite: bool = False) -> Path:
    """Write the example configuration and return the created path."""
    target = Path(path).expanduser()
    if target.exists() and not overwrite:
        raise ConfigError(f"config already exists: {target} (use --force to overwrite)")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    return target
