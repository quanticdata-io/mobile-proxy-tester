"""A fake mobile proxy on 127.0.0.1.

One server plays three roles, so tests need no network:

- as a **proxy**: absolute-form ``GET http://echo.test/...`` requests are
  answered with the current exit IP in the QuanticData echo format;
- as a **rotation URL**: a direct ``GET /rotate`` moves to the next exit IP;
- as a **network lookup**: a direct ``GET /lookup/<ip>`` answers like
  ipinfo.io (``org: "AS<n> <carrier>"``).
"""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

ECHO_URL = "http://echo.test/ip"

CARRIERS = {
    "203.0.113.10": ("AS21928", "T-Mobile USA, Inc.", "US", "Cincinnati"),
    "203.0.113.11": ("AS21928", "T-Mobile USA, Inc.", "US", "Cincinnati"),
    "198.51.100.20": ("AS6167", "Verizon Business", "US", "Columbus"),
}


@dataclass
class FakeMobile:
    ips: list[str]
    per_request: bool = False  # rotate on every proxied request (a rotating string)
    credentials: tuple[str, str] | None = None
    rotate_status: int = 200
    rotate_sticks: bool = False  # /rotate answers 200 but the IP stays
    index: int = 0
    echo_requests: int = 0
    lookups: list[str] = field(default_factory=list)
    port: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def proxy_url(self) -> str:
        if self.credentials:
            return f"http://{self.credentials[0]}:{self.credentials[1]}@127.0.0.1:{self.port}"
        return self.base

    def current(self) -> str:
        return self.ips[self.index % len(self.ips)]


def _handler(fake: FakeMobile) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _json(self, status: int, data: Any) -> None:
            raw = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            if self.path.startswith("http://"):
                if fake.credentials:
                    want = "Basic " + base64.b64encode(":".join(fake.credentials).encode()).decode()
                    if self.headers.get("Proxy-Authorization") != want:
                        self._json(407, {"error": "auth"})
                        return
                with fake.lock:
                    fake.echo_requests += 1
                    ip = fake.current()
                    if fake.per_request:
                        fake.index += 1
                _, _, country, city = CARRIERS.get(ip, ("", "", "DE", "Berlin"))
                self._json(
                    200, {"type": "response", "payload": {"ip": ip, "countryCode": country, "city": city}}
                )
            elif self.path == "/rotate":
                if fake.rotate_status == 200 and not fake.rotate_sticks:
                    with fake.lock:
                        fake.index += 1
                self._json(fake.rotate_status, {"rotated": fake.rotate_status == 200})
            elif self.path.startswith("/lookup/"):
                ip = self.path.rsplit("/", 1)[1]
                fake.lookups.append(ip)
                asn, name, country, city = CARRIERS.get(ip, ("AS3320", "Deutsche Telekom AG", "DE", "Berlin"))
                self._json(200, {"ip": ip, "org": f"{asn} {name}", "country": country, "city": city})
            elif self.path.startswith("/v1/public/proxies/ip-info"):
                ip = self.path.split("ip=", 1)[1]
                asn, name, country, city = CARRIERS.get(ip, ("AS3320", "Deutsche Telekom AG", "DE", "Berlin"))
                if self.headers.get("Authorization") != "Bearer qd_test_key":
                    self._json(401, {"type": "error", "message": "bad key"})
                    return
                self._json(
                    200,
                    {
                        "type": "response",
                        "payload": {
                            "ip": ip,
                            "countryCode": country,
                            "city": city,
                            "isp": name,
                            "as": f"{asn} {name}",
                        },
                    },
                )
            else:
                self._json(404, {"error": "not found"})

    return Handler


@pytest.fixture
def make_mobile() -> Iterator[Callable[..., FakeMobile]]:
    servers: list[ThreadingHTTPServer] = []

    def make(ips: list[str], **kwargs: Any) -> FakeMobile:
        fake = FakeMobile(ips=ips, **kwargs)
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(fake))
        fake.port = int(server.server_address[1])
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return fake

    yield make
    for server in servers:
        server.shutdown()
        server.server_close()


class FakeTime:
    """Monotonic clock and sleep that only move when told to."""

    def __init__(self) -> None:
        self.now = 0.0
        self.on_sleep: Callable[[float], None] | None = None

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.on_sleep:
            self.on_sleep(self.now)


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()
