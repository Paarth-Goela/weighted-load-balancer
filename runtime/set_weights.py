#!/usr/bin/env python3
"""
Change Backend A / Backend B traffic weights on a running HAProxy via its
Runtime API -- no restart, no reload.

Examples
--------
  python runtime/set_weights.py --show
  python runtime/set_weights.py --a 90 --b 10
  python runtime/set_weights.py --scenario canary-2        # 80:20
  python runtime/set_weights.py --list-scenarios

Exit codes: 0 success, 1 runtime/HAProxy failure, 2 invalid input.
"""

from __future__ import annotations

import argparse
import sys

from haproxy_runtime import (
    DEFAULT_BACKEND,
    DEFAULT_CONTAINER,
    DEFAULT_SERVER_A,
    DEFAULT_SERVER_B,
    DEFAULT_SOCKET,
    SCENARIOS,
    HAProxyRuntime,
    RuntimeAPIError,
    WeightValidationError,
    docker_exec_transport,
    percentages,
    validate_weights,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Set HAProxy server weights at runtime (no restart).",
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--show", action="store_true", help="show current weights and exit")
    mode.add_argument("--scenario", choices=sorted(SCENARIOS), help="apply a named configuration")
    mode.add_argument("--list-scenarios", action="store_true", help="list named configurations")
    mode.add_argument("--a", dest="a", metavar="WEIGHT", help="weight for Backend A (0-256)")
    p.add_argument("--b", dest="b", metavar="WEIGHT", help="weight for Backend B (0-256)")

    conn = p.add_argument_group("connection")
    conn.add_argument("--container", default=DEFAULT_CONTAINER)
    conn.add_argument("--socket", default=DEFAULT_SOCKET)
    conn.add_argument("--backend", default=DEFAULT_BACKEND)
    conn.add_argument("--server-a", default=DEFAULT_SERVER_A)
    conn.add_argument("--server-b", default=DEFAULT_SERVER_B)
    return p


def show(rt: HAProxyRuntime) -> None:
    states = rt.servers_state()
    a, b = rt.get_weights()
    pa, pb = percentages(a, b)
    print(f"Backend '{rt.backend}' (HAProxy Runtime API)")
    for name, pct in ((rt.server_a, pa), (rt.server_b, pb)):
        s = states[name]
        print(
            f"  {name:<10} weight={s.weight:<3} initial={s.initial_weight:<3} "
            f"status={s.status:<8} share={pct:6.2f}%"
        )
    for name in (rt.server_a, rt.server_b):
        if not states[name].is_up and states[name].weight > 0:
            print(f"  WARNING: {name} is {states[name].status}; it will not receive traffic.")


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_scenarios:
        for name, (a, b) in SCENARIOS.items():
            print(f"  {name:<10} A={a:<3} B={b}")
        return 0

    rt = HAProxyRuntime(
        transport=docker_exec_transport(args.container, args.socket),
        backend=args.backend,
        server_a=args.server_a,
        server_b=args.server_b,
    )

    try:
        if args.show:
            if args.b is not None:
                parser.error("--b cannot be combined with --show")
            show(rt)
            return 0

        if args.scenario:
            if args.b is not None:
                parser.error("--b cannot be combined with --scenario")
            raw_a, raw_b = SCENARIOS[args.scenario]
        else:
            if args.b is None:
                parser.error("--a requires --b (both weights must be given)")
            raw_a, raw_b = args.a, args.b

        # Validate locally first -> invalid input never reaches HAProxy.
        a, b = validate_weights(raw_a, raw_b)
        result = rt.set_weights(a, b)
    except WeightValidationError as exc:
        print(f"REJECTED: {exc}. No changes were made.", file=sys.stderr)
        return 2
    except RuntimeAPIError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    prev_a = result.previous[rt.server_a]
    prev_b = result.previous[rt.server_b]
    pa, pb = percentages(a, b)
    print(f"Weights updated without restart: {prev_a}:{prev_b} -> {a}:{b}")
    print(f"  {rt.server_a}: {pa:.2f}% of new requests")
    print(f"  {rt.server_b}: {pb:.2f}% of new requests")
    states = rt.servers_state()
    for name, w in ((rt.server_a, a), (rt.server_b, b)):
        if w > 0 and not states[name].is_up:
            print(f"  WARNING: {name} is {states[name].status}; it will not receive traffic.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
