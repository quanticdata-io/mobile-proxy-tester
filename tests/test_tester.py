from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from conftest import ECHO_URL, FakeMobile, FakeTime
from mobile_proxy_tester import (
    Exit,
    HTTPLookup,
    Network,
    NoLookup,
    RotationTrigger,
    Sample,
    Sampler,
    parse_echo,
    parse_network,
    parse_proxy,
    quanticdata_lookup,
    rotate_and_wait,
    segments,
    summarize_pool,
    summarize_rotation,
    summarize_watch,
    watch,
)
from mobile_proxy_tester.analysis import subnet
from mobile_proxy_tester.cli import duration, main

MakeMobile = Callable[..., FakeMobile]
T0 = datetime(2026, 11, 18, tzinfo=timezone.utc)


def sample(
    i: int, elapsed: float, ip: str | None, network: str = "AS1 Carrier", country: str = "US"
) -> Sample:
    ok = ip is not None
    asn, _, name = network.partition(" ")
    return Sample(
        i,
        T0,
        elapsed,
        ok,
        100.0 + i if ok else None,
        Exit(ip, country) if ip else None,
        Network(asn, name) if ok else None,
        None if ok else "timeout",
    )


# -- parsing --------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "ip", "country"),
    [
        (
            '{"type":"response","payload":{"ip":"203.0.113.10","countryCode":"US","city":"Ohio"}}',
            "203.0.113.10",
            "US",
        ),
        ('{"origin": "198.51.100.1, 192.0.2.1"}', "198.51.100.1", None),
        ('{"query": "2001:db8::5", "countryCode": "DE"}', "2001:db8::5", "DE"),
        ("203.0.113.9\n", "203.0.113.9", None),
    ],
)
def test_parse_echo(body: str, ip: str, country: str | None) -> None:
    exit_ = parse_echo(body)
    assert (exit_.ip, exit_.country) == (ip, country)


def test_parse_echo_rejects_garbage() -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        parse_echo("<html>blocked</html>")
    with pytest.raises(ValueError):  # noqa: PT011
        parse_echo('{"status": "ok"}')


def test_parse_network_shapes() -> None:
    qd = parse_network(
        {"type": "response", "payload": {"as": "AS21928 T-Mobile USA, Inc.", "isp": "T-Mobile USA"}}
    )
    assert (qd.asn, qd.operator) == ("AS21928", "T-Mobile USA")
    ipinfo = parse_network({"org": "AS6167 Verizon Business", "country": "US"})
    assert ipinfo.label == "AS6167 Verizon Business"
    nested = parse_network({"asn": {"asn": "AS3320", "name": "Deutsche Telekom AG"}})
    assert nested.label == "AS3320 Deutsche Telekom AG"
    numeric = parse_network({"asn": 3320, "as_name": "DTAG"})
    assert numeric.label == "AS3320 DTAG"
    assert Network().label == "-"


@pytest.mark.parametrize(
    ("text", "seconds"), [("90", 90), ("30s", 30), ("15m", 900), ("2h", 7200), ("500ms", 0.5)]
)
def test_duration(text: str, seconds: float) -> None:
    assert duration(text) == seconds


# -- analysis -------------------------------------------------------------


def test_segments_bounds_and_reuse() -> None:
    samples = [
        sample(1, 0, "203.0.113.10"),
        sample(2, 60, "203.0.113.10"),
        sample(3, 120, None),
        sample(4, 180, "198.51.100.20", "AS6167 Verizon"),
        sample(5, 240, "203.0.113.10"),
    ]
    segs = segments(samples)
    assert [(s.ip, s.held_at_least, s.held_at_most) for s in segs] == [
        ("203.0.113.10", 60, 180),
        ("198.51.100.20", 0, 60),
        ("203.0.113.10", 0, None),
    ]
    summary = summarize_watch(samples)
    assert summary.failed == 1
    assert summary.distinct_ips == 2
    assert summary.ip_changes == 2
    assert summary.network_changes == 2
    assert summary.reused_ips == ["203.0.113.10"]
    assert summary.longest_hold == 60
    assert summary.median_completed_hold == 30
    assert summary.networks == {"AS1 Carrier": 3, "AS6167 Verizon": 1}


