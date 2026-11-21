#!/usr/bin/env python3
"""
Weighted routing test for the HAProxy Layer 7 load balancer.

Sends N HTTP requests to the load balancer, each on a *fresh TCP connection*,
identifies which backend served each request (from the JSON "backend" field),
and compares the observed split with the expected weights.

HAProxy makes every routing decision. This script never chooses a backend.

Examples
--------
  # Expected weights given explicitly
  python traffic-generator/test_distribution.py --a 80 --b 20 --requests 10000

  # Named scenario (90:10, 80:20, 75:25, 50:50, 100:0, 0:100)
  python traffic-generator/test_distribution.py --scenario canary-1

  # Apply the weights through the Runtime API first, then test
  python traffic-generator/test_distribution.py --a 75 --b 25 --apply

  # No weights given -> expected weights are read live from HAProxy
  python traffic-generator/test_distribution.py

Exit codes: 0 PASS, 1 FAIL, 2 invalid input / cannot determine expectation.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Tuple
from urllib.parse import urlsplit

# Shared Runtime API client lives in ../runtime
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "runtime"))
from haproxy_runtime import (  # noqa: E402
    DEFAULT_BACKEND,
    DEFAULT_CONTAINER,
    DEFAULT_SOCKET,
    SCENARIOS,
    HAProxyRuntime,
    RuntimeAPIError,
    WeightValidationError,
    docker_exec_transport,
    percentages,
    validate_weights,
)

BACKEND_A = "Backend A"
BACKEND_B = "Backend B"
# Milestone 3: Backend A = Apigee OPDK pool, Backend B = Apigee Hybrid pool
# (simulators report "platform"; older backends only report "backend").
PLATFORM_LABEL = {"APIGEE_OPDK": BACKEND_A, "APIGEE_HYBRID": BACKEND_B}
DISPLAY = {BACKEND_A: "Backend A (Apigee OPDK pool)", BACKEND_B: "Backend B (Apigee Hybrid pool)"}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def one_request(host: str, port: int, path: str, timeout: float) -> str:
    """Return only the pool/backend label (see request_detail)."""
    return request_detail(host, port, path, timeout)[0]


def request_detail(host: str, port: int, path: str, timeout: float) -> Tuple[str, Optional[str]]:
    """Send one GET on a brand-new TCP connection; return (backend label, instance).

    A new connection + 'Connection: close' per request guarantees every
    request is an independent load-balancing decision by HAProxy (no
    keep-alive / connection reuse effects).
    """
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Connection": "close"})
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            return f"HTTP {resp.status}", None
        try:
            data = json.loads(body)
        except ValueError:
            return "unparseable", None
        label = PLATFORM_LABEL.get(data.get("platform")) or data.get("backend") or "unidentified"
        return label, data.get("instance")
    except Exception as exc:  # network errors are counted, not fatal
        return f"error: {type(exc).__name__}", None
    finally:
        conn.close()


def run_traffic(url: str, total: int, concurrency: int, timeout: float) -> Tuple[Counter, float]:
    counts, _, elapsed = run_traffic_detailed(url, total, concurrency, timeout)
    return counts, elapsed


def run_traffic_detailed(url: str, total: int, concurrency: int,
                         timeout: float) -> Tuple[Counter, Counter, float]:
    """Returns (requests per backend label, requests per instance, seconds)."""
    parts = urlsplit(url)
    host = parts.hostname or "localhost"
    port = parts.port or 80
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(lambda _: request_detail(host, port, path, timeout), range(total)))
    counts = Counter(label for label, _ in results)
    instances = Counter(f"{label} / {inst}" for label, inst in results if inst)
    return counts, instances, time.monotonic() - start


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------
def within(expected_pct: float, observed_pct: float, tolerance: float) -> bool:
    # 0% and 100% are hard guarantees in HAProxy (weight 0 = no new traffic),
    # so they are checked exactly rather than with a tolerance.
    if expected_pct in (0.0, 100.0):
        return observed_pct == expected_pct
    return abs(observed_pct - expected_pct) <= tolerance


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Measure HAProxy weighted traffic distribution.")
    p.add_argument("--a", metavar="WEIGHT", help="expected weight for Backend A")
    p.add_argument("--b", metavar="WEIGHT", help="expected weight for Backend B")
    p.add_argument("--scenario", choices=sorted(SCENARIOS), help="named weight configuration")
    p.add_argument("--apply", action="store_true",
                   help="set these weights in HAProxy via the Runtime API before testing")
    p.add_argument("--requests", type=int, default=1000, help="number of requests (default 1000)")
    p.add_argument("--tolerance", type=float, default=2.0,
                   help="allowed deviation in percentage points (default 2.0)")
    p.add_argument("--concurrency", type=int, default=10, help="parallel clients (default 10)")
    p.add_argument("--timeout", type=float, default=5.0, help="per-request timeout seconds")
    p.add_argument("--url", default="http://localhost:8080/", help="load balancer URL")
    p.add_argument("--container", default=DEFAULT_CONTAINER)
    p.add_argument("--socket", default=DEFAULT_SOCKET)
    p.add_argument("--backend", default=DEFAULT_BACKEND)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.requests <= 0:
        parser.error("--requests must be a positive integer")
    if args.concurrency <= 0:
        parser.error("--concurrency must be a positive integer")
    if args.tolerance < 0:
        parser.error("--tolerance must be >= 0")
    if args.scenario and (args.a is not None or args.b is not None):
        parser.error("use either --scenario or --a/--b, not both")
    if (args.a is None) != (args.b is None):
        parser.error("--a and --b must be given together")

    runtime: Optional[HAProxyRuntime] = None

    def rt() -> HAProxyRuntime:
        nonlocal runtime
        if runtime is None:
            runtime = HAProxyRuntime(
                transport=docker_exec_transport(args.container, args.socket),
                backend=args.backend,
            )
        return runtime

    try:
        if args.scenario:
            exp_a, exp_b = SCENARIOS[args.scenario]
            source = f"scenario '{args.scenario}'"
        elif args.a is not None:
            exp_a, exp_b = validate_weights(args.a, args.b)
            source = "command line"
        else:
            if args.apply:
                parser.error("--apply needs --a/--b or --scenario")
            exp_a, exp_b = rt().get_weights()
            source = "live HAProxy weights"
        exp_a, exp_b = validate_weights(exp_a, exp_b)

        if args.apply:
            res = rt().set_weights(exp_a, exp_b)
            prev = res.previous
            print(f"Applied weights via Runtime API: "
                  f"{prev[rt().server_a]}:{prev[rt().server_b]} -> {exp_a}:{exp_b}\n")
        elif source != "live HAProxy weights":
            # Warn (don't fail) if HAProxy is configured differently.
            try:
                live = rt().get_weights()
                if live != (exp_a, exp_b):
                    print(f"WARNING: HAProxy is currently configured {live[0]}:{live[1]}, "
                          f"but expecting {exp_a}:{exp_b}. Use --apply to set it.\n")
            except RuntimeAPIError as exc:
                print(f"NOTE: could not read live weights ({exc}); continuing.\n")
    except WeightValidationError as exc:
        print(f"REJECTED: {exc}", file=sys.stderr)
        return 2
    except RuntimeAPIError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    pct_a, pct_b = percentages(exp_a, exp_b)
    counts, per_instance, elapsed = run_traffic_detailed(args.url, args.requests, args.concurrency, args.timeout)

    n_a = counts.get(BACKEND_A, 0)
    n_b = counts.get(BACKEND_B, 0)
    failures = {k: v for k, v in counts.items() if k not in (BACKEND_A, BACKEND_B)}
    obs_a = 100.0 * n_a / args.requests
    obs_b = 100.0 * n_b / args.requests

    ok_a = within(pct_a, obs_a, args.tolerance)
    ok_b = within(pct_b, obs_b, args.tolerance)
    passed = ok_a and ok_b and not failures

    print("WEIGHTED ROUTING TEST")
    print()
    print(f"Target:         {args.url}")
    print(f"Weights:        A={exp_a} B={exp_b} (from {source})")
    print(f"Total requests: {args.requests}  (concurrency {args.concurrency}, "
          f"new TCP connection per request, {elapsed:.1f}s, "
          f"{args.requests / elapsed:.0f} req/s)")
    print(f"Tolerance:      +/-{args.tolerance:g} percentage points "
          f"(0% / 100% checked exactly)")
    for label, exp, obs, n, ok in (
        ("Backend A", pct_a, obs_a, n_a, ok_a),
        ("Backend B", pct_b, obs_b, n_b, ok_b),
    ):
        print()
        print(f"{DISPLAY.get(label, label)}:")
        print(f"Expected: {exp:g}%")
        print(f"Observed: {obs:.2f}%")
        print(f"Requests: {n}")
        print(f"Deviation: {obs - exp:+.2f} pp  [{'ok' if ok else 'OUT OF TOLERANCE'}]")
    if per_instance:
        print()
        print("Per instance (instance-level balancing inside each pool):")
        for k, v in sorted(per_instance.items()):
            print(f"  {k:<40} {v:>7}  ({100.0 * v / args.requests:.2f}%)")
    if failures:
        print()
        print("Failed / unidentified requests:")
        for k, v in sorted(failures.items()):
            print(f"  {k}: {v}")
    print()
    print(f"Distribution test: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
