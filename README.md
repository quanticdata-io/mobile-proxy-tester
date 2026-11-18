# mobile-proxy-tester

[![CI](https://github.com/quanticdata-io/mobile-proxy-tester/actions/workflows/ci.yml/badge.svg)](https://github.com/quanticdata-io/mobile-proxy-tester/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Measure how a mobile proxy's exit IP and carrier change over time. A one-off check tells you a proxy works; this tells you what you actually need to know before you run accounts, ad checks or app tests on 4G/5G IPs:

- **watch**: how long does a sticky session really keep its IP, does the carrier (ASN) stay the same, does the IP come back later;
- **rotate**: when you call the provider's "change IP" URL, how long until a new IP shows up, and how often is it one you already had;
- **pool**: across a list of sessions, how many distinct IPs, subnets and carriers do you really get.

It works with any HTTP or SOCKS5 proxy from any provider (mobile, residential or other), and reads the carrier from a network lookup you choose. QuanticData mobile plans are supported directly.

```text
$ mobile-proxy-tester watch "http://user:pass@gw.example.net:8080" --interval 5m --duration 2h --lookup ipinfo
watching http://user:*****@gw.example.net:8080 every 5.0m
[00:00:00] #1    203.0.113.10                            US Cincinnati          AS21928 T-Mobile USA, Inc.               -
[00:05:00] #2    203.0.113.10                            US Cincinnati          AS21928 T-Mobile USA, Inc.               -
...
[01:35:00] #20   203.0.113.11                            US Cincinnati          AS21928 T-Mobile USA, Inc.               -  IP CHANGED

25 samples over 2.0h: 25 ok, 0 failed
exit IPs: 2 distinct, 1 changes, network changes: 0, IPs seen again later: 0
longest hold: 1.5h, median hold of IPs that changed: 1.5h
```

(Illustrative output with documentation addresses and latencies left out; your results are your proxy's.)

## Install

Not on PyPI yet: install from source (Python 3.10 or later).

```bash
pip install "mobile-proxy-tester[socks] @ git+https://github.com/quanticdata-io/mobile-proxy-tester"
mobile-proxy-tester --help        # or: python -m mobile_proxy_tester --help
```

The `socks` extra is needed only for `socks5://` proxies.

## How it measures

Each **sample** is one request to an **IP echo** through the proxy, on a new connection (so a gateway that rotates per connection shows it; `--keepalive` reuses one connection instead). The echo answers with the exit IP and a location. Each new exit IP is then looked up **directly, not through the proxy**, to get its ASN and operator, which for a mobile IP is the carrier. Lookups are cached for the run.

| Part | Default | Change with |
|---|---|---|
| IP echo | `https://api.quanticdata.io/v1/public/ip-echo`: free, no account, never cached; location as Cloudflare sees it | `--echo-url` (any service that answers JSON with `ip`/`origin`/`query`, or plain text) |
| Network lookup | `quanticdata` when `QUANTICDATA_API_KEY` is set, otherwise `none` | `--lookup quanticdata`, `--lookup ipinfo` (`IPINFO_TOKEN` optional), `--lookup none`, or `--lookup "https://your.service/{ip}"` |

Why the time bounds are a range: with samples every 5 minutes, an IP seen at 00:00 and 00:20 and replaced at 00:25 held for **at least 20 and at most 25 minutes**. The report shows both; shorter intervals give tighter bounds and more traffic.

## watch: how long a session holds

```bash
mobile-proxy-tester watch PROXY [--interval 30s] [--duration 10m | --count N] [checks]
```

| Option | Meaning |
|---|---|
| `--interval` | time between samples (default 30s; units ms, s, m, h) |
| `--duration`, `--count` | when to stop (default 10 minutes); Ctrl-C stops early and still prints the summary |
| `--keepalive` | reuse one connection for all samples |
| `--min-hold 20m` | exit code 1 if any IP changed before holding 20 minutes (a sticky-session check) |
| `--expect-country US` | exit code 1 if a sample exits from another country |
| `--expect-rotation` | exit code 1 unless every sample has a new IP (a rotating-string check; use `--interval 0 --count 20`) |

The summary lists every IP with how long it held, how many times the IP and the network changed, which IPs came back after another one, the carriers seen and latency p50/p95.

## rotate: how quickly a change-IP URL works

Many mobile providers give each session a URL that changes its IP. QuanticData gives one for sticky sessions of Residential Basic and Datacenter traffic plans ([rotation links](https://quanticdata.io/docs/)); on its Mobile plans a new IP comes from a new sticky string instead (see below).

```bash
mobile-proxy-tester rotate PROXY --rotate-url "https://provider.example/change-ip?token=..." --attempts 5
```

| Option | Meaning |
|---|---|
| `--rotate-url`, `--rotate-method GET\|POST` | the URL to call (directly, not through the proxy) |
| `--attempts` | rotations to run (5) |
| `--poll` | time between echo checks after a rotation (2s) |
| `--max-wait` | give up on one rotation after this long (60s) |
| `--cooldown` | wait between rotations (10s); respect your provider's limit |

It prints, per rotation, the IP before and after and the time until the echo showed the new IP, then how many rotations worked and how many landed on an IP already seen in the run. Exit code 1 when a rotation gave no new IP.

## pool: what a list of sessions really gives you

```bash
mobile-proxy-tester pool -f sessions.txt --lookup ipinfo
```

One request through each proxy, then: distinct exit IPs, distinct subnets (/24 for IPv4, /48 for IPv6), exit IPs shared by several sessions, and the count per carrier and per country.

## Output

| Flag | Output |
|---|---|
| (default) | live lines on stderr, summary on stdout |
| `--json` | one JSON document with the summary, every sample and the failed checks |
| `--csv FILE` | one row per sample: `index, utc, elapsed_s, ok, ip, country, region, city, asn, operator, latency_ms, event, error` |
| `-q` | summary only |

Passwords are masked everywhere unless `--show-credentials` is given. Exit codes: 0 done, 1 a check failed, 2 bad usage, 3 runtime or API error.

## QuanticData mobile plans

With a QuanticData Mobile plan the tester can create the proxy strings itself through the documented, free [`POST /public/proxies/generate`](https://quanticdata.io/docs/) endpoint, and list the carriers you can target from the Mobile location tree (`GET /public/generator/mobile/targeting-options`). The strings are used exactly as returned. Traffic is billed by the plan as usual.

```bash
export QUANTICDATA_API_KEY=qd_live_...                     # dashboard, API keys

mobile-proxy-tester carriers --country us                   # carrier codes per region and city

# Does a 30-minute sticky session on one carrier keep its IP and carrier?
mobile-proxy-tester watch --qd-order ORDER_ID --qd-country us --qd-state ohio --qd-city cincinnati \
  --qd-isp t_mobile_usa --qd-rotation sticky --qd-session-time 30 --interval 2m --duration 40m --min-hold 30m

# A super sticky session keeps its IP for as long as the device stays online: how long is that here?
mobile-proxy-tester watch --qd-order ORDER_ID --qd-country us --qd-super-sticky --interval 5m --duration 6h

# Spread of 20 sticky sessions in one country
mobile-proxy-tester pool --qd-order ORDER_ID --qd-country de --qd-rotation sticky --qd-quantity 20
```

`--qd-*` options map to the fields of `generate`: `--qd-country`, `--qd-state`, `--qd-city`, `--qd-isp` (carrier code), `--qd-rotation` (`rotating`, `sticky`), `--qd-session-time` (minutes), `--qd-super-sticky`, `--qd-protocol` (`http`, `socks5`), `--qd-gateway`, and `--qd-quantity` for `pool`. Which ones apply depends on the plan type, as the [API reference](https://quanticdata.io/docs/) lists. With an API key set, the network lookup uses QuanticData's free [`ip-info`](https://quanticdata.io/docs/) endpoint, which shares a daily cap of new lookups. Plans: [quanticdata.io/pricing](https://quanticdata.io/pricing/).

## Use it as a library

```python
from mobile_proxy_tester import Sampler, ipinfo_lookup, parse_proxy, summarize_watch, watch

sampler = Sampler(parse_proxy("http://user:pass@gw.example.net:8080"), lookup=ipinfo_lookup())
samples = list(watch(sampler, interval=60, duration=1800))
summary = summarize_watch(samples)
print(summary.distinct_ips, summary.longest_hold, summary.networks)
```

See [`examples/`](examples/).

## Limits, honestly

- The carrier comes from the network lookup's ASN data. It shows which network an IP belongs to; it does not prove the IP is a phone on a cellular link. Databases also lag behind address reassignments.
- Location comes from the echo service's database (Cloudflare for the default echo). Different databases can disagree, especially for mobile IPs.
- Many mobile users share one public IP behind carrier NAT, so an IP "seen again" is normal on mobile and does not by itself mean a reused session.
- Every sample is a real request through your proxy and uses a little of its traffic. Long watches with short intervals add up.
- A watch only observes: it cannot tell whether an IP changed because the session ended, the device went offline or the provider rotated it.

## Development

```bash
uv venv && uv pip install -e ".[socks]" --group dev
ruff check . && ruff format --check . && mypy && pytest
```

Tests run against an in-process fake mobile proxy, rotation URL and lookup service on 127.0.0.1; they never use the network. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