def test_pool_summary_subnets_and_duplicates() -> None:
    samples = [
        sample(1, 0, "203.0.113.10"),
        sample(2, 0, "203.0.113.11"),
        sample(3, 0, "203.0.113.11"),
        sample(4, 0, "2001:db8:1::1"),
        sample(5, 0, None),
    ]
    summary = summarize_pool(samples)
    assert summary.succeeded == 4
    assert summary.distinct_ips == 3
    assert summary.distinct_subnets == 2
    assert summary.duplicate_ips == ["203.0.113.11"]
    assert subnet("2001:db8:1::1") == "2001:db8:1::/48"


# -- sampling through a real local proxy ----------------------------------


def test_sampler_through_proxy_with_lookup_cache(make_mobile: MakeMobile) -> None:
    fake = make_mobile(["203.0.113.10"], credentials=("user-isp-t_mobile_usa", "pw"))
    lookup = HTTPLookup(fake.base + "/lookup/{ip}")
    sampler = Sampler(parse_proxy(fake.proxy_url), echo_url=ECHO_URL, lookup=lookup, timeout=5)
    first, second = sampler.sample(), sampler.sample()
    assert first.ok and first.ip == "203.0.113.10"
    assert first.network and first.network.label == "AS21928 T-Mobile USA, Inc."
    assert second.ip == first.ip
    assert fake.lookups == ["203.0.113.10"]  # cached
    assert fake.echo_requests == 2


def test_sampler_reports_proxy_errors(make_mobile: MakeMobile) -> None:
    fake = make_mobile(["203.0.113.10"], credentials=("u", "right"))
    bad = parse_proxy(fake.proxy_url.replace("right", "wrong"))
    result = Sampler(bad, echo_url=ECHO_URL, lookup=NoLookup(), timeout=5).sample()
    assert not result.ok and result.error == "HTTP 407"


def test_keepalive_reuses_one_connection(make_mobile: MakeMobile) -> None:
    fake = make_mobile(["203.0.113.10", "203.0.113.11"], per_request=True)
    sampler = Sampler(parse_proxy(fake.base), echo_url=ECHO_URL, keepalive=True, timeout=5)
    ips = [sampler.sample().ip for _ in range(3)]
    sampler.close()
    assert ips == ["203.0.113.10", "203.0.113.11", "203.0.113.10"]


def test_watch_schedule(make_mobile: MakeMobile, fake_time: FakeTime) -> None:
    fake = make_mobile(["203.0.113.10", "198.51.100.20"])
    fake_time.on_sleep = lambda now: setattr(fake, "index", 1 if now >= 120 else 0)
    sampler = Sampler(parse_proxy(fake.base), echo_url=ECHO_URL, clock=fake_time.clock, timeout=5)
    samples = list(watch(sampler, interval=60, duration=240, sleep=fake_time.sleep, clock=fake_time.clock))
    assert [s.elapsed for s in samples] == [0, 60, 120, 180, 240]
    assert [s.ip for s in samples] == ["203.0.113.10"] * 2 + ["198.51.100.20"] * 3
    with pytest.raises(ValueError, match="duration or a count"):
        next(watch(sampler, interval=1))


def test_rotate_and_wait(make_mobile: MakeMobile, fake_time: FakeTime) -> None:
    fake = make_mobile(["203.0.113.10", "198.51.100.20", "203.0.113.10"])
    sampler = Sampler(parse_proxy(fake.base), echo_url=ECHO_URL, clock=fake_time.clock, timeout=5)
    trigger = RotationTrigger(fake.base + "/rotate")
    results = list(
        rotate_and_wait(
            sampler,
            trigger,
            attempts=2,
            poll=1,
            max_wait=5,
            cooldown=10,
            sleep=fake_time.sleep,
            clock=fake_time.clock,
        )
    )
    assert [(r.before_ip, r.after_ip, r.changed) for r in results] == [
        ("203.0.113.10", "198.51.100.20", True),
        ("198.51.100.20", "203.0.113.10", True),
    ]
    summary = summarize_rotation(results)
    assert (summary.rotated, summary.repeated_ips) == (2, 1)
    assert summary.seconds_to_new_ip_median == 0


