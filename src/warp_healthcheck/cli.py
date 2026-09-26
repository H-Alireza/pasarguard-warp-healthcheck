from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from collections.abc import Sequence

from warp_healthcheck import __version__
from warp_healthcheck.checker import HealthChecker, ProbeStatus
from warp_healthcheck.config import AppConfig, ConfigError, load_config, resolve_config_path
from warp_healthcheck.models import PanelError
from warp_healthcheck.observatory import (
    DEFAULT_PROBE_URL,
    ensure_observatory,
    has_misspelled_probe_url,
    observatory_has_tag,
    observatory_interval_seconds,
)
from warp_healthcheck.panel import PanelClient

logger = logging.getLogger("warp-healthcheck")

# The panel waits up to timeout_seconds for the node, so our own read timeout
# has to be a little longer or we race the panel's answer.
REQUEST_MARGIN_SECONDS = 5

# doctor exit codes: 0 all good; 1 cannot monitor (auth, panel, no Warp cores);
# DOCTOR_WARNINGS = panel OK but some nodes are not UP or Observatory needs work.
DOCTOR_WARNINGS = 3


def _configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # httpx logs every request at INFO; that is noise in the journal.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def make_client(config: AppConfig) -> PanelClient:
    return PanelClient(
        config.panel.base_url,
        config.panel.username,
        config.panel.password,
        timeout=config.check.timeout_seconds + REQUEST_MARGIN_SECONDS,
        verify_tls=config.panel.verify_tls,
    )


def _install_stop_signals(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, lambda _signum, _frame: stop.set())


async def _cmd_run(config: AppConfig) -> int:
    stop = asyncio.Event()
    _install_stop_signals(stop)
    async with make_client(config) as client:
        await client.login()
        checker = HealthChecker(config, client)
        await checker.run_forever(stop)
    logger.info("Stopped")
    return 0


async def cmd_doctor(config: AppConfig) -> int:
    async with make_client(config) as client:
        await client.login()
        admin = await client.get_current_admin()
        username = admin.get("username") or config.panel.username
        print(f"Panel OK as {username} @ {config.panel.base_url}")
        if config.telegram.enabled:
            print("Telegram alerts: on")

        checker = HealthChecker(config, client)
        cores = await checker.discover_warp_cores()
        if not cores:
            print("No Warp cores found.")
            return 1

        exit_code = 0
        check = config.check
        tag = check.outbound_tag
        for core in cores:
            has_obs = observatory_has_tag(core.config, tag)
            print(f"\nCore {core.id} {core.name}  observatory={'yes' if has_obs else 'NO'}")
            if not has_obs:
                print("  ! No Observatory for this tag. Run: warp-healthcheck setup")
                exit_code = DOCTOR_WARNINGS
            if has_misspelled_probe_url(core.config):
                print(
                    "  ! Observatory uses 'probeUrl', which Xray ignores (the key is "
                    "'probeURL'), so it probes Xray's default URL. Run: warp-healthcheck setup"
                )
                exit_code = DOCTOR_WARNINGS
            interval = observatory_interval_seconds(core.config, tag)
            if interval is not None and interval > check.stale_after_seconds:
                print(
                    f"  ! Observatory probes every {interval:g}s but results older than "
                    f"{check.stale_after_seconds}s count as stale, so Warp will always be "
                    "UNKNOWN and never restarted. Lower probeInterval on the core or raise "
                    "check.stale_after_seconds."
                )
                exit_code = DOCTOR_WARNINGS
            nodes = await client.list_nodes(core_id=core.id)
            if not nodes:
                print("  (no nodes)")
                continue
            for node in nodes:
                if not node.connected:
                    print(f"  node {node.id} {node.name} [{node.status}] skipped")
                    continue
                probe = await checker.probe_node(node)
                mark = {
                    ProbeStatus.UP: "UP",
                    ProbeStatus.DOWN: "DOWN",
                    ProbeStatus.UNKNOWN: "UNKNOWN",
                    ProbeStatus.SKIP: "SKIP",
                }[probe.status]
                extra = f" delay={probe.delay_ms}ms" if probe.delay_ms else ""
                print(f"  node {node.id} {node.name} [{node.status}] {mark}{extra} — {probe.detail}")
                if probe.status is not ProbeStatus.UP:
                    exit_code = DOCTOR_WARNINGS
        return exit_code


