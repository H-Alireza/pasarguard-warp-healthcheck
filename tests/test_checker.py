from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from warp_healthcheck.checker import HealthChecker, ProbeStatus, classify_latency
from warp_healthcheck.config import AppConfig, CheckConfig, PanelConfig
from warp_healthcheck.models import (
    CoreInfo,
    NodeInfo,
    OutboundLatency,
    PanelAPIError,
    PanelUnavailable,
)

WARP_CORE_CONFIG = {
    "outbounds": [{"protocol": "wireguard", "tag": "warp"}],
    "observatory": {"subjectSelector": ["warp"], "probeInterval": "10s"},
}


def _latency(alive: bool, delay: int = 120, age: float = 1.0) -> OutboundLatency:
    ts = int(time.time() - age)
    return OutboundLatency(
        name="warp", alive=alive, delay=delay, last_seen_time=ts, last_try_time=ts
    )


class FakeClient:
    def __init__(self, nodes: list[NodeInfo]) -> None:
        self.nodes = nodes
        # node_id -> list[OutboundLatency] or an exception to raise
        self.latency: dict[int, object] = {}
        self.restarts: list[int] = []
        self.restart_error: Exception | None = None

    async def list_nodes(self, core_id: int | None = None) -> list[NodeInfo]:
        return self.nodes

    async def get_outbounds_latency(self, node_id: int, name: str = "", timeout=None):
        value = self.latency[node_id]
        if isinstance(value, Exception):
            raise value
        return value

    async def restart_core(self, core_id: int) -> None:
        if self.restart_error:
            raise self.restart_error
        self.restarts.append(core_id)


class FakeNotifier:
    enabled = True

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> bool:
        self.messages.append(text)
        return True


def _config(tmp_path: Path, **check) -> AppConfig:
    return AppConfig(
        panel=PanelConfig(base_url="https://panel", username="u", password="p"),
        check=CheckConfig(**{"fail_threshold": 2, "max_restarts_per_hour": 2, **check}),
        status_file=tmp_path / "status.json",
    )


def _setup(tmp_path: Path, n_nodes: int = 2, **check):
    nodes = [NodeInfo(id=i, name=f"n{i}", status="connected", address="") for i in range(1, n_nodes + 1)]
    client = FakeClient(nodes)
    notifier = FakeNotifier()
    checker = HealthChecker(_config(tmp_path, **check), client, notifier)  # type: ignore[arg-type]
    core = CoreInfo(id=7, name="de", config=WARP_CORE_CONFIG)
    checker._core_order = [core.id]
    return checker, client, notifier, core


# ---------------------------------------------------------------- classify


def test_classify_up_down_stale():
    now = time.time()
    assert classify_latency([_latency(True)], "warp", now=now, stale_after_seconds=20)[0] is ProbeStatus.UP
    assert classify_latency([_latency(False)], "warp", now=now, stale_after_seconds=20)[0] is ProbeStatus.DOWN
    stale = classify_latency([_latency(False, age=60)], "warp", now=now, stale_after_seconds=20)
    assert stale[0] is ProbeStatus.UNKNOWN


def test_missing_tag_is_unknown_not_down():
    # Regression: an empty result used to count as DOWN and restart the core.
    status, detail, _ = classify_latency([], "warp", now=time.time(), stale_after_seconds=20)
    assert status is ProbeStatus.UNKNOWN
    other = OutboundLatency(name="direct", alive=True)
    status, detail, _ = classify_latency([other], "warp", now=time.time(), stale_after_seconds=20)
    assert status is ProbeStatus.UNKNOWN
    assert "direct" in detail


# ---------------------------------------------------------------- check_core


def test_restart_after_threshold_then_recovery(tmp_path):
    checker, client, notifier, core = _setup(tmp_path, restart_cooldown_seconds=0)
    client.latency = {1: [_latency(True)], 2: [_latency(False)]}

    asyncio.run(checker.check_core(core))
    assert client.restarts == []
    asyncio.run(checker.check_core(core))
    assert client.restarts == [7]
    assert "Restarted core" in notifier.messages[-1]

    client.latency = {1: [_latency(True, 90)], 2: [_latency(True, 110)]}
    asyncio.run(checker.check_core(core))
    assert "recovered" in notifier.messages[-1]
    assert checker._state[7].state == "ok"


def test_recovery_not_blocked_by_unknown_node(tmp_path):
    checker, client, notifier, core = _setup(tmp_path, restart_cooldown_seconds=0, fail_threshold=1)
    client.latency = {1: PanelUnavailable("ReadTimeout"), 2: [_latency(False)]}
    asyncio.run(checker.check_core(core))
    assert client.restarts == [7]
    client.latency = {1: PanelUnavailable("ReadTimeout"), 2: [_latency(True)]}
    asyncio.run(checker.check_core(core))
    assert "recovered" in notifier.messages[-1]


def test_node_timeout_does_not_abort_or_count(tmp_path):
    # Regression: a timeout on one node used to raise PanelUnavailable and
    # skip the whole cycle for every core.
    checker, client, notifier, core = _setup(tmp_path)
    client.latency = {1: PanelUnavailable("read timeout"), 2: [_latency(True)]}
    for _ in range(5):
        asyncio.run(checker.check_core(core))
    runtime = checker._state[7]
    assert client.restarts == []
    assert runtime.last_status[1] is ProbeStatus.UNKNOWN
    assert runtime.last_status[2] is ProbeStatus.UP
    assert runtime.consecutive_failures[1] == 0


def test_hourly_cap_notifies_once(tmp_path):
    checker, client, notifier, core = _setup(
        tmp_path, n_nodes=1, restart_cooldown_seconds=0, max_restarts_per_hour=1, fail_threshold=1
    )
    client.latency = {1: [_latency(False)]}
    for _ in range(6):
        asyncio.run(checker.check_core(core))
    assert client.restarts == [7]
    capped = [m for m in notifier.messages if "cap" in m]
    assert len(capped) == 1
    assert checker._state[7].capped


def test_restart_failure_backs_off(tmp_path):
    checker, client, notifier, core = _setup(
        tmp_path, n_nodes=1, fail_threshold=1, restart_cooldown_seconds=90
    )
    client.latency = {1: [_latency(False)]}
    client.restart_error = PanelAPIError("boom", 500)
    asyncio.run(checker.check_core(core))
    asyncio.run(checker.check_core(core))  # in cooldown: no second attempt
    runtime = checker._state[7]
    assert runtime.cooldown_until > time.time()
    assert len(runtime.restart_times) == 0
    assert sum("failed" in m for m in notifier.messages) == 1


# ---------------------------------------------------------------- status file


def test_status_file(tmp_path):
    checker, client, notifier, core = _setup(tmp_path)
    client.latency = {1: [_latency(True, 150)], 2: [_latency(False)]}
    asyncio.run(checker.check_core(core))
    checker.write_status()
    data = json.loads((tmp_path / "status.json").read_text())
    assert data["cores"][0]["id"] == 7
    nodes = {n["id"]: n for n in data["cores"][0]["nodes"]}
    assert nodes[1]["probe"] == "up" and nodes[1]["delay_ms"] == 150
    assert nodes[2]["probe"] == "down" and nodes[2]["fails"] == 1
    assert nodes[2]["history"] == [None]