def test_rotation_that_does_not_change(make_mobile: MakeMobile, fake_time: FakeTime) -> None:
    fake = make_mobile(["203.0.113.10", "198.51.100.20"], rotate_sticks=True)
    sampler = Sampler(parse_proxy(fake.base), echo_url=ECHO_URL, clock=fake_time.clock, timeout=5)
    results = list(
        rotate_and_wait(
            sampler,
            RotationTrigger(fake.base + "/rotate"),
            attempts=1,
            poll=2,
            max_wait=6,
            sleep=fake_time.sleep,
            clock=fake_time.clock,
        )
    )
    assert not results[0].changed
    assert len(results[0].samples) == 4  # t=0, 2, 4 and 6 = max_wait, then it gives up
    failed = make_mobile(["203.0.113.10"], rotate_status=429)
    sampler = Sampler(parse_proxy(failed.base), echo_url=ECHO_URL, timeout=5)
    result = next(
        rotate_and_wait(sampler, RotationTrigger(failed.base + "/rotate"), attempts=1, sleep=fake_time.sleep)
    )
    assert not result.triggered and result.trigger_status == "HTTP 429"


def test_quanticdata_lookup(make_mobile: MakeMobile) -> None:
    fake = make_mobile(["203.0.113.10"])
    lookup = quanticdata_lookup(api_key="qd_test_key", base_url=fake.base + "/v1")
    assert lookup.lookup("203.0.113.10").label == "AS21928 T-Mobile USA, Inc."
    unauthorized = quanticdata_lookup(api_key="other", base_url=fake.base + "/v1")
    assert unauthorized.lookup("203.0.113.10") == Network()
    with pytest.raises(ValueError, match="placeholder"):
        HTTPLookup("https://example.test/")


# -- command line ---------------------------------------------------------


