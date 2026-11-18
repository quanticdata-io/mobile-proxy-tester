"""Turn samples into answers: how long did each IP hold, how often did the
carrier change, how quickly did a rotation give a new IP."""

from __future__ import annotations

import ipaddress
import itertools
import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .sampler import RotationResult, Sample


@dataclass(frozen=True)
class Segment:
    """A run of consecutive successful samples with the same exit IP.

    ``held_at_least`` is the time between the first and last sample of the run.
    ``held_at_most`` adds the gap to the next sample with another IP; it is
    ``None`` for the last run, which had not ended when the test stopped.
    """

    ip: str
    network: str
    country: str | None
    first: float
    last: float
    samples: int
    held_at_least: float
    held_at_most: float | None


def segments(samples: Sequence[Sample]) -> list[Segment]:
    ok = [s for s in samples if s.ok and s.ip]
    out: list[Segment] = []
    start = 0
    for i in range(1, len(ok) + 1):
        if i == len(ok) or ok[i].ip != ok[start].ip:
            run = ok[start:i]
            first, last = run[0], run[-1]
            out.append(
                Segment(
                    ip=str(first.ip),
                    network=first.network.label if first.network else "-",
                    country=first.exit.country if first.exit else None,
                    first=first.elapsed,
                    last=last.elapsed,
                    samples=len(run),
                    held_at_least=last.elapsed - first.elapsed,
                    held_at_most=(ok[i].elapsed - first.elapsed) if i < len(ok) else None,
                )
            )
            start = i
    return out


def _percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def subnet(ip: str) -> str:
    """The /24 of an IPv4 address or the /48 of an IPv6 address."""
    addr = ipaddress.ip_address(ip)
    prefix = 24 if addr.version == 4 else 48
    return str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False))


@dataclass
class WatchSummary:
    samples: int
    succeeded: int
    failed: int
    duration: float
    distinct_ips: int
    ip_changes: int
    network_changes: int
    reused_ips: list[str]
    networks: dict[str, int]
    countries: dict[str, int]
    longest_hold: float | None
    median_completed_hold: float | None
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    segments: list[Segment] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["segments"] = [s.__dict__ for s in self.segments]
        return data


def summarize_watch(samples: Sequence[Sample]) -> WatchSummary:
    segs = segments(samples)
    ok = [s for s in samples if s.ok and s.ip]
    seen: set[str] = set()
    reused: list[str] = []
    for seg in segs:
        if seg.ip in seen and seg.ip not in reused:
            reused.append(seg.ip)
        seen.add(seg.ip)
    network_changes = sum(1 for a, b in itertools.pairwise(segs) if a.network != b.network)
    completed = [s.held_at_least for s in segs if s.held_at_most is not None]
    latencies = [s.latency_ms for s in samples if s.ok and s.latency_ms is not None]
    return WatchSummary(
        samples=len(samples),
        succeeded=len(ok),
        failed=len(samples) - len(ok),
        duration=(samples[-1].elapsed - samples[0].elapsed) if samples else 0.0,
        distinct_ips=len({s.ip for s in ok}),
        ip_changes=max(len(segs) - 1, 0),
        network_changes=network_changes,
        reused_ips=reused,
        networks=dict(Counter(s.network.label if s.network else "-" for s in ok)),
        countries=dict(Counter((s.exit.country or "-") if s.exit else "-" for s in ok)),
        longest_hold=max((s.held_at_least for s in segs), default=None),
        median_completed_hold=statistics.median(completed) if completed else None,
        latency_p50_ms=_percentile(latencies, 0.5),
        latency_p95_ms=_percentile(latencies, 0.95),
        segments=segs,
    )


@dataclass
class RotationSummary:
    attempts: int
    triggered: int
    rotated: int
    repeated_ips: int  # rotations that landed on an IP already seen in this run
    seconds_to_new_ip_median: float | None
    seconds_to_new_ip_max: float | None
    networks: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def summarize_rotation(results: Sequence[RotationResult]) -> RotationSummary:
    seen: set[str] = set()
    if results and results[0].before_ip:
        seen.add(results[0].before_ip)
    repeated = 0
    networks: Counter[str] = Counter()
    for r in results:
        if r.changed and r.after_ip:
            if r.after_ip in seen:
                repeated += 1
            seen.add(r.after_ip)
        last_ok = next((s for s in reversed(r.samples) if s.ok and s.network), None)
        if last_ok and last_ok.network:
            networks[last_ok.network.label] += 1
    waits = [r.seconds_to_new_ip for r in results if r.seconds_to_new_ip is not None]
    return RotationSummary(
        attempts=len(results),
        triggered=sum(1 for r in results if r.triggered),
        rotated=sum(1 for r in results if r.changed),
        repeated_ips=repeated,
        seconds_to_new_ip_median=statistics.median(waits) if waits else None,
        seconds_to_new_ip_max=max(waits) if waits else None,
        networks=dict(networks),
    )


@dataclass
class PoolSummary:
    proxies: int
    succeeded: int
    distinct_ips: int
    distinct_subnets: int
    duplicate_ips: list[str]
    networks: dict[str, int]
    countries: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def summarize_pool(samples: Sequence[Sample]) -> PoolSummary:
    ok = [s for s in samples if s.ok and s.ip]
    ips = Counter(str(s.ip) for s in ok)
    return PoolSummary(
        proxies=len(samples),
        succeeded=len(ok),
        distinct_ips=len(ips),
        distinct_subnets=len({subnet(ip) for ip in ips}),
        duplicate_ips=sorted(ip for ip, n in ips.items() if n > 1),
        networks=dict(Counter(s.network.label if s.network else "-" for s in ok).most_common()),
        countries=dict(Counter((s.exit.country or "-") if s.exit else "-" for s in ok).most_common()),
    )
