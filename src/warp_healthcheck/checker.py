from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from warp_healthcheck import __version__
from warp_healthcheck.config import AppConfig
from warp_healthcheck.models import (
    CoreInfo,
    NodeInfo,
    OutboundLatency,
    PanelAuthError,
    PanelError,
    PanelUnavailable,
    core_has_warp,
)
from warp_healthcheck.notify import Notifier
from warp_healthcheck.observatory import observatory_has_tag
from warp_healthcheck.panel import PanelClient

logger = logging.getLogger("warp-healthcheck")

HOUR_SECONDS = 3600.0
HISTORY_LENGTH = 30
MAX_EVENTS = 50
SUMMARY_EVERY_SECONDS = 300.0


class ProbeStatus(str, Enum):
    UP = "up"
    DOWN = "down"
    UNKNOWN = "unknown"
    SKIP = "skip"


@dataclass(frozen=True)
class ProbeResult:
    node: NodeInfo
    status: ProbeStatus
    detail: str
    delay_ms: int = 0
    checked_at: float = 0.0


@dataclass
class CoreRuntime:
    consecutive_failures: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    cooldown_until: float = 0.0
    restart_times: deque[float] = field(default_factory=deque)
    # For change-only logging and the status file.
    last_status: dict[int, ProbeStatus] = field(default_factory=dict)
    last_probes: dict[int, ProbeResult] = field(default_factory=dict)
    history: dict[int, deque[int | None]] = field(
        default_factory=lambda: defaultdict(lambda: deque(maxlen=HISTORY_LENGTH))
    )
    name: str = ""
    observatory: bool = False
    capped: bool = False
    awaiting_recovery: bool = False
    state: str = "pending"
    error: str = ""


def _unix_seconds(value: int) -> float | None:
    if not value:
        return None
    if value > 10_000_000_000_000_000:
        return value / 1_000_000_000.0
    if value > 1_000_000_000_000:
        return value / 1000.0
    return float(value)


def classify_latency(
    latencies: list[OutboundLatency],
    tag: str,
    *,
    now: float,
    stale_after_seconds: int,
) -> tuple[ProbeStatus, str, int]:
    matching = [item for item in latencies if item.name == tag or item.link == tag]
    if not matching:
        # No result for the tag means Observatory does not watch it (yet).
        # That is a config problem, not a dead tunnel, so never restart for it.
        names = ", ".join(sorted({item.name for item in latencies if item.name}))
        suffix = f" (got: {names})" if names else ""
        return (
            ProbeStatus.UNKNOWN,
            f"No Observatory result for '{tag}'{suffix}; run `warp-healthcheck setup`",
            0,
        )

    sample = matching[0]
    last_try = _unix_seconds(sample.last_try_time)
    last_seen = _unix_seconds(sample.last_seen_time)
    if last_try is None and last_seen is None and not sample.alive:
        return ProbeStatus.UNKNOWN, "Observatory has not probed Warp yet", sample.delay

    if last_try is not None and now - last_try > stale_after_seconds:
        age = int(now - last_try)
        return (
            ProbeStatus.UNKNOWN,
            f"Stale Observatory result (last try {age}s ago)",
            sample.delay,
        )

    if sample.alive:
        return ProbeStatus.UP, f"alive delay={sample.delay}ms", sample.delay
    return ProbeStatus.DOWN, f"Observatory reports Warp dead delay={sample.delay}ms", sample.delay


def _is_observatory_missing(error: PanelError) -> bool:
    text = str(error).lower()
    needles = (
        "latency not found",
        "not available",
        "observatory",
        "debug/vars",
    )
    return any(needle in text for needle in needles)


def _node_label(node: NodeInfo) -> str:
    return f"{node.name}#{node.id}"


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


