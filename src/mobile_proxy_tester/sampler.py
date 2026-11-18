"""Send requests through a proxy and record which exit IP each one used."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from .lookup import DEFAULT_ECHO_URL, Exit, Network, NetworkLookup, NoLookup, parse_echo
from .proxy import Proxy


@dataclass(frozen=True)
class Sample:
    """One request through the proxy."""

    index: int
    at: datetime  # UTC wall clock, for the report
    elapsed: float  # seconds since the run started (monotonic)
    ok: bool
    latency_ms: float | None
    exit: Exit | None
    network: Network | None
    error: str | None = None
    event: str | None = None  # "rotate" when a rotation was triggered just before this sample

    @property
    def ip(self) -> str | None:
        return self.exit.ip if self.exit else None


class Sampler:
    """Requests the IP echo through ``proxy`` and looks up each new exit IP.

    With ``keepalive=False`` (default) every sample opens a new connection to
    the proxy, which is what rotating gateways key their rotation on. With
    ``keepalive=True`` samples reuse one connection, to see whether the exit
    changes inside a connection.
    """

    def __init__(
        self,
        proxy: Proxy,
        *,
        echo_url: str = DEFAULT_ECHO_URL,
        lookup: NetworkLookup | None = None,
        timeout: float = 20.0,
        keepalive: bool = False,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.proxy = proxy
        self.echo_url = echo_url
        self.lookup = lookup or NoLookup()
        self.timeout = timeout
        self.keepalive = keepalive
        self._transport = transport
        self._clock = clock
        self._now = now
        self._started = clock()
        self._index = 0
        self._shared: httpx.Client | None = None

    def _client(self) -> httpx.Client:
        if self._transport is not None:
            return httpx.Client(transport=self._transport, timeout=self.timeout)
        limits = httpx.Limits(max_keepalive_connections=1 if self.keepalive else 0)
        return httpx.Client(proxy=self.proxy.url, timeout=self.timeout, limits=limits)

    def sample(self, event: str | None = None) -> Sample:
        self._index += 1
        at = self._now()
        elapsed = self._clock() - self._started
        started = time.perf_counter()
        try:
            if self.keepalive:
                if self._shared is None:
                    self._shared = self._client()
                response = self._shared.get(self.echo_url, headers={"Cache-Control": "no-cache"})
            else:
                with self._client() as client:
                    response = client.get(self.echo_url, headers={"Cache-Control": "no-cache"})
            latency = (time.perf_counter() - started) * 1000
            if response.status_code != 200:
                return Sample(
                    self._index,
                    at,
                    elapsed,
                    False,
                    latency,
                    None,
                    None,
                    f"HTTP {response.status_code}",
                    event,
                )
            exit_ = parse_echo(response.content)
        except (httpx.HTTPError, ValueError) as exc:
            return Sample(self._index, at, elapsed, False, None, None, None, _describe(exc), event)
        return Sample(
            self._index, at, elapsed, True, latency, exit_, self.lookup.lookup(exit_.ip), None, event
        )

    def close(self) -> None:
        if self._shared is not None:
            self._shared.close()
            self._shared = None


def _describe(exc: BaseException) -> str:
    name = type(exc).__name__
    text = str(exc).strip()
    return f"{name}: {text}" if text else name


def watch(
    sampler: Sampler,
    *,
    interval: float,
    duration: float | None = None,
    count: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[Sample]:
    """Sample every ``interval`` seconds until ``duration`` seconds or ``count`` samples."""
    if duration is None and count is None:
        raise ValueError("give a duration or a count")
    start = clock()
    taken = 0
    while True:
        tick = clock()
        yield sampler.sample()
        taken += 1
        if count is not None and taken >= count:
            return
        next_at = tick + interval
        if duration is not None and next_at - start > duration:
            return
        wait = next_at - clock()
        if wait > 0:
            sleep(wait)


class RotationTrigger:
    """Calls a "change IP" URL (GET by default), as many mobile and residential
    providers offer, or the QuanticData rotation link of a sticky session."""

    def __init__(
        self,
        url: str,
        method: str = "GET",
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.url = url
        self.method = method.upper()
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def __call__(self) -> tuple[bool, str]:
        try:
            response = self._client.request(self.method, self.url)
        except httpx.HTTPError as exc:
            return False, _describe(exc)
        return 200 <= response.status_code < 300, f"HTTP {response.status_code}"

    def close(self) -> None:
        self._client.close()


@dataclass(frozen=True)
class RotationResult:
    attempt: int
    triggered: bool
    trigger_status: str
    before_ip: str | None
    after_ip: str | None
    changed: bool
    seconds_to_new_ip: float | None  # from the trigger until the echo showed a new IP
    samples: list[Sample]


def rotate_and_wait(
    sampler: Sampler,
    trigger: Callable[[], tuple[bool, str]],
    *,
    attempts: int,
    poll: float = 2.0,
    max_wait: float = 60.0,
    cooldown: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[RotationResult]:
    """Trigger a rotation ``attempts`` times; after each, poll until the exit IP changes.

    ``cooldown`` waits between attempts, for providers that limit how often an
    IP can be changed (QuanticData rotation links: once every 10 seconds per session).
    """
    before = sampler.sample()
    current = before.ip
    for attempt in range(1, attempts + 1):
        if attempt > 1 and cooldown > 0:
            sleep(cooldown)
        ok, status = trigger()
        triggered_at = clock()
        samples: list[Sample] = []
        after: str | None = None
        changed = False
        waited: float | None = None
        if ok:
            while True:
                sample = sampler.sample(event="rotate" if not samples else None)
                samples.append(sample)
                if sample.ip and sample.ip != current:
                    after, changed, waited = sample.ip, True, clock() - triggered_at
                    break
                if sample.ip:
                    after = sample.ip
                if clock() - triggered_at + poll > max_wait:
                    break
                sleep(poll)
        yield RotationResult(attempt, ok, status, current, after, changed, waited, samples)
        if after:
            current = after
