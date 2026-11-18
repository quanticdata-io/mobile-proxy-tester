# Contributing

Thanks for helping improve mobile-proxy-tester.

## Setup

```bash
git clone https://github.com/quanticdata-io/mobile-proxy-tester
cd mobile-proxy-tester
uv venv && uv pip install -e ".[socks]" --group dev
```

Plain pip works too: `pip install -e ".[socks]" pytest pytest-cov mypy ruff`.

## Checks

CI runs these on Python 3.10 and 3.13, on Ubuntu and Windows:

```bash
ruff check .
ruff format --check .
mypy
pytest
```

## Ground rules

- Tests never use the network. `tests/conftest.py` has a fake mobile proxy that also plays the rotation URL and the lookup service, and a fake clock so hour-long watches run in milliseconds.
- New lookup or echo formats: add the answer shape to `parse_echo` / `parse_network` with a test using a synthetic answer (documentation addresses such as 203.0.113.0/24).
- Report what was observed, not a guess: when a value cannot be measured, leave it empty rather than estimate it.
- The CSV columns and JSON fields are a public contract: add new ones at the end, do not rename them.
- User-facing changes need a CHANGELOG entry under "Unreleased".
