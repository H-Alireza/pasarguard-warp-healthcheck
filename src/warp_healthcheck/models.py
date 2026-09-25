from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class PanelError(Exception):
    """Base panel API error."""


class PanelAuthError(PanelError):
    """Authentication failed."""


class PanelUnavailable(PanelError):
    """Panel could not be reached or returned a server error."""


class PanelAPIError(PanelError):
    """Unexpected API response."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class CoreInfo:
    id: int
    name: str
    config: dict[str, Any]
    core_type: str | None = None
    exclude_inbound_tags: list[str] = field(default_factory=list)
    fallbacks_inbound_tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class NodeInfo:
    id: int
    name: str
    status: str
    address: str
    core_config_id: int | None = None

    @property
    def connected(self) -> bool:
        return self.status.strip().lower() == "connected"


@dataclass(frozen=True)
class OutboundLatency:
    name: str
    alive: bool
    delay: int = 0
    link: str = ""
    last_seen_time: int = 0
    last_try_time: int = 0
    source: str = ""


def find_warp_outbound(config: dict[str, Any], tag: str) -> dict[str, Any] | None:
    outbounds = config.get("outbounds")
    if not isinstance(outbounds, list):
        return None
    for outbound in outbounds:
        if not isinstance(outbound, dict):
            continue
        protocol = str(outbound.get("protocol") or "").lower()
        if outbound.get("tag") == tag and protocol == "wireguard":
            return outbound
    return None


def core_has_warp(core: CoreInfo, tag: str) -> bool:
    return find_warp_outbound(core.config, tag) is not None
