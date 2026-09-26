from __future__ import annotations

from warp_healthcheck.observatory import (
    ensure_observatory,
    has_misspelled_probe_url,
    observatory_has_tag,
    observatory_interval_seconds,
    parse_duration,
)


def test_parse_duration():
    assert parse_duration("10s") == 10
    assert parse_duration("1m30s") == 90
    assert parse_duration("500ms") == 0.5
    assert parse_duration("2h") == 7200
    assert parse_duration(15) == 15
    assert parse_duration("soon") is None
    assert parse_duration("10x") is None
    assert parse_duration("") is None


def test_adds_observatory_when_missing():
    config, changed = ensure_observatory({}, "warp", probe_interval="10s")
    assert changed
    assert config["observatory"]["subjectSelector"] == ["warp"]
    assert config["observatory"]["probeURL"] == "https://www.cloudflare.com/cdn-cgi/trace"
    assert "probeUrl" not in config["observatory"]
    assert observatory_has_tag(config, "warp")


def test_appends_tag_to_existing_observatory():
    original = {"observatory": {"subjectSelector": ["proxy"], "probeURL": "x", "probeInterval": "1m"}}
    config, changed = ensure_observatory(original, "warp")
    assert changed
    assert config["observatory"]["subjectSelector"] == ["proxy", "warp"]
    assert config["observatory"]["probeInterval"] == "1m"
    assert original["observatory"]["subjectSelector"] == ["proxy"]  # not mutated


def test_uses_burst_observatory_if_present():
    config, changed = ensure_observatory({"burstObservatory": {"subjectSelector": []}}, "warp")
    assert changed
    assert "observatory" not in config
    assert config["burstObservatory"]["subjectSelector"] == ["warp"]
    _, changed_again = ensure_observatory(config, "warp")
    assert not changed_again


def test_observatory_interval_seconds():
    assert observatory_interval_seconds(
        {"observatory": {"subjectSelector": ["warp"], "probeInterval": "1m"}}, "warp"
    ) == 60
    assert observatory_interval_seconds(
        {"burstObservatory": {"subjectSelector": ["warp"], "pingConfig": {"interval": "30s"}}},
        "warp",
    ) == 30
    assert observatory_interval_seconds({"observatory": {"subjectSelector": ["x"]}}, "warp") is None


def test_renames_misspelled_probe_url():
    # Xray reads `probeURL`; `probeUrl` (written by older versions) is ignored.
    original = {"observatory": {"subjectSelector": ["warp"], "probeUrl": "https://x/y", "probeInterval": "10s"}}
    assert has_misspelled_probe_url(original)
    config, changed = ensure_observatory(original, "warp")
    assert changed
    assert config["observatory"]["probeURL"] == "https://x/y"
    assert "probeUrl" not in config["observatory"]
    assert not has_misspelled_probe_url(config)
    _, changed_again = ensure_observatory(config, "warp")
    assert not changed_again


def test_misspelled_key_dropped_when_correct_key_exists():
    original = {"observatory": {"subjectSelector": ["warp"], "probeURL": "https://keep", "probeUrl": "https://old"}}
    config, changed = ensure_observatory(original, "warp")
    assert changed
    assert config["observatory"]["probeURL"] == "https://keep"
    assert "probeUrl" not in config["observatory"]
