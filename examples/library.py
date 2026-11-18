"""Watch one sticky session for 30 minutes and print how long each IP held.

PROXY_URL=http://user:pass@gw.example.net:8080 python examples/library.py
"""

from __future__ import annotations

import os

from mobile_proxy_tester import Sampler, ipinfo_lookup, parse_proxy, summarize_watch, watch


def main() -> None:
    proxy = parse_proxy(os.environ["PROXY_URL"])
    sampler = Sampler(proxy, lookup=ipinfo_lookup())
    samples = []
    for sample in watch(sampler, interval=60, duration=30 * 60):
        print(sample.index, sample.ip, sample.network.label if sample.network else "-")
        samples.append(sample)
    summary = summarize_watch(samples)
    for segment in summary.segments:
        print(f"{segment.ip}: held {segment.held_at_least:.0f}s or more, network {segment.network}")


if __name__ == "__main__":
    main()
