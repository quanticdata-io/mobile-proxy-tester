"""QuanticData mobile plans: carriers by location, and proxy strings.

Documented, free endpoints of https://quanticdata.io/docs/:

- ``GET /public/generator/mobile/targeting-options``: countries, regions,
  cities and the carriers of each city (the ``isp`` values of ``generate``);
- ``POST /public/proxies/generate``: proxy strings with targeting and session
  applied. They are used exactly as returned.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.quanticdata.io/v1"


class QuanticDataError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"QuanticData API: {message} (HTTP {status})")
        self.status = status


@dataclass(frozen=True)
class Carrier:
    country: str
    region: str
    city: str
    code: str
    name: str


class QuanticDataClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        key = (api_key or os.environ.get("QUANTICDATA_API_KEY", "")).strip()
        if not key:
            raise ValueError("QUANTICDATA_API_KEY is not set: create an API key in the QuanticData dashboard")
        base = (base_url or os.environ.get("QUANTICDATA_API_BASE") or DEFAULT_BASE_URL).rstrip("/")
        self._client = httpx.Client(
            base_url=base,
            headers={"Authorization": f"Bearer {key}", "User-Agent": "mobile-proxy-tester"},
            timeout=timeout,
            transport=transport,
        )

    def _call(self, method: str, path: str, json: dict[str, Any] | None = None) -> Any:
        response = self._client.request(method, path, json=json)
        try:
            envelope = response.json()
        except ValueError as exc:
            raise QuanticDataError(response.status_code, "the answer is not JSON") from exc
        if response.status_code >= 300 or envelope.get("type") != "response":
            raise QuanticDataError(response.status_code, str(envelope.get("message") or "unknown error"))
        return envelope.get("payload")

    def carriers(self, country: str | None = None) -> list[Carrier]:
        """Carriers per city from the Mobile targeting tree, optionally for one country."""
        payload = self._call("GET", "/public/generator/mobile/targeting-options") or {}
        tree = payload.get("data") or {}
        out: list[Carrier] = []
        for cc, cnode in tree.items():
            if country and cc.lower() != country.lower():
                continue
            for region, rnode in (cnode.get("regions") or {}).items():
                for city, city_node in (rnode.get("cities") or {}).items():
                    for isp in city_node.get("isps") or []:
                        out.append(Carrier(cc, region, city, str(isp.get("code")), str(isp.get("name"))))
        return out

    def generate(
        self, order_id: str, *, quantity: int = 1, protocol: str = "http", **targeting: Any
    ) -> list[str]:
        """Proxy strings for one plan. ``targeting`` takes the documented field
        names: country, state, city, isp, rotation, sessionTime, superSticky, gateway."""
        body: dict[str, Any] = {
            "orderId": order_id,
            "quantity": quantity,
            "protocol": protocol,
            "format": f"{protocol}://user:pass@host:port",
        }
        body.update({k: v for k, v in targeting.items() if v not in (None, "", False)})
        payload = self._call("POST", "/public/proxies/generate", json=body) or {}
        return [str(line) for line in payload.get("proxies") or []]

    def close(self) -> None:
        self._client.close()