async def cmd_setup(config: AppConfig, *, dry_run: bool, yes: bool) -> int:
    tag = config.check.outbound_tag
    async with make_client(config) as client:
        await client.login()
        checker = HealthChecker(config, client)
        cores = await checker.discover_warp_cores()
        if not cores:
            print("No Warp cores found.")
            return 1

        changes = []
        for core in cores:
            new_config, changed = ensure_observatory(
                core.config,
                tag,
                probe_url=DEFAULT_PROBE_URL,
                probe_interval=config.observatory_probe_interval,
            )
            if changed:
                changes.append((core, new_config))
            else:
                print(f"Core {core.id} {core.name}: Observatory already covers '{tag}'")

        if not changes:
            print("Nothing to change.")
            return 0

        print(f"{len(changes)} core(s) need Observatory for tag '{tag}':")
        for core, _config in changes:
            print(f"  - {core.id} {core.name}")

        if dry_run:
            print("Dry run; no cores were updated.")
            return 0
        if not yes:
            answer = input("Apply and restart those cores' nodes? [y/N] ").strip().lower()
            if answer not in {"y", "yes"}:
                print("Aborted.")
                return 1

        for core, new_config in changes:
            await client.update_core(core, new_config, restart_nodes=True)
            print(f"Updated core {core.id} {core.name} and restarted its nodes.")
        return 0


async def _cmd_restart_core(config: AppConfig, core_id: int) -> int:
    async with make_client(config) as client:
        await client.restart_core(core_id)
    print(f"Restart requested for core {core_id}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="warp-healthcheck",
        description=(
            "Probe PasarGuard Warp outbounds and restart unhealthy cores. "
            "Run without a command to open the menu."
        ),
    )
    parser.add_argument("-c", "--config", help="Path to config.yaml")
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("menu", help="Open the interactive menu (default)")
    sub.add_parser("run", help="Run the health-check loop (used by systemd)")
    sub.add_parser("doctor", help="Check panel auth, Warp cores, and one latency probe")

    status = sub.add_parser("status", help="Show the last status written by the service")
    status.add_argument("-w", "--watch", action="store_true", help="Refresh every 2s")
    status.add_argument("--json", action="store_true", help="Print the raw status JSON")

    setup = sub.add_parser(
        "setup",
        help="Add Xray Observatory for the Warp tag on matching cores",
    )
    setup.add_argument(
        "--dry-run",
        action="store_true",
        help="Show which cores would change without writing them",
    )
    setup.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Apply changes without prompting",
    )

    restart = sub.add_parser("restart-core", help="Restart one core now")
    restart.add_argument("core_id", type=int)
    restart.add_argument("-y", "--yes", action="store_true", help="Do not ask for confirmation")

    update = sub.add_parser("update", help="Update to the latest version from GitHub")
    update.add_argument("-y", "--yes", action="store_true", help="Do not ask for confirmation")

    logs = sub.add_parser("logs", help="Show service logs")
    logs.add_argument("-n", "--lines", type=int, default=100)
    logs.add_argument("--no-follow", action="store_true")

    sub.add_parser("test-telegram", help="Send a test Telegram message")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command
    if command is None:
        if not sys.stdin.isatty():
            parser.print_help()
            return 2
        command = "menu"

    _configure_logging(logging.INFO if command in {"run", "doctor", "setup"} else logging.WARNING)

    # These do not need the config file.
    from warp_healthcheck import menu

    if command == "logs":
        return menu.show_logs(follow=not args.no_follow, lines=args.lines)
    if command == "update":
        if not menu.require_root("Updating"):
            return 1
        if not menu.confirm_update(assume_yes=args.yes):
            return 0
        return menu.perform_update()

    try:
        config_path = resolve_config_path(args.config)
        config = load_config(config_path)
        logger.info("Using config %s", config_path)
    except ConfigError as exc:
        logger.error("%s", exc)
        return 2

    try:
        if command == "run":
            return asyncio.run(_cmd_run(config))
        if command == "doctor":
            return asyncio.run(cmd_doctor(config))
        if command == "setup":
            return asyncio.run(cmd_setup(config, dry_run=args.dry_run, yes=args.yes))
        if command == "status":
            if args.json:
                data = menu.read_status(config.status_file)
                print(json.dumps(data, indent=2))
                return 0 if data is not None else 1
            if args.watch:
                menu.watch_status(config)
            else:
                menu.print_status(config)
            return 0
        if command == "restart-core":
            if not args.yes and not menu.confirm(
                f"Restart core {args.core_id}? This restarts every node that uses it."
            ):
                return 1
            return asyncio.run(_cmd_restart_core(config, args.core_id))
        if command == "test-telegram":
            return menu.test_telegram(config)
        if command == "menu":
            return menu.run_menu(config, config_path)
    except PanelError as exc:
        logger.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        logger.info("Interrupted")
        return 130
    parser.error(f"Unknown command {command}")
    return 2