class HealthChecker:
    def __init__(
        self,
        config: AppConfig,
        client: PanelClient,
        notifier: Notifier | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._notifier = notifier or Notifier(config.telegram)
        self._state: dict[int, CoreRuntime] = defaultdict(CoreRuntime)
        self._core_order: list[int] = []
        self._events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self._started_at = time.time()
        self._last_summary = time.monotonic()
        self._status_write_failed = False

    # ------------------------------------------------------------------ discovery

    async def discover_warp_cores(self) -> list[CoreInfo]:
        tag = self._config.check.outbound_tag
        if self._config.core_ids:
            cores: list[CoreInfo] = []
            for core_id in self._config.core_ids:
                core = await self._client.get_core(core_id)
                if core_has_warp(core, tag):
                    cores.append(core)
                else:
                    logger.warning(
                        "Core %s (%s) has no wireguard outbound tagged %s; skipping",
                        core.id,
                        core.name,
                        tag,
                    )
            return cores

        cores = await self._client.list_cores()
        return [core for core in cores if core_has_warp(core, tag)]

    # ------------------------------------------------------------------ probing

    async def probe_node(self, node: NodeInfo) -> ProbeResult:
        now = time.time()
        if not node.connected:
            return ProbeResult(
                node=node,
                status=ProbeStatus.SKIP,
                detail=f"node status is {node.status or 'unknown'}",
                checked_at=now,
            )
        try:
            latencies = await self._client.get_outbounds_latency(
                node.id,
                name=self._config.check.outbound_tag,
                timeout=self._config.check.timeout_seconds,
            )
        except PanelAuthError:
            raise
        except PanelUnavailable as exc:
            # A timeout on one node's latency call must not abort the cycle for
            # every other core. We can't tell what Warp is doing, so: unknown.
            return ProbeResult(
                node=node,
                status=ProbeStatus.UNKNOWN,
                detail=f"latency request failed: {exc}",
                checked_at=now,
            )
        except PanelError as exc:
            if _is_observatory_missing(exc):
                return ProbeResult(
                    node=node,
                    status=ProbeStatus.UNKNOWN,
                    detail=f"Observatory unavailable: {exc}",
                    checked_at=now,
                )
            return ProbeResult(
                node=node, status=ProbeStatus.DOWN, detail=str(exc), checked_at=now
            )

        status, detail, delay = classify_latency(
            latencies,
            self._config.check.outbound_tag,
            now=time.time(),
            stale_after_seconds=self._config.check.stale_after_seconds,
        )
        return ProbeResult(
            node=node, status=status, detail=detail, delay_ms=delay, checked_at=now
        )

    # ------------------------------------------------------------------ events

    def _event(self, core: CoreInfo, kind: str, message: str) -> None:
        self._events.append(
            {
                "time": time.time(),
                "core_id": core.id,
                "core": core.name,
                "kind": kind,
                "message": message,
            }
        )

    async def _notify(self, text: str) -> None:
        await self._notifier.send(text)

    # ------------------------------------------------------------------ per core

    def _record_probe(self, runtime: CoreRuntime, label: str, probe: ProbeResult) -> None:
        node_id = probe.node.id
        previous = runtime.last_status.get(node_id)
        runtime.last_status[node_id] = probe.status
        runtime.last_probes[node_id] = probe
        runtime.history[node_id].append(
            probe.delay_ms if probe.status is ProbeStatus.UP else None
        )
        node_label = _node_label(probe.node)
        check = self._config.check

        if probe.status is ProbeStatus.UP:
            runtime.consecutive_failures[node_id] = 0
            if previous is not ProbeStatus.UP:
                logger.info("Core %s node %s Warp up: %s", label, node_label, probe.detail)
        elif probe.status is ProbeStatus.DOWN:
            runtime.consecutive_failures[node_id] += 1
            logger.warning(
                "Core %s node %s Warp down (%s/%s): %s",
                label,
                node_label,
                runtime.consecutive_failures[node_id],
                check.fail_threshold,
                probe.detail,
            )
        elif previous is not probe.status:
            logger.warning(
                "Core %s node %s Warp unknown; not counting as down: %s",
                label,
                node_label,
                probe.detail,
            )

    async def check_core(self, core: CoreInfo) -> None:
        check = self._config.check
        runtime = self._state[core.id]
        runtime.name = core.name
        runtime.observatory = observatory_has_tag(core.config, check.outbound_tag)
        runtime.error = ""
        now = time.time()
        label = f"{core.name}#{core.id}"

        if runtime.cooldown_until > now:
            runtime.state = "cooldown"
            logger.debug(
                "Core %s is in restart cooldown (%ss left); skipping",
                label,
                int(runtime.cooldown_until - now),
            )
            return

        nodes = await self._client.list_nodes(core_id=core.id)
        connected = [node for node in nodes if node.connected]
        listed_ids = {node.id for node in nodes}
        connected_ids = {node.id for node in connected}
        for node_id in list(runtime.consecutive_failures):
            if node_id not in connected_ids:
                runtime.consecutive_failures.pop(node_id, None)
        for mapping in (runtime.last_status, runtime.last_probes, runtime.history):
            for node_id in list(mapping):
                if node_id not in listed_ids:
                    mapping.pop(node_id, None)
        for node in nodes:
            if not node.connected:
                runtime.last_probes[node.id] = ProbeResult(
                    node=node,
                    status=ProbeStatus.SKIP,
                    detail=f"node status is {node.status or 'unknown'}",
                    checked_at=now,
                )
                runtime.last_status.pop(node.id, None)

        if not connected:
            if runtime.state != "no-nodes":
                logger.warning(
                    "Core %s has no connected nodes (%s listed); skipping Warp check",
                    label,
                    len(nodes),
                )
            runtime.state = "no-nodes"
            return

        results = await asyncio.gather(
            *(self.probe_node(node) for node in connected),
            return_exceptions=True,
        )

        probes: list[ProbeResult] = []
        for node, result in zip(connected, results, strict=True):
            if isinstance(result, PanelAuthError):
                raise result
            if isinstance(result, BaseException):
                logger.warning(
                    "Core %s node %s probe error: %s", label, _node_label(node), result
                )
                result = ProbeResult(
                    node=node,
                    status=ProbeStatus.UNKNOWN,
                    detail=f"probe error: {result}",
                    checked_at=now,
                )
            probes.append(result)

        for probe in probes:
            self._record_probe(runtime, label, probe)

        down = [p for p in probes if p.status is ProbeStatus.DOWN]
        up = [p for p in probes if p.status is ProbeStatus.UP]
        if down:
            runtime.state = "down"
        elif len(up) == len(probes):
            runtime.state = "ok"
        else:
            runtime.state = "degraded"

        # Recovered = nothing down any more. An UNKNOWN node (e.g. a slow panel
        # call) must not hold the recovery message back forever.
        if runtime.awaiting_recovery and up and not down:
            runtime.awaiting_recovery = False
            delays = ", ".join(f"{p.node.name} {p.delay_ms}ms" for p in up)
            self._event(core, "recovered", f"Warp up again ({delays})")
            logger.info("Core %s recovered after restart", label)
            await self._notify(
                f"✅ Core <b>{html.escape(core.name)}</b> #{core.id} recovered\n"
                f"{html.escape(delays)}"
            )

        self._prune_restarts(runtime, now)
        if len(runtime.restart_times) < check.max_restarts_per_hour:
            runtime.capped = False

        offenders = [
            probe
            for probe in down
            if runtime.consecutive_failures[probe.node.id] >= check.fail_threshold
        ]
        if not offenders:
            return

        if len(runtime.restart_times) >= check.max_restarts_per_hour:
            if not runtime.capped:
                runtime.capped = True
                message = (
                    f"Warp is down but the hourly restart cap "
                    f"({check.max_restarts_per_hour}) is reached"
                )
                logger.error("Core %s %s", label, message)
                self._event(core, "capped", message)
                await self._notify(
                    f"⛔ Core <b>{html.escape(core.name)}</b> #{core.id}: {message}. "
                    "Needs a look."
                )
            return

        await self._restart(core, runtime, offenders, now=now)

    async def _restart(
        self,
        core: CoreInfo,
        runtime: CoreRuntime,
        offenders: list[ProbeResult],
        *,
        now: float,
    ) -> None:
        check = self._config.check
        label = f"{core.name}#{core.id}"
        names = ", ".join(_node_label(item.node) for item in offenders)
        why = f"Warp failures on: {names}"
        logger.error("Restarting core %s after %s", label, why)
        try:
            await self._client.restart_core(core.id)
        except PanelUnavailable:
            raise
        except PanelError as exc:
            # Back off for one cooldown instead of hammering the restart API
            # every cycle, but don't count it toward the hourly cap.
            runtime.cooldown_until = now + check.restart_cooldown_seconds
            runtime.error = f"restart failed: {exc}"
            logger.error("Core %s restart failed: %s", label, exc)
            self._event(core, "restart-failed", str(exc))
            await self._notify(
                f"❌ Restart of core <b>{html.escape(core.name)}</b> #{core.id} "
                f"failed: {html.escape(str(exc))}"
            )
            return

        runtime.restart_times.append(now)
        runtime.cooldown_until = now + check.restart_cooldown_seconds
        runtime.consecutive_failures.clear()
        runtime.awaiting_recovery = True
        runtime.state = "cooldown"
        self._event(core, "restart", why)
        logger.info(
            "Core %s restart requested; cooldown %ss",
            label,
            check.restart_cooldown_seconds,
        )
        details = "\n".join(
            f"• {html.escape(p.node.name)}: {html.escape(p.detail)}" for p in offenders
        )
        await self._notify(
            f"🔄 Restarted core <b>{html.escape(core.name)}</b> #{core.id}\n"
            f"{details}\n"
            f"Restarts in last hour: {len(runtime.restart_times)}/{check.max_restarts_per_hour}"
        )

    # ------------------------------------------------------------------ loop

    async def run_once(self) -> None:
        cores = await self.discover_warp_cores()
        self._core_order = [core.id for core in cores]
        for core_id in list(self._state):
            if core_id not in self._core_order:
                self._state.pop(core_id, None)
        if not cores:
            logger.warning("No Warp cores found to monitor")
            return
        for core in cores:
            try:
                await self.check_core(core)
            except PanelUnavailable:
                raise
            except PanelAuthError:
                raise
            except PanelError as exc:
                runtime = self._state[core.id]
                runtime.state = "error"
                runtime.error = str(exc)
                logger.error("Core %s (%s) check failed: %s", core.name, core.id, exc)

    async def run_forever(self, stop: asyncio.Event) -> None:
        logger.info(
            "Watching Warp tag %s every %ss (fail threshold %s)%s",
            self._config.check.outbound_tag,
            self._config.check.interval_seconds,
            self._config.check.fail_threshold,
            "; Telegram alerts on" if self._notifier.enabled else "",
        )
        while not stop.is_set():
            started = time.monotonic()
            cycle_error: str | None = None
            try:
                await self.run_once()
            except PanelAuthError as exc:
                cycle_error = f"Panel auth failed: {exc}"
                logger.error("Panel auth failed; skipping this cycle: %s", exc)
            except PanelUnavailable as exc:
                cycle_error = f"Panel unavailable: {exc}"
                logger.error("Panel unavailable; skipping this cycle: %s", exc)
            except Exception as exc:
                cycle_error = f"Unexpected error: {exc}"
                logger.exception("Unexpected error during health check cycle")
            elapsed = time.monotonic() - started
            self.write_status(cycle_error=cycle_error, duration=elapsed)
            self._maybe_log_summary()
            remaining = self._config.check.interval_seconds - elapsed
            if remaining > 0:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=remaining)
                except TimeoutError:
                    pass

    def _maybe_log_summary(self) -> None:
        if time.monotonic() - self._last_summary < SUMMARY_EVERY_SECONDS:
            return
        self._last_summary = time.monotonic()
        parts = []
        for core_id in self._core_order:
            runtime = self._state.get(core_id)
            if runtime is None:
                continue
            ups = [
                p.delay_ms
                for p in runtime.last_probes.values()
                if p.status is ProbeStatus.UP
            ]
            total = sum(
                1 for p in runtime.last_probes.values() if p.status is not ProbeStatus.SKIP
            )
            avg = f" avg {sum(ups) // len(ups)}ms" if ups else ""
            parts.append(f"{runtime.name}#{core_id} {runtime.state} {len(ups)}/{total} up{avg}")
        if parts:
            logger.info("Summary: %s", "; ".join(parts))

    # ------------------------------------------------------------------ status file

    def snapshot(self, *, cycle_error: str | None = None, duration: float = 0.0) -> dict[str, Any]:
        check = self._config.check
        now = time.time()
        cores = []
        for core_id in self._core_order:
            runtime = self._state.get(core_id)
            if runtime is None:
                continue
            self._prune_restarts(runtime, now)
            nodes = []
            for node_id, probe in sorted(runtime.last_probes.items()):
                nodes.append(
                    {
                        "id": node_id,
                        "name": probe.node.name,
                        "node_status": probe.node.status,
                        "probe": probe.status.value,
                        "delay_ms": probe.delay_ms if probe.status is ProbeStatus.UP else None,
                        "detail": probe.detail,
                        "fails": runtime.consecutive_failures.get(node_id, 0),
                        "checked_at": probe.checked_at,
                        "history": list(runtime.history.get(node_id, [])),
                    }
                )
            cores.append(
                {
                    "id": core_id,
                    "name": runtime.name,
                    "state": runtime.state,
                    "error": runtime.error,
                    "observatory": runtime.observatory,
                    "cooldown_until": runtime.cooldown_until,
                    "restarts_last_hour": len(runtime.restart_times),
                    "last_restart_at": runtime.restart_times[-1] if runtime.restart_times else None,
                    "capped": runtime.capped,
                    "nodes": nodes,
                }
            )
        return {
            "version": __version__,
            "pid": os.getpid(),
            "started_at": self._started_at,
            "updated_at": now,
            "tag": check.outbound_tag,
            "interval_seconds": check.interval_seconds,
            "fail_threshold": check.fail_threshold,
            "max_restarts_per_hour": check.max_restarts_per_hour,
            "telegram": self._notifier.enabled,
            "cycle": {"ok": cycle_error is None, "error": cycle_error, "duration": duration},
            "cores": cores,
            "events": list(self._events),
        }

    def write_status(self, *, cycle_error: str | None = None, duration: float = 0.0) -> None:
        path = self._config.status_file
        try:
            write_json_atomic(path, self.snapshot(cycle_error=cycle_error, duration=duration))
            self._status_write_failed = False
        except OSError as exc:
            if not self._status_write_failed:
                logger.warning("Cannot write status file %s: %s", path, exc)
            self._status_write_failed = True

    @staticmethod
    def _prune_restarts(runtime: CoreRuntime, now: float) -> None:
        while runtime.restart_times and now - runtime.restart_times[0] >= HOUR_SECONDS:
            runtime.restart_times.popleft()
