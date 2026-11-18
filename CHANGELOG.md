# Changelog

All notable changes are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-18

### Added

- `watch`: samples a proxy on an interval and reports how long each exit IP held (lower and upper bound), IP and network changes, IPs seen again, carriers, countries and latency; `--min-hold`, `--expect-country` and `--expect-rotation` checks.
- `rotate`: calls a change-IP URL, then times how long until the exit IP changes; counts rotations that land on an IP already seen.
- `pool`: one request per proxy of a list; distinct IPs, subnets, shared IPs, carriers and countries.
- `carriers`: the QuanticData Mobile location tree with carrier codes.
- QuanticData proxy strings from `generate` (`--qd-*`), network lookups through QuanticData `ip-info`, ipinfo.io or any JSON service.
- Table, JSON and CSV output; masked passwords; a Python API.

[Unreleased]: https://github.com/quanticdata-io/mobile-proxy-tester/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/quanticdata-io/mobile-proxy-tester/releases/tag/v0.1.0
