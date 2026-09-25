from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Invalid or missing configuration."""


@dataclass(frozen=True)
class PanelConfig:
    base_url: str
    username: str
    password: str
    verify_tls: bool = True


@dataclass(frozen=True)
class CheckConfig:
    interval_seconds: int = 10
    timeout_seconds: int = 8
    fail_threshold: int = 3
    restart_cooldown_seconds: int = 90
    max_restarts_per_hour: int = 6
    stale_after_seconds: int = 20
    outbound_tag: str = "warp"


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str = ""
    chat_id: str = ""
    # Optional HTTP/SOCKS proxy for api.telegram.org, e.g. socks5://127.0.0.1:1080
    proxy: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)


DEFAULT_STATUS_FILE = "/var/lib/warp-healthcheck/status.json"


@dataclass(frozen=True)
class AppConfig:
    panel: PanelConfig
    check: CheckConfig = field(default_factory=CheckConfig)
    core_ids: tuple[int, ...] = ()
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    status_file: Path = Path(DEFAULT_STATUS_FILE)

    @property
    def observatory_probe_interval(self) -> str:
        return f"{self.check.interval_seconds}s"


def default_config_paths() -> list[Path]:
    env_path = os.environ.get("WARP_HEALTHCHECK_CONFIG")
    paths: list[Path] = []
    if env_path:
        paths.append(Path(env_path))
    paths.append(Path("config.yaml"))
    paths.append(Path("/etc/warp-healthcheck/config.yaml"))
    return paths


def resolve_config_path(cli_path: str | None) -> Path:
    if cli_path:
        path = Path(cli_path)
        if not path.is_file():
            raise ConfigError(f"Config file not found: {path}")
        return path
    for path in default_config_paths():
        if path.is_file():
            return path
    raise ConfigError(
        "No config file found. Copy config.example.yaml to config.yaml "
        "or pass --config /path/to/config.yaml"
    )


def _require_str(data: dict[str, Any], key: str, *, source: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{source}.{key} is required")
    return value.strip()


def _as_int(value: Any, default: int, *, name: str, minimum: int = 1) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if parsed < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return parsed


def _as_bool(value: Any, default: bool, *, name: str) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    raise ConfigError(f"{name} must be a boolean")


def load_config(path: Path) -> AppConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except PermissionError as exc:
        raise ConfigError(f"Cannot read {path} (permission denied). Run with sudo.") from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("Config root must be a mapping")

    panel_raw = raw.get("panel") or {}
    if not isinstance(panel_raw, dict):
        raise ConfigError("panel must be a mapping")

    check_raw = raw.get("check") or {}
    if not isinstance(check_raw, dict):
        raise ConfigError("check must be a mapping")

    base_url = os.environ.get("PASARGUARD_BASE_URL") or _require_str(
        panel_raw, "base_url", source="panel"
    )
    username = os.environ.get("PASARGUARD_USERNAME") or _require_str(
        panel_raw, "username", source="panel"
    )
    password = os.environ.get("PASARGUARD_PASSWORD") or _require_str(
        panel_raw, "password", source="panel"
    )

    core_ids_raw = raw.get("core_ids") or []
    if core_ids_raw is None:
        core_ids_raw = []
    if not isinstance(core_ids_raw, list):
        raise ConfigError("core_ids must be a list of integers")
    core_ids: list[int] = []
    for item in core_ids_raw:
        try:
            core_ids.append(int(item))
        except (TypeError, ValueError) as exc:
            raise ConfigError("core_ids must be a list of integers") from exc

    interval = _as_int(
        check_raw.get("interval_seconds"), 10, name="check.interval_seconds"
    )
    stale_default = interval * 2
    stale = check_raw.get("stale_after_seconds")
    stale_after = stale_default if stale is None else _as_int(
        stale, stale_default, name="check.stale_after_seconds"
    )

    outbound_tag = str(check_raw.get("outbound_tag") or "warp").strip() or "warp"

    telegram_raw = raw.get("telegram") or {}
    if not isinstance(telegram_raw, dict):
        raise ConfigError("telegram must be a mapping")
    telegram = TelegramConfig(
        bot_token=str(
            os.environ.get("WARP_HEALTHCHECK_TELEGRAM_TOKEN")
            or telegram_raw.get("bot_token")
            or ""
        ).strip(),
        chat_id=str(
            os.environ.get("WARP_HEALTHCHECK_TELEGRAM_CHAT_ID")
            or telegram_raw.get("chat_id")
            or ""
        ).strip(),
        proxy=str(telegram_raw.get("proxy") or "").strip(),
    )

    status_file = Path(
        os.environ.get("WARP_HEALTHCHECK_STATUS_FILE")
        or str(raw.get("status_file") or "").strip()
        or DEFAULT_STATUS_FILE
    )

    return AppConfig(
        panel=PanelConfig(
            base_url=base_url.rstrip("/"),
            username=username,
            password=password,
            verify_tls=_as_bool(
                panel_raw.get("verify_tls"), True, name="panel.verify_tls"
            ),
        ),
        check=CheckConfig(
            interval_seconds=interval,
            timeout_seconds=_as_int(
                check_raw.get("timeout_seconds"), 8, name="check.timeout_seconds"
            ),
            fail_threshold=_as_int(
                check_raw.get("fail_threshold"), 3, name="check.fail_threshold"
            ),
            restart_cooldown_seconds=_as_int(
                check_raw.get("restart_cooldown_seconds"),
                90,
                name="check.restart_cooldown_seconds",
                minimum=0,
            ),
            max_restarts_per_hour=_as_int(
                check_raw.get("max_restarts_per_hour"),
                6,
                name="check.max_restarts_per_hour",
            ),
            stale_after_seconds=stale_after,
            outbound_tag=outbound_tag,
        ),
        core_ids=tuple(core_ids),
        telegram=telegram,
        status_file=status_file,
    )
