"""Terminal status view and management menu (stdlib only, plain ANSI)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from warp_healthcheck import GITHUB_REPO, RAW_BASE_URL, __version__
from warp_healthcheck.config import AppConfig, ConfigError, load_config

SERVICE = "warp-healthcheck"
INSTALLER = Path("/opt/warp-healthcheck/app/install.sh")
SPARKS = "▁▂▃▄▅▆▇█"


# ---------------------------------------------------------------------- output


def _color_enabled() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _unicode_ok() -> bool:
    return (sys.stdout.encoding or "").lower().replace("-", "").startswith("utf")


class Style:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def cyan(self, text: str) -> str:
        return self._wrap("36", text)


_ANSI = re.compile(r"\033\[[0-9;]*m")


def _visible_len(text: str) -> int:
    return len(_ANSI.sub("", text))


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _visible_len(text))


def _truncate(text: str, width: int) -> str:
    if width <= 1 or len(text) <= width:
        return text
    return text[: width - 1] + "…"


def _ago(ts: float | None, now: float) -> str:
    if not ts:
        return "never"
    seconds = int(now - ts)
    if seconds < 0:
        seconds = 0
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{seconds // 60}m ago"
    if seconds < 172800:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def sparkline(history: list[int | None], *, unicode: bool = True) -> str:
    values = [v for v in history if v is not None]
    if not history:
        return ""
    lo, hi = (min(values), max(values)) if values else (0, 0)
    chars = SPARKS if unicode else "_.-=#"
    out = []
    for value in history:
        if value is None:
            out.append("x")
        elif hi == lo:
            out.append(chars[len(chars) // 2])
        else:
            index = round((value - lo) / (hi - lo) * (len(chars) - 1))
            out.append(chars[index])
    return "".join(out)


def _ping_text(style: Style, delay: int | None) -> str:
    if delay is None:
        return style.dim("—")
    text = f"{delay}ms"
    if delay < 300:
        return style.green(text)
    if delay < 800:
        return style.yellow(text)
    return style.red(text)


def _probe_text(style: Style, probe: str) -> str:
    return {
        "up": style.green("UP"),
        "down": style.red("DOWN"),
        "unknown": style.yellow("UNKNOWN"),
        "skip": style.dim("OFFLINE"),
    }.get(probe, probe)


def _state_text(style: Style, state: str) -> str:
    return {
        "ok": style.green("OK"),
        "down": style.red("DOWN"),
        "degraded": style.yellow("DEGRADED"),
        "cooldown": style.cyan("RESTARTING"),
        "no-nodes": style.dim("NO NODES"),
        "error": style.red("ERROR"),
        "pending": style.dim("PENDING"),
    }.get(state, state.upper())


# ---------------------------------------------------------------------- status


def read_status(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def service_state() -> str:
    if not shutil.which("systemctl"):
        return "unknown"
    result = subprocess.run(
        ["systemctl", "is-active", SERVICE],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or "unknown"


def render_status(
    status: dict[str, Any] | None,
    *,
    service: str,
    style: Style,
    now: float | None = None,
    width: int | None = None,
) -> str:
    now = time.time() if now is None else now
    width = width or shutil.get_terminal_size((100, 30)).columns
    unicode = _unicode_ok()
    lines: list[str] = []

    service_label = {
        "active": style.green("running"),
        "inactive": style.red("stopped"),
        "failed": style.red("failed"),
        "activating": style.yellow("starting"),
    }.get(service, style.dim(service))
    lines.append(
        f"{style.bold('Warp health check')} v{__version__}   service: {service_label}"
    )

    if status is None:
        lines.append("")
        lines.append(
            style.yellow("No status yet.")
            + " The service writes it after its first check cycle."
        )
        if service != "active":
            lines.append(f"Start it with: systemctl start {SERVICE}")
        return "\n".join(lines)

    updated = status.get("updated_at") or 0
    interval = int(status.get("interval_seconds") or 10)
    age_text = _ago(updated, now)
    if now - updated > max(30, interval * 3):
        age_text = style.red(f"{age_text} (stale — is the service running?)")
    telegram = style.green("on") if status.get("telegram") else style.dim("off")
    lines.append(
        style.dim(
            f"tag '{status.get('tag')}' · every {interval}s · "
            f"restart after {status.get('fail_threshold')} fails · "
            f"max {status.get('max_restarts_per_hour')}/h · "
        )
        + f"telegram {telegram}"
        + style.dim(f" · updated {age_text}")
    )
    if status.get("version") and status["version"] != __version__:
        lines.append(
            style.yellow(
                f"Service is running v{status['version']}; restart it to load v{__version__}."
            )
        )
    cycle = status.get("cycle") or {}
    if not cycle.get("ok", True):
        lines.append(style.red(f"Last cycle failed: {cycle.get('error')}"))

    cores = status.get("cores") or []
    if not cores:
        lines.append("")
        lines.append(style.yellow("No Warp cores found."))

    name_w = max(
        [len(n.get("name", "")) for c in cores for n in c.get("nodes", [])] + [8]
    )
    name_w = min(name_w, 24)
    for core in cores:
        lines.append("")
        core_title = style.bold(f"Core {core['id']}")
        header = (
            f"{core_title} {core.get('name', '')}  "
            f"[{_state_text(style, core.get('state', ''))}]"
        )
        extras = [f"restarts {core.get('restarts_last_hour', 0)}/h"]
        if core.get("last_restart_at"):
            extras.append(f"last {_ago(core['last_restart_at'], now)}")
        cooldown = (core.get("cooldown_until") or 0) - now
        if cooldown > 0:
            extras.append(style.cyan(f"cooldown {int(cooldown)}s"))
        if not core.get("observatory"):
            extras.append(style.yellow("no observatory"))
        if core.get("capped"):
            extras.append(style.red("restart cap reached"))
        lines.append(header + "  " + style.dim(" · ").join(extras))
        if core.get("error"):
            lines.append("  " + style.red(_truncate(core["error"], width - 2)))

        nodes = core.get("nodes") or []
        if not nodes:
            lines.append(style.dim("  (no nodes)"))
            continue
        lines.append(
            style.dim(
                "  "
                + _pad("NODE", name_w + 2)
                + _pad("WARP", 9)
                + _pad("PING", 8)
                + _pad("FAILS", 7)
                + _pad("LAST 30", 32)
                + "DETAIL"
            )
        )
        for node in nodes:
            spark = sparkline(node.get("history") or [], unicode=unicode)
            spark = spark.replace("x", style.red("x"))
            fails = int(node.get("fails") or 0)
            fails_text = style.red(str(fails)) if fails else style.dim("0")
            row = (
                "  "
                + _pad(_truncate(node.get("name", ""), name_w), name_w + 2)
                + _pad(_probe_text(style, node.get("probe", "")), 9)
                + _pad(_ping_text(style, node.get("delay_ms")), 8)
                + _pad(fails_text, 7)
                + _pad(spark, 32)
            )
            detail = node.get("detail", "")
            if node.get("probe") == "up":
                detail = ""
            room = width - _visible_len(row) - 1
            if detail and room > 10:
                row += style.dim(_truncate(detail, room))
            lines.append(row)

    events = status.get("events") or []
    if events:
        lines.append("")
        lines.append(style.bold("Recent events"))
        for event in events[-5:][::-1]:
            kind = event.get("kind", "")
            color = {
                "restart": style.cyan,
                "recovered": style.green,
                "capped": style.red,
                "restart-failed": style.red,
            }.get(kind, style.dim)
            prefix = (
                f"  {_pad(_ago(event.get('time'), now), 9)}"
                f"{color(_pad(kind, 15))}{event.get('core', '')}: "
            )
            room = max(10, width - _visible_len(prefix) - 1)
            lines.append(prefix + _truncate(str(event.get("message", "")), room))
    return "\n".join(lines)


def print_status(config: AppConfig) -> None:
    style = Style(_color_enabled())
    print(render_status(read_status(config.status_file), service=service_state(), style=style))


def watch_status(config: AppConfig, refresh: float = 2.0) -> None:
    style = Style(_color_enabled())
    try:
        while True:
            text = render_status(
                read_status(config.status_file), service=service_state(), style=style
            )
            sys.stdout.write("\033[H\033[J" + text + "\n\n" + style.dim("Ctrl+C to go back") + "\n")
            sys.stdout.flush()
            time.sleep(refresh)
    except KeyboardInterrupt:
        print()


# ---------------------------------------------------------------------- actions


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def require_root(action: str) -> bool:
    if _is_root():
        return True
    print(f"{action} needs root. Re-run with: sudo warp-healthcheck")
    return False


def confirm(question: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        answer = input(f"{question} [{hint}] ").strip().lower()
    except EOFError:
        return False
    if not answer:
        return default
    return answer in {"y", "yes"}


def _run(cmd: list[str]) -> int:
    try:
        return subprocess.run(cmd, check=False).returncode
    except KeyboardInterrupt:
        return 130
    except FileNotFoundError:
        print(f"{cmd[0]} not found")
        return 127


def systemctl(action: str) -> int:
    if not require_root(f"systemctl {action}"):
        return 1
    code = _run(["systemctl", action, SERVICE])
    if code == 0:
        print(f"{SERVICE}: {action} OK ({service_state()})")
    return code


def show_logs(follow: bool = True, lines: int = 100) -> int:
    cmd = ["journalctl", "-u", SERVICE, "-n", str(lines), "--no-pager"]
    if follow:
        cmd.append("-f")
    return _run(cmd)


def fetch_latest_version(timeout: float = 10.0) -> str | None:
    try:
        response = httpx.get(
            f"{RAW_BASE_URL}/src/warp_healthcheck/__init__.py",
            timeout=timeout,
            follow_redirects=True,
        )
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    match = re.search(r'__version__\s*=\s*"([^"]+)"', response.text)
    return match.group(1) if match else None


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version))


def confirm_update(*, assume_yes: bool = False) -> bool:
    """Print installed/latest versions; return True if the update should run."""
    print(f"Installed: v{__version__}")
    latest = fetch_latest_version()
    if latest is None:
        print(f"Could not read the latest version from github.com/{GITHUB_REPO}.")
        return assume_yes or confirm("Try updating anyway?")
    print(f"Latest:    v{latest}")
    if _version_tuple(latest) <= _version_tuple(__version__):
        print("Already up to date.")
        return not assume_yes and confirm("Reinstall anyway?")
    return assume_yes or confirm(f"Update to v{latest}?", default=True)


def perform_update() -> int:
    """Download the latest installer from GitHub and run `install.sh update`."""
    if not require_root("Updating"):
        return 1
    try:
        response = httpx.get(f"{RAW_BASE_URL}/install.sh", timeout=30.0, follow_redirects=True)
    except httpx.HTTPError as exc:
        print(f"Download failed: {exc}")
        return 1
    if response.status_code != 200 or not response.text.startswith("#!"):
        print(f"Download failed: HTTP {response.status_code}")
        return 1
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, encoding="utf-8") as fh:
        fh.write(response.text)
        script = fh.name
    try:
        return _run(["bash", script, "update"])
    finally:
        os.unlink(script)


def edit_config(config_path: Path) -> int:
    if not require_root("Editing the config"):
        return 1
    editor = os.environ.get("EDITOR") or next(
        (e for e in ("nano", "vim", "vi") if shutil.which(e)), None
    )
    if editor is None:
        print(f"No editor found. Edit {config_path} manually.")
        return 1
    before = config_path.read_bytes()
    _run([editor, str(config_path)])
    if config_path.read_bytes() == before:
        print("No changes.")
        return 0
    try:
        load_config(config_path)
    except ConfigError as exc:
        print(f"Config is invalid: {exc}")
        print("The service was not restarted. Fix the file and try again.")
        return 1
    print("Config OK.")
    if confirm("Restart the service to apply it?", default=True):
        return systemctl("restart")
    return 0


def restart_core_interactive(config: AppConfig) -> int:
    from warp_healthcheck.cli import make_client

    status = read_status(config.status_file) or {}
    cores = status.get("cores") or []
    if cores:
        for core in cores:
            print(f"  {core['id']}) {core.get('name', '')} [{core.get('state', '')}]")
    try:
        raw = input("Core ID to restart (empty to cancel): ").strip()
    except EOFError:
        return 1
    if not raw:
        return 0
    if not raw.isdigit():
        print("Not a number.")
        return 1
    core_id = int(raw)
    if not confirm(f"Restart core {core_id}? This restarts every node that uses it."):
        return 1

    async def _do() -> None:
        async with make_client(config) as client:
            await client.restart_core(core_id)

    from warp_healthcheck.models import PanelError

    try:
        asyncio.run(_do())
    except PanelError as exc:
        print(f"Restart failed: {exc}")
        return 1
    print(f"Restart requested for core {core_id}.")
    return 0


def test_telegram(config: AppConfig) -> int:
    from warp_healthcheck.notify import Notifier

    if not config.telegram.enabled:
        print("Telegram is not configured. Set telegram.bot_token and telegram.chat_id in the config.")
        return 1
    ok = asyncio.run(Notifier(config.telegram).send("Test message: alerts are working."))
    print("Sent." if ok else "Failed; see the warning above.")
    return 0 if ok else 1


def uninstall() -> int:
    if not require_root("Uninstalling"):
        return 1
    if not INSTALLER.is_file():
        print(f"{INSTALLER} not found.")
        return 1
    if not confirm("Uninstall warp-healthcheck? The config in /etc/warp-healthcheck is kept."):
        return 1
    return _run(["bash", str(INSTALLER), "uninstall"])


# ---------------------------------------------------------------------- menu


MENU = [
    ("1", "Live status"),
    ("2", "Probe now (doctor)"),
    ("3", "Service logs"),
    ("4", "Restart service"),
    ("5", "Stop service"),
    ("6", "Start service"),
    ("7", "Edit config"),
    ("8", "Restart a core now"),
    ("9", "Set up Observatory"),
    ("10", "Send Telegram test"),
    ("11", "Update from GitHub"),
    ("12", "Uninstall"),
    ("0", "Exit"),
]


def _pause() -> None:
    try:
        input("\nPress Enter to continue...")
    except (EOFError, KeyboardInterrupt):
        pass


def run_menu(config: AppConfig, config_path: Path) -> int:
    from warp_healthcheck import cli

    style = Style(_color_enabled())
    while True:
        if style.enabled:
            sys.stdout.write("\033[H\033[2J")
        print(render_status(read_status(config.status_file), service=service_state(), style=style))
        print()
        half = (len(MENU) + 1) // 2
        left, right = MENU[:half], MENU[half:]
        for index, (key, label) in enumerate(left):
            cell = _pad(f"{style.cyan(key.rjust(2))}) {label}", 30)
            if index < len(right):
                rkey, rlabel = right[index]
                cell += f"{style.cyan(rkey.rjust(2))}) {rlabel}"
            print(cell)
        try:
            choice = input("\nSelect: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        try:
            if choice == "0" or choice.lower() in {"q", "exit"}:
                return 0
            if choice in {"", "r"}:
                continue
            if choice == "1":
                watch_status(config)
                continue
            if choice == "2":
                try:
                    asyncio.run(cli.cmd_doctor(config))
                except Exception as exc:  # noqa: BLE001 - show and return to menu
                    print(f"doctor failed: {exc}")
            elif choice == "3":
                show_logs()
            elif choice == "4":
                systemctl("restart")
            elif choice == "5":
                if confirm("Stop the service? Warp will not be monitored."):
                    systemctl("stop")
            elif choice == "6":
                systemctl("start")
            elif choice == "7":
                edit_config(config_path)
                try:
                    config = load_config(config_path)
                except ConfigError:
                    pass
            elif choice == "8":
                restart_core_interactive(config)
            elif choice == "9":
                try:
                    asyncio.run(cli.cmd_setup(config, dry_run=False, yes=False))
                except Exception as exc:  # noqa: BLE001
                    print(f"setup failed: {exc}")
            elif choice == "10":
                test_telegram(config)
            elif choice == "11":
                if require_root("Updating") and confirm_update():
                    if perform_update() == 0:
                        print("\nUpdate finished. Reopen the menu to use the new version.")
                        return 0
            elif choice == "12":
                if uninstall() == 0:
                    return 0
            else:
                print("Unknown option.")
        except KeyboardInterrupt:
            print()
        _pause()
