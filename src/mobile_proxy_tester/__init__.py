"""Measure how a mobile proxy's exit IP and carrier change over time."""

from ._version import __version__
from .analysis import segments, summarize_pool, summarize_rotation, summarize_watch
from .lookup import (
    Exit,
    HTTPLookup,
    Network,
    NoLookup,
    ipinfo_lookup,
    parse_echo,
    parse_network,
    quanticdata_lookup,
)
from .proxy import Proxy, parse_proxy
from .sampler import RotationTrigger, Sample, Sampler, rotate_and_wait, watch

__all__ = [
    "Exit",
    "HTTPLookup",
    "Network",
    "NoLookup",
    "Proxy",
    "RotationTrigger",
    "Sample",
    "Sampler",
    "__version__",
    "ipinfo_lookup",
    "parse_echo",
    "parse_network",
    "parse_proxy",
    "quanticdata_lookup",
    "rotate_and_wait",
    "segments",
    "summarize_pool",
    "summarize_rotation",
    "summarize_watch",
    "watch",
]
