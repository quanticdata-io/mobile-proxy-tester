"""Command line: ``mobile-proxy-tester watch|rotate|pool|carriers``."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections.abc import Callable, Sequence
from typing import IO, Any

from ._version import __version__
from .analysis import (
    PoolSummary,
    RotationSummary,
    WatchSummary,
    summarize_pool,
    summarize_rotation,
    summarize_watch,
)
from .lookup import DEFAULT_ECHO_URL, HTTPLookup, NetworkLookup, NoLookup, ipinfo_lookup, quanticdata_lookup
from .proxy import Proxy, ProxyParseError, load_file, parse_proxy
from .quanticdata import QuanticDataClient, QuanticDataError
from .sampler import RotationResult, RotationTrigger, Sample, Sampler, rotate_and_wait, watch

EXIT_OK, EXIT_ASSERT, EXIT_USAGE, EXIT_RUNTIME = 0, 1, 2, 3

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*$")


def duration(text: str) -> float:
    """``90``, ``90s``, ``15m``, ``2h``, ``500ms`` -> seconds."""
    match = _DURATION.match(text)
    if not match:
        raise argparse.ArgumentTypeError(f"not a duration: {text!r} (examples: 30s, 15m, 2h)")
    value, unit = float(match.group(1)), match.group(2) or "s"
    return value * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]


def clock(seconds: float) -> str:
    seconds = round(seconds)
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def human(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--echo-url", default=DEFAULT_ECHO_URL, help="IP echo called through the proxy")
    p.add_argument(
        "--lookup",
        default="auto",
        help="network (ASN/carrier) lookup: quanticdata, ipinfo, none, or a URL with {ip}. "
        "auto = quanticdata when QUANTICDATA_API_KEY is set, else none",
    )
    p.add_argument("--timeout", type=duration, default=20.0, help="timeout of one request (20s)")
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    p.add_argument("--csv", metavar="FILE", help="write every sample to a CSV file")
    p.add_argument("-q", "--quiet", action="store_true", help="no live output, only the summary")
    p.add_argument("--show-credentials", action="store_true", help="do not mask proxy passwords")
    qd = p.add_argument_group("QuanticData (instead of a PROXY argument; needs QUANTICDATA_API_KEY)")
    qd.add_argument("--qd-order", metavar="ORDER_ID", help="generate the proxy string(s) from this plan")
    qd.add_argument("--qd-country")
    qd.add_argument("--qd-state")
    qd.add_argument("--qd-city")
    qd.add_argument("--qd-isp", help="carrier code (see the carriers command)")
    qd.add_argument("--qd-rotation", choices=["rotating", "sticky"])
    qd.add_argument("--qd-session-time", type=int, metavar="MINUTES")
    qd.add_argument(
        "--qd-super-sticky", action="store_true", help="one IP for as long as the device is online"
    )
    qd.add_argument("--qd-protocol", choices=["http", "socks5"], default="http")
    qd.add_argument("--qd-gateway", choices=["ww", "us", "eu", "as"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mobile-proxy-tester",
        description="Measure how a mobile (or any) proxy's exit IP and carrier change over time.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    w = sub.add_parser(
        "watch", help="sample one proxy on an interval: how long each exit IP and carrier holds"
    )
    w.add_argument("proxy", nargs="?", help="proxy string, e.g. http://user:pass@host:port")
    w.add_argument("--interval", type=duration, default=30.0, help="time between samples (30s)")
    w.add_argument("--duration", type=duration, help="stop after this long (default 10m unless --count)")
    w.add_argument("--count", type=int, help="stop after this many samples")
    w.add_argument(
        "--keepalive", action="store_true", help="reuse one connection instead of a new one per sample"
    )
    w.add_argument("--min-hold", type=duration, help="fail if the exit IP changed before holding this long")
    w.add_argument("--expect-country", metavar="CC", help="fail if a sample exits from another country")
    w.add_argument("--expect-rotation", action="store_true", help="fail unless every sample has a new IP")
    _common(w)

    r = sub.add_parser("rotate", help="trigger IP changes with a rotation URL and time each one")
    r.add_argument("proxy", nargs="?", help="proxy string of the session the URL rotates")
    r.add_argument("--rotate-url", required=True, help="the provider's change-IP URL")
    r.add_argument("--rotate-method", default="GET", choices=["GET", "POST"])
    r.add_argument("--attempts", type=int, default=5)
    r.add_argument("--poll", type=duration, default=2.0, help="time between checks after a rotation (2s)")
    r.add_argument("--max-wait", type=duration, default=60.0, help="give up on one rotation after (60s)")
    r.add_argument("--cooldown", type=duration, default=10.0, help="wait between rotations (10s)")
    _common(r)

    pl = sub.add_parser(
        "pool", help="one request through each proxy of a list: carriers, IP and subnet spread"
    )
    pl.add_argument("proxies", nargs="*", help="proxy strings (or -f FILE, or --qd-order with --qd-quantity)")
    pl.add_argument("-f", "--file", help="proxy list, one per line")
    pl.add_argument("--qd-quantity", type=int, default=10)
    _common(pl)

    c = sub.add_parser("carriers", help="QuanticData: list mobile carriers by country, region and city")
    c.add_argument("--country", help="two-letter country code")
    c.add_argument("--json", action="store_true")
    return parser


def _lookup(args: argparse.Namespace) -> NetworkLookup:
    choice = args.lookup
    if choice == "auto":
        choice = "quanticdata" if os.environ.get("QUANTICDATA_API_KEY") else "none"
    if choice == "none":
        return NoLookup()
    if choice == "quanticdata":
        return quanticdata_lookup()
    if choice == "ipinfo":
        return ipinfo_lookup()
    return HTTPLookup(choice)


def _qd_targeting(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "country": args.qd_country,
        "state": args.qd_state,
        "city": args.qd_city,
        "isp": args.qd_isp,
        "rotation": args.qd_rotation,
        "sessionTime": args.qd_session_time,
        "superSticky": args.qd_super_sticky,
        "gateway": args.qd_gateway,
    }


def _proxies(args: argparse.Namespace, quantity: int) -> list[Proxy]:
    if args.qd_order:
        client = QuanticDataClient()
        try:
            lines = client.generate(
                args.qd_order, quantity=quantity, protocol=args.qd_protocol, **_qd_targeting(args)
            )
        finally:
            client.close()
        if not lines:
            raise QuanticDataError(200, "no proxy string returned for this order")
        return [parse_proxy(line, args.qd_protocol) for line in lines]
    found: list[Proxy] = []
    for text in getattr(args, "proxies", None) or ([args.proxy] if getattr(args, "proxy", None) else []):
        found.append(parse_proxy(text))
    if getattr(args, "file", None):
        found.extend(load_file(args.file))
    return found


def _display(proxy: Proxy, show: bool) -> str:
    return proxy.url if show else proxy.masked


def _line(sample: Sample, previous: Sample | None) -> str:
    if not sample.ok:
        return f"[{clock(sample.elapsed)}] #{sample.index:<4} FAILED  {sample.error}"
    exit_ = sample.exit
    assert exit_ is not None
    where = " ".join(p for p in (exit_.country, exit_.city) if p) or "-"
    network = sample.network.label if sample.network else "-"
    note = ""
    if previous is not None and previous.ok and previous.ip != sample.ip:
        note = "  IP CHANGED"
        if previous.network and sample.network and previous.network.label != sample.network.label:
            note += ", NETWORK CHANGED"
    if sample.event == "rotate":
        note = "  (after rotation)" + note
    latency = f"{sample.latency_ms:.0f}ms" if sample.latency_ms is not None else "-"
    head = f"[{clock(sample.elapsed)}] #{sample.index:<4}"
    return f"{head} {exit_.ip:<39} {where:<22} {network:<40} {latency}{note}"


CSV_COLUMNS = [
    "index",
    "utc",
    "elapsed_s",
    "ok",
    "ip",
    "country",
    "region",
    "city",
    "asn",
    "operator",
    "latency_ms",
    "event",
    "error",
]


def _csv_row(s: Sample) -> list[Any]:
    e, n = s.exit, s.network
    return [
        s.index,
        s.at.isoformat(timespec="seconds"),
        round(s.elapsed, 3),
        s.ok,
        s.ip or "",
        (e.country if e else "") or "",
        (e.region if e else "") or "",
        (e.city if e else "") or "",
        (n.asn if n else "") or "",
        (n.operator if n else "") or "",
        round(s.latency_ms, 1) if s.latency_ms is not None else "",
        s.event or "",
        s.error or "",
    ]


def _write_csv(path: str, samples: Sequence[Sample]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        writer.writerows(_csv_row(s) for s in samples)


def _sample_dict(s: Sample) -> dict[str, Any]:
    return dict(zip(CSV_COLUMNS, _csv_row(s), strict=True))


def _print_counts(out: IO[str], title: str, counts: dict[str, int]) -> None:
    if counts:
        out.write(f"{title}:\n")
        for name, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            out.write(f"  {n:>4}  {name}\n")


def _print_watch(out: IO[str], s: WatchSummary) -> None:
    out.write(
        f"\n{s.samples} samples over {human(s.duration)}: {s.succeeded} ok, {s.failed} failed\n"
        f"exit IPs: {s.distinct_ips} distinct, {s.ip_changes} changes, "
        f"network changes: {s.network_changes}, IPs seen again later: {len(s.reused_ips)}\n"
        f"longest hold: {human(s.longest_hold)}, "
        f"median hold of IPs that changed: {human(s.median_completed_hold)}\n"
        f"latency p50 {s.latency_p50_ms or 0:.0f}ms, p95 {s.latency_p95_ms or 0:.0f}ms\n"
    )
    if s.segments:
        out.write("\nIP                                      HELD (at least .. at most)  SAMPLES  NETWORK\n")
        for seg in s.segments:
            upper = human(seg.held_at_most) if seg.held_at_most is not None else "open"
            held = f"{human(seg.held_at_least)} .. {upper}"
            out.write(f"{seg.ip:<39} {held:<27} {seg.samples:>7}  {seg.network}\n")
    _print_counts(out, "\nnetworks (by samples)", s.networks)


def _print_rotation(out: IO[str], s: RotationSummary, results: Sequence[RotationResult]) -> None:
    out.write(f"\n{'ATTEMPT':>7}  {'TRIGGER':<9} {'BEFORE':<39} {'AFTER':<39} TIME\n")
    for r in results:
        status = r.trigger_status if r.triggered else f"failed ({r.trigger_status})"
        after = r.after_ip if r.changed else f"unchanged ({r.after_ip or '-'})"
        out.write(
            f"{r.attempt:>7}  {status:<9} {r.before_ip or '-':<39} {after:<39} {human(r.seconds_to_new_ip)}\n"
        )
    out.write(
        f"\n{s.rotated}/{s.attempts} rotations gave a new IP, {s.repeated_ips} landed on an IP already seen; "
        f"time to new IP: median {human(s.seconds_to_new_ip_median)}, max {human(s.seconds_to_new_ip_max)}\n"
    )
    _print_counts(out, "networks after rotation", s.networks)


def _print_pool(out: IO[str], s: PoolSummary) -> None:
    out.write(
        f"\n{s.succeeded}/{s.proxies} proxies answered: {s.distinct_ips} distinct exit IPs in "
        f"{s.distinct_subnets} subnets (/24 or /48)\n"
    )
    if s.duplicate_ips:
        out.write(f"shared exit IPs: {', '.join(s.duplicate_ips)}\n")
    _print_counts(out, "networks", s.networks)
    _print_counts(out, "countries", s.countries)


def cmd_watch(
    args: argparse.Namespace,
    out: IO[str],
    err: IO[str],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> int:
    proxies = _proxies(args, 1)
    if len(proxies) != 1:
        err.write("watch needs exactly one proxy (argument or --qd-order)\n")
        return EXIT_USAGE
    proxy = proxies[0]
    if args.duration is None and args.count is None:
        args.duration = 600.0
    sampler = Sampler(
        proxy,
        echo_url=args.echo_url,
        lookup=_lookup(args),
        timeout=args.timeout,
        keepalive=args.keepalive,
        clock=clock,
    )
    if not args.quiet and not args.json:
        err.write(f"watching {_display(proxy, args.show_credentials)} every {human(args.interval)}\n")
    samples: list[Sample] = []
    try:
        for sample in watch(
            sampler,
            interval=args.interval,
            duration=args.duration,
            count=args.count,
            sleep=sleep,
            clock=clock,
        ):
            if not args.quiet and not args.json:
                err.write(_line(sample, samples[-1] if samples else None) + "\n")
                err.flush()
            samples.append(sample)
    except KeyboardInterrupt:
        err.write("interrupted: summarising the samples taken so far\n")
    finally:
        sampler.close()
    summary = summarize_watch(samples)
    if args.csv:
        _write_csv(args.csv, samples)
    failures = _watch_assertions(args, summary, samples)
    if args.json:
        json.dump(
            {
                "proxy": _display(proxy, args.show_credentials),
                "summary": summary.to_dict(),
                "samples": [_sample_dict(s) for s in samples],
                "failures": failures,
            },
            out,
            indent=2,
        )
        out.write("\n")
    else:
        _print_watch(out, summary)
        for failure in failures:
            out.write(f"FAIL: {failure}\n")
    return EXIT_ASSERT if failures else EXIT_OK


def _watch_assertions(args: argparse.Namespace, s: WatchSummary, samples: Sequence[Sample]) -> list[str]:
    failures: list[str] = []
    if s.succeeded == 0:
        failures.append("no sample succeeded")
    if args.min_hold is not None:
        early = [
            seg for seg in s.segments if seg.held_at_most is not None and seg.held_at_most < args.min_hold
        ]
        if early:
            failures.append(
                f"{len(early)} exit IP(s) changed before {human(args.min_hold)} "
                f"(shortest held at most {human(min(seg.held_at_most or 0 for seg in early))})"
            )
    if args.expect_country:
        want = args.expect_country.upper()
        wrong = [x for x in samples if x.ok and x.exit and (x.exit.country or "").upper() not in (want, "")]
        if wrong:
            failures.append(f"{len(wrong)} sample(s) exited outside {want}")
    if args.expect_rotation and s.succeeded > 1 and s.ip_changes < s.succeeded - 1:
        failures.append(f"exit IP did not change on every sample ({s.ip_changes} changes in {s.succeeded})")
    return failures


def cmd_rotate(
    args: argparse.Namespace,
    out: IO[str],
    err: IO[str],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> int:
    proxies = _proxies(args, 1)
    if len(proxies) != 1:
        err.write("rotate needs exactly one proxy (argument or --qd-order)\n")
        return EXIT_USAGE
    proxy = proxies[0]
    sampler = Sampler(proxy, echo_url=args.echo_url, lookup=_lookup(args), timeout=args.timeout, clock=clock)
    trigger = RotationTrigger(args.rotate_url, args.rotate_method)
    results: list[RotationResult] = []
    try:
        for result in rotate_and_wait(
            sampler,
            trigger,
            attempts=args.attempts,
            poll=args.poll,
            max_wait=args.max_wait,
            cooldown=args.cooldown,
            sleep=sleep,
            clock=clock,
        ):
            results.append(result)
            if not args.quiet and not args.json:
                change = (
                    f"-> {result.after_ip} in {human(result.seconds_to_new_ip)}"
                    if result.changed
                    else "no change"
                )
                err.write(
                    f"rotation {result.attempt}: {result.trigger_status}, {result.before_ip} {change}\n"
                )
    except KeyboardInterrupt:
        err.write("interrupted\n")
    finally:
        sampler.close()
        trigger.close()
    summary = summarize_rotation(results)
    samples = [s for r in results for s in r.samples]
    if args.csv:
        _write_csv(args.csv, samples)
    failures = (
        []
        if summary.rotated == summary.attempts
        else [f"{summary.attempts - summary.rotated} rotation(s) gave no new IP"]
    )
    if args.json:
        json.dump(
            {
                "proxy": _display(proxy, args.show_credentials),
                "summary": summary.to_dict(),
                "rotations": [
                    {
                        "attempt": r.attempt,
                        "triggered": r.triggered,
                        "trigger_status": r.trigger_status,
                        "before_ip": r.before_ip,
                        "after_ip": r.after_ip,
                        "changed": r.changed,
                        "seconds_to_new_ip": r.seconds_to_new_ip,
                    }
                    for r in results
                ],
                "failures": failures,
            },
            out,
            indent=2,
        )
        out.write("\n")
    else:
        _print_rotation(out, summary, results)
        for failure in failures:
            out.write(f"FAIL: {failure}\n")
    return EXIT_ASSERT if failures else EXIT_OK


def cmd_pool(args: argparse.Namespace, out: IO[str], err: IO[str]) -> int:
    proxies = _proxies(args, args.qd_quantity)
    if not proxies:
        err.write("pool needs proxies: arguments, -f FILE or --qd-order\n")
        return EXIT_USAGE
    lookup = _lookup(args)
    samples: list[Sample] = []
    for proxy in proxies:
        sample = Sampler(proxy, echo_url=args.echo_url, lookup=lookup, timeout=args.timeout).sample()
        samples.append(sample)
        if not args.quiet and not args.json:
            err.write(
                f"{_display(proxy, args.show_credentials):<60} "
                + _line(sample, None).split("] ", 1)[1]
                + "\n"
            )
    summary = summarize_pool(samples)
    if args.csv:
        _write_csv(args.csv, samples)
    if args.json:
        json.dump(
            {"summary": summary.to_dict(), "samples": [_sample_dict(s) for s in samples]}, out, indent=2
        )
        out.write("\n")
    else:
        _print_pool(out, summary)
    return EXIT_OK if summary.succeeded else EXIT_ASSERT


def cmd_carriers(args: argparse.Namespace, out: IO[str]) -> int:
    client = QuanticDataClient()
    try:
        carriers = client.carriers(args.country)
    finally:
        client.close()
    if args.json:
        json.dump([c.__dict__ for c in carriers], out, indent=2)
        out.write("\n")
        return EXIT_OK
    out.write(f"{'COUNTRY':<8}{'REGION':<24}{'CITY':<24}{'ISP CODE':<28}NAME\n")
    for c in carriers:
        out.write(f"{c.country:<8}{c.region:<24}{c.city:<24}{c.code:<28}{c.name}\n")
    out.write(
        f"\n{len(carriers)} carrier entries. "
        "Pass ISP CODE as --qd-isp, with --qd-country, --qd-state and --qd-city.\n"
    )
    return EXIT_OK


def main(
    argv: Sequence[str] | None = None,
    *,
    out: IO[str] | None = None,
    err: IO[str] | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> int:
    import time

    out = out or sys.stdout
    err = err or sys.stderr
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0) and EXIT_USAGE
    try:
        if args.command == "watch":
            return cmd_watch(args, out, err, sleep, clock)
        if args.command == "rotate":
            return cmd_rotate(args, out, err, sleep, clock)
        if args.command == "pool":
            return cmd_pool(args, out, err)
        return cmd_carriers(args, out)
    except (ProxyParseError, ValueError) as exc:
        err.write(f"error: {exc}\n")
        return EXIT_USAGE
    except (QuanticDataError, OSError) as exc:
        err.write(f"error: {exc}\n")
        return EXIT_RUNTIME


if __name__ == "__main__":
    raise SystemExit(main())