def run(argv: list[str], fake_time: FakeTime | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    kwargs = {"sleep": fake_time.sleep, "clock": fake_time.clock} if fake_time else {}
    code = main(argv, out=out, err=err, **kwargs)  # type: ignore[arg-type]
    return code, out.getvalue(), err.getvalue()


def test_cli_watch_sticky_holds(make_mobile: MakeMobile, fake_time: FakeTime, tmp_path: Path) -> None:
    fake = make_mobile(["203.0.113.10"], credentials=("u", "s3cret"))
    report = tmp_path / "samples.csv"
    code, out, err = run(
        [
            "watch",
            fake.proxy_url,
            "--echo-url",
            ECHO_URL,
            "--lookup",
            fake.base + "/lookup/{ip}",
            "--interval",
            "5m",
            "--duration",
            "30m",
            "--min-hold",
            "20m",
            "--expect-country",
            "us",
            "--csv",
            str(report),
        ],
        fake_time,
    )
    assert code == 0, out + err
    assert "7 samples over 30.0m" in out
    assert "1 distinct" in out
    assert "s3cret" not in out + err
    rows = list(csv.DictReader(report.open(encoding="utf-8")))
    assert len(rows) == 7
    assert rows[0]["asn"] == "AS21928"


def test_cli_watch_detects_early_change(make_mobile: MakeMobile, fake_time: FakeTime) -> None:
    fake = make_mobile(["203.0.113.10", "198.51.100.20"])
    fake_time.on_sleep = lambda now: setattr(fake, "index", 1 if now >= 600 else 0)
    code, out, err = run(
        [
            "watch",
            fake.base,
            "--echo-url",
            ECHO_URL,
            "--lookup",
            fake.base + "/lookup/{ip}",
            "--interval",
            "5m",
            "--count",
            "4",
            "--min-hold",
            "30m",
        ],
        fake_time,
    )
    assert code == 1
    assert "FAIL: 1 exit IP(s) changed before 30.0m" in out
    assert "IP CHANGED, NETWORK CHANGED" in err


def test_cli_watch_rotating_string_json(make_mobile: MakeMobile, fake_time: FakeTime) -> None:
    fake = make_mobile(["203.0.113.10", "203.0.113.11", "198.51.100.20"], per_request=True)
    code, out, _ = run(
        [
            "watch",
            fake.base,
            "--echo-url",
            ECHO_URL,
            "--lookup",
            "none",
            "--interval",
            "0",
            "--count",
            "3",
            "--expect-rotation",
            "--json",
        ],
        fake_time,
    )
    assert code == 0
    data = json.loads(out)
    assert data["summary"]["distinct_ips"] == 3
    assert [s["ip"] for s in data["samples"]] == ["203.0.113.10", "203.0.113.11", "198.51.100.20"]


def test_cli_rotate(make_mobile: MakeMobile, fake_time: FakeTime) -> None:
    fake = make_mobile(["203.0.113.10", "198.51.100.20", "203.0.113.11"])
    code, out, _ = run(
        [
            "rotate",
            fake.base,
            "--rotate-url",
            fake.base + "/rotate",
            "--echo-url",
            ECHO_URL,
            "--lookup",
            "none",
            "--attempts",
            "2",
            "--cooldown",
            "10s",
        ],
        fake_time,
    )
    assert code == 0, out
    assert "2/2 rotations gave a new IP" in out


def test_cli_pool(make_mobile: MakeMobile, tmp_path: Path) -> None:
    a, b = make_mobile(["203.0.113.10"]), make_mobile(["198.51.100.20"])
    listing = tmp_path / "list.txt"
    listing.write_text(f"{a.base}\n{b.base}\n", encoding="utf-8")
    code, out, _ = run(
        ["pool", "-f", str(listing), "--echo-url", ECHO_URL, "--lookup", a.base + "/lookup/{ip}", "-q"]
    )
    assert code == 0
    assert "2/2 proxies answered: 2 distinct exit IPs in 2 subnets" in out
    assert "AS6167 Verizon Business" in out


def test_cli_quanticdata_generate_and_carriers(
    monkeypatch: pytest.MonkeyPatch, fake_time: FakeTime, make_mobile: MakeMobile
) -> None:
    fake = make_mobile(["203.0.113.10"])
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/generate"):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"type": "response", "payload": {"proxies": [fake.base]}})
        tree = {
            "us": {
                "regions": {
                    "ohio": {
                        "cities": {
                            "cincinnati": {
                                "isps": [
                                    {"code": "t_mobile_usa", "name": "T-Mobile Usa"},
                                    {"code": "verizon_wireless", "name": "Verizon"},
                                ]
                            }
                        }
                    }
                }
            }
        }
        return httpx.Response(200, json={"type": "response", "payload": {"data": tree}})

    import mobile_proxy_tester.quanticdata as qd

    real = qd.QuanticDataClient.__init__

    def patched(self: qd.QuanticDataClient, *args: object, **kwargs: object) -> None:
        real(self, api_key="qd_test_key", transport=httpx.MockTransport(handler))

    monkeypatch.setattr(qd.QuanticDataClient, "__init__", patched)
    code, out, err = run(
        [
            "watch",
            "--qd-order",
            "ord_1",
            "--qd-country",
            "us",
            "--qd-isp",
            "t_mobile_usa",
            "--qd-super-sticky",
            "--echo-url",
            ECHO_URL,
            "--lookup",
            "none",
            "--count",
            "2",
            "--interval",
            "1s",
        ],
        fake_time,
    )
    assert code == 0, err
    assert seen[0] == {
        "orderId": "ord_1",
        "quantity": 1,
        "protocol": "http",
        "format": "http://user:pass@host:port",
        "country": "us",
        "isp": "t_mobile_usa",
        "superSticky": True,
    }
    code, out, _ = run(["carriers", "--country", "us"])
    assert code == 0
    assert "t_mobile_usa" in out and "2 carrier entries" in out


def test_cli_usage_errors(fake_time: FakeTime) -> None:
    assert run(["watch"], fake_time)[0] == 2
    assert run(["watch", "not a proxy"], fake_time)[0] == 2
    assert run(["nope"])[0] == 2
