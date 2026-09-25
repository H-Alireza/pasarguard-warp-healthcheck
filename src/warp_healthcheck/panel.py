from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from warp_healthcheck.models import (
    CoreInfo,
    NodeInfo,
    OutboundLatency,
    PanelAPIError,
    PanelAuthError,
    PanelUnavailable,
)


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, set, tuple)):
        return [str(item) for item in value]
    return [str(value)]


def _core_from_payload(payload: dict[str, Any]) -> CoreInfo:
    config = payload.get("config")
    if not isinstance(config, dict):
        config = {}
    core_type = payload.get("type")
    return CoreInfo(
        id=int(payload["id"]),
        name=str(payload.get("name") or f"core-{payload['id']}"),
        config=config,
        core_type=str(core_type) if core_type else None,
        exclude_inbound_tags=_as_str_list(payload.get("exclude_inbound_tags")),
        fallbacks_inbound_tags=_as_str_list(payload.get("fallbacks_inbound_tags")),
    )


def _node_from_payload(payload: dict[str, Any]) -> NodeInfo:
    core_id = payload.get("core_config_id")
    return NodeInfo(
        id=int(payload["id"]),
        name=str(payload.get("name") or f"node-{payload['id']}"),
        status=str(payload.get("status") or ""),
        address=str(payload.get("address") or ""),
        core_config_id=int(core_id) if core_id is not None else None,
    )


def _latency_from_payload(payload: dict[str, Any]) -> OutboundLatency:
    return OutboundLatency(
        name=str(payload.get("name") or payload.get("link") or ""),
        alive=bool(payload.get("alive")),
        delay=int(payload.get("delay") or 0),
        link=str(payload.get("link") or ""),
        last_seen_time=int(payload.get("last_seen_time") or 0),
        last_try_time=int(payload.get("last_try_time") or 0),
        source=str(payload.get("source") or ""),
    )


def _error_detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        text = response.text.strip()
        return text or f"HTTP {response.status_code}"
    if isinstance(data, dict):
        detail = data.get("detail") or data.get("message") or data
        return str(detail)
    return str(data)


def _request_error(exc: httpx.RequestError) -> str:
    # httpx timeouts often have an empty message; the class name is the useful part.
    text = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {text}" if text else name


class PanelClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout: float = 8.0,
        verify_tls: bool = True,
    ) -> None:
        self._username = username
        self._password = password
        self._token: str | None = None
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            verify=verify_tls,
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> PanelClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def login(self) -> None:
        # quote() encodes '+' as %2B so it is not treated as a space in form bodies.
        body = urlencode(
            {
                "grant_type": "password",
                "username": self._username,
                "password": self._password,
            },
            quote_via=quote,
        )
        try:
            response = await self._client.post(
                "/api/admin/token",
                content=body.encode("utf-8"),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.RequestError as exc:
            raise PanelUnavailable(f"Cannot reach panel: {_request_error(exc)}") from exc
        if response.status_code in {401, 403}:
            raise PanelAuthError(
                f"{_error_detail(response)}. "
                "Edit /etc/warp-healthcheck/config.yaml (keep the password in double quotes) "
                "or run: sudo bash install.sh reconfigure"
            )
        if response.status_code >= 500:
            raise PanelUnavailable(_error_detail(response))
        if response.status_code >= 400:
            raise PanelAPIError(_error_detail(response), response.status_code)
        try:
            token = response.json().get("access_token")
        except ValueError as exc:
            raise PanelAPIError("Token response was not JSON") from exc
        if not token:
            raise PanelAPIError("Token response missing access_token")
        self._token = str(token)

    async def _headers(self) -> dict[str, str]:
        if not self._token:
            await self.login()
        return {"Authorization": f"Bearer {self._token}"}

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        retry_auth: bool = True,
        unavailable_on_5xx: bool = True,
    ) -> httpx.Response:
        try:
            response = await self._client.request(
                method,
                path,
                params=params,
                json=json,
                headers=await self._headers(),
            )
        except httpx.RequestError as exc:
            raise PanelUnavailable(f"Cannot reach panel: {_request_error(exc)}") from exc

        if response.status_code == 401 and retry_auth:
            await self.login()
            return await self.request(
                method,
                path,
                params=params,
                json=json,
                retry_auth=False,
                unavailable_on_5xx=unavailable_on_5xx,
            )
        if response.status_code in {401, 403}:
            raise PanelAuthError(_error_detail(response))
        if response.status_code >= 500 and unavailable_on_5xx:
            raise PanelUnavailable(_error_detail(response))
        return response

    async def get_current_admin(self) -> dict[str, Any]:
        response = await self.request("GET", "/api/admin")
        if response.status_code >= 400:
            raise PanelAPIError(_error_detail(response), response.status_code)
        data = response.json()
        if not isinstance(data, dict):
            raise PanelAPIError("Unexpected admin response")
        return data

    async def list_cores(self) -> list[CoreInfo]:
        cores: list[CoreInfo] = []
        offset = 0
        limit = 100
        while True:
            response = await self.request(
                "GET", "/api/cores", params={"offset": offset, "limit": limit}
            )
            if response.status_code >= 400:
                raise PanelAPIError(_error_detail(response), response.status_code)
            payload = response.json()
            items = payload.get("cores") if isinstance(payload, dict) else payload
            if not isinstance(items, list):
                raise PanelAPIError("Unexpected cores response")
            for item in items:
                if isinstance(item, dict):
                    cores.append(_core_from_payload(item))
            total = int(payload.get("count") or payload.get("total") or 0) if isinstance(payload, dict) else 0
            offset += len(items)
            if not items or (total and offset >= total) or len(items) < limit:
                break
        return cores

    async def get_core(self, core_id: int) -> CoreInfo:
        response = await self.request("GET", f"/api/core/{core_id}")
        if response.status_code == 404:
            raise PanelAPIError(f"Core {core_id} not found", 404)
        if response.status_code >= 400:
            raise PanelAPIError(_error_detail(response), response.status_code)
        payload = response.json()
        if not isinstance(payload, dict):
            raise PanelAPIError("Unexpected core response")
        return _core_from_payload(payload)

    async def update_core(
        self,
        core: CoreInfo,
        config: dict[str, Any],
        *,
        restart_nodes: bool = True,
    ) -> CoreInfo:
        payload: dict[str, Any] = {
            "name": core.name,
            "config": config,
            "exclude_inbound_tags": core.exclude_inbound_tags,
            "fallbacks_inbound_tags": core.fallbacks_inbound_tags,
        }
        if core.core_type:
            payload["type"] = core.core_type
        response = await self.request(
            "PUT",
            f"/api/core/{core.id}",
            params={"restart_nodes": str(restart_nodes).lower()},
            json=payload,
        )
        if response.status_code >= 400:
            raise PanelAPIError(_error_detail(response), response.status_code)
        data = response.json()
        if isinstance(data, dict) and "id" in data:
            return _core_from_payload(data)
        return core

    async def restart_core(self, core_id: int) -> None:
        response = await self.request("POST", f"/api/core/{core_id}/restart")
        if response.status_code >= 400:
            raise PanelAPIError(_error_detail(response), response.status_code)

    async def list_nodes(self, core_id: int | None = None) -> list[NodeInfo]:
        nodes: list[NodeInfo] = []
        offset = 0
        limit = 100
        while True:
            params: dict[str, Any] = {"offset": offset, "limit": limit}
            if core_id is not None:
                params["core_id"] = core_id
            response = await self.request("GET", "/api/nodes", params=params)
            if response.status_code >= 400:
                raise PanelAPIError(_error_detail(response), response.status_code)
            payload = response.json()
            items = payload.get("nodes") if isinstance(payload, dict) else payload
            if not isinstance(items, list):
                raise PanelAPIError("Unexpected nodes response")
            for item in items:
                if isinstance(item, dict):
                    nodes.append(_node_from_payload(item))
            total = int(payload.get("total") or payload.get("count") or 0) if isinstance(payload, dict) else 0
            offset += len(items)
            if not items or (total and offset >= total) or len(items) < limit:
                break
        return nodes

    async def get_outbounds_latency(
        self, node_id: int, name: str = "", timeout: int | None = None
    ) -> list[OutboundLatency]:
        params: dict[str, Any] = {}
        if name:
            params["name"] = name
        if timeout is not None:
            params["timeout"] = timeout
        response = await self.request(
            "GET",
            f"/api/node/{node_id}/outbounds_latency",
            params=params,
            unavailable_on_5xx=False,
        )
        if response.status_code >= 400:
            raise PanelAPIError(_error_detail(response), response.status_code)
        payload = response.json()
        items: Iterable[Any]
        if isinstance(payload, dict):
            items = payload.get("latencies") or []
        elif isinstance(payload, list):
            items = payload
        else:
            raise PanelAPIError("Unexpected latency response")
        latencies: list[OutboundLatency] = []
        for item in items:
            if isinstance(item, dict):
                latencies.append(_latency_from_payload(item))
        return latencies
