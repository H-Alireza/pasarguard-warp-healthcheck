from __future__ import annotations

import time

from warp_healthcheck.config import load_config
from warp_healthcheck.menu import Style, render_status, sparkline


def test_sparkline():
    assert sparkline([]) == ""
    assert sparkline([100, None, 300]) == "▁x█"
    assert sparkline([50, 50]) == "▅▅"


def test_render_status_plain():
    now = time.time()
    status = {
        "version": "0.0.0",
        "updated_at": now - 3,
        "interval_seconds": 10,
        "tag": "warp",
        "fail_threshold": 3,
        "max_restarts_per_hour": 6,
        "telegram": False,
        "cycle": {"ok": True},
        "cores": [
            {
                "id": 1,
                "name": "germany",
                "state": "down",
                "observatory": True,
                "cooldown_until": 0,
                "restarts_last_hour": 1,
                "last_restart_at": now - 120,
                "nodes": [
                    {"id": 1, "name": "de-1", "probe": "up", "delay_ms": 140, "fails": 0,
                     "detail": "alive", "history": [130, 140]},
                    {"id": 2, "name": "de-2", "probe": "down", "delay_ms": None, "fails": 2,
                     "detail": "Observatory reports Warp dead", "history": [120, None]},
                ],
            }
        ],
        "events": [{"time": now - 120, "kind": "restart", "core": "germany", "message": "Warp failures on: de-2#2"}],
    }
    text = render_status(status, service="active", style=Style(False), now=now, width=120)
    assert "Core 1 germany  [DOWN]" in text
    assert "de-1" in text and "140ms" in text
    assert "Warp dead" in text
    assert "restarts 1/h" in text
    assert "Recent events" in text
    assert "Service is running v0.0.0" in text


def test_render_status_missing():
    text = render_status(None, service="inactive", style=Style(False))
    assert "No status yet" in text
    assert "systemctl start" in text


def test_load_config_telegram(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "panel:\n  base_url: https://p/\n  username: a\n  password: b\n"
        "telegram:\n  bot_token: '123:abc'\n  chat_id: -100\n"
        f"status_file: {tmp_path / 's.json'}\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.panel.base_url == "https://p"
    assert config.telegram.enabled
    assert config.telegram.chat_id == "-100"
    assert config.status_file == tmp_path / "s.json"
