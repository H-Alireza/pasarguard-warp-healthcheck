from __future__ import annotations

import copy
import re
from typing import Any


DEFAULT_PROBE_URL = "https://www.cloudflare.com/cdn-cgi/trace"
MISSPELLED_PROBE_URL = "probeUrl"

_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
_DURATION_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def parse_duration(value: Any) -> float | None:
    """Parse a Go-style duration ("10s", "1m30s", "500ms") into seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    parts = _DURATION_PART.findall(text)
    if not parts or "".join(num + unit for num, unit in parts) != text:
        return None
    return sum(float(num) * _DURATION_UNITS[unit] for num, unit in parts)


def observatory_interval_seconds(config: dict[str, Any], tag: str) -> float | None:
    """Probe interval of the observer that covers `tag`, if it is set."""
    observatory = config.get("observatory")
    if isinstance(observatory, dict) and tag in (observatory.get("subjectSelector") or []):
        return parse_duration(observatory.get("probeInterval"))
    burst = config.get("burstObservatory")
    if isinstance(burst, dict) and tag in (burst.get("subjectSelector") or []):
        ping = burst.get("pingConfig")
        if isinstance(ping, dict):
            return parse_duration(ping.get("interval"))
    return None


def observatory_has_tag(config: dict[str, Any], tag: str) -> bool:
    for key in ("observatory", "burstObservatory"):
        block = config.get(key)
        if not isinstance(block, dict):
            continue
        selectors = block.get("subjectSelector") or []
        if isinstance(selectors, list) and tag in selectors:
            return True
    return False


def has_misspelled_probe_url(config: dict[str, Any]) -> bool:
    """True if the observatory block uses `probeUrl`, which Xray ignores."""
    observatory = config.get("observatory")
    return isinstance(observatory, dict) and MISSPELLED_PROBE_URL in observatory


def ensure_observatory(
    config: dict[str, Any],
    tag: str,
    *,
    probe_url: str = DEFAULT_PROBE_URL,
    probe_interval: str = "10s",
) -> tuple[dict[str, Any], bool]:
    """Return (new_config, changed) with Observatory covering `tag`."""
    updated = copy.deepcopy(config)
    burst = updated.get("burstObservatory")
    observatory = updated.get("observatory")

    if isinstance(burst, dict) and not isinstance(observatory, dict):
        selectors = list(burst.get("subjectSelector") or [])
        if tag in selectors:
            return updated, False
        selectors.append(tag)
        burst["subjectSelector"] = selectors
        ping = burst.get("pingConfig")
        if not isinstance(ping, dict):
            burst["pingConfig"] = {
                "destination": probe_url,
                "interval": probe_interval,
                "timeout": "5s",
                "sampling": 3,
            }
        return updated, True

    if isinstance(observatory, dict):
        selectors = list(observatory.get("subjectSelector") or [])
        changed = False
        if tag not in selectors:
            selectors.append(tag)
            observatory["subjectSelector"] = selectors
            changed = True
        # Xray reads `probeURL` (case-sensitive). Older versions of this tool
        # wrote `probeUrl`, which Xray ignores; move it to the right key.
        if MISSPELLED_PROBE_URL in observatory:
            misspelled = observatory.pop(MISSPELLED_PROBE_URL)
            if not observatory.get("probeURL") and misspelled:
                observatory["probeURL"] = misspelled
            changed = True
        if not observatory.get("probeURL"):
            observatory["probeURL"] = probe_url
            changed = True
        if not observatory.get("probeInterval"):
            observatory["probeInterval"] = probe_interval
            changed = True
        return updated, changed

    updated["observatory"] = {
        "subjectSelector": [tag],
        "probeURL": probe_url,
        "probeInterval": probe_interval,
        "enableConcurrency": True,
    }
    return updated, True
