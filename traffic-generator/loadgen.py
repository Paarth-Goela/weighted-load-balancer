"""
Load generator with configurable profiles (standard library only, Python 3.9+).

A run is a list of phases. Each phase has a duration, a concurrency (number of
active client threads) and an optional total request rate:

    rps = None  -> closed loop: every active thread sends its next request as
                   soon as the previous one completes (max throughput at that
                   concurrency).
    rps = N     -> paced: active threads share a target of N requests/second
                   (open-loop-ish; useful to compare two targets at the SAME
                   offered load).

Built-in profiles (see PROFILES):  sustained, ramp, spike, steps.

Every response is attributed to a pool and instance from the X-Gateway-Pool /
X-Gateway-Instance headers HAProxy adds when EXPOSE_ROUTING_HEADERS=1 (local
simulation only). This includes errors HAProxy generates itself (502/503/504).
A request that gets no HTTP response at all (connection refused/reset,
timeout) is recorded with status 0 and pool "none".

Error = HTTP 5xx or no response. 4xx counts as a response, not an error
(same definition as the traffic-manager's metrics).

    python3 traffic-generator/loadgen.py --profile sustained --duration 30 --concurrency 8
    python3 traffic-generator/loadgen.py --profile ramp --duration 40 --start 1 --end 32 --json out.json
"""

from __future__ import annotations

import argparse
import csv
import http.client
import json
import math
import socket
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

POOL_NAMES = {"apigee_opdk_pool": "APIGEE_OPDK", "apigee_hybrid_pool": "APIGEE_HYBRID"}


# ------------------------------------------------------------------ profiles
@dataclass
class Phase:
    name: str
    duration: float
    concurrency: int
    rps: Optional[float] = None


def sustained(duration: float = 60, concurrency: int = 8, rps: Optional[float] = None) -> List[Phase]:
    return [Phase("sustained", duration, concurrency, rps)]


def ramp(duration: float = 60, start: int = 1, end: int = 32, steps: int = 0) -> List[Phase]:
    """Concurrency increases linearly from start to end (one step per ~second by default)."""
    steps = steps or max(2, int(duration))
    return [Phase(f"ramp-{i}", duration / steps, max(1, round(start + (end - start) * i / (steps - 1))))
            for i in range(steps)]


def spike(base: int = 4, peak: int = 48, before: float = 10, hold: float = 10, after: float = 10) -> List[Phase]:
    return [Phase("before-spike", before, base), Phase("spike", hold, peak), Phase("after-spike", after, base)]


def steps(levels: Tuple[int, ...] = (2, 8, 16, 32), each: float = 10) -> List[Phase]:
    return [Phase(f"level-{c}", each, c) for c in levels]


PROFILES = {"sustained": sustained, "ramp": ramp, "spike": spike, "steps": steps}


# ---------------------------------------------------------------- recording
@dataclass
class Sample:
    t: float            # seconds since run start (request start)
    ms: float           # client-observed latency
    status: int         # HTTP status; 0 = no response
    pool: str           # APIGEE_OPDK | APIGEE_HYBRID | none
    instance: str       # instance name | none
    phase: int


def is_error(status: int) -> bool:
    return status == 0 or status >= 500


def percentile(sorted_values: List[float], q: float) -> Optional[float]:
    """Nearest-rank percentile of an already-sorted list."""
    if not sorted_values:
        return None
    return sorted_values[max(0, math.ceil(q * len(sorted_values)) - 1)]


def summarize(samples: List[Sample], seconds: float) -> dict:
    n = len(samples)
    errors = sum(1 for s in samples if is_error(s.status))
    lat = sorted(s.ms for s in samples if s.status)
    by_status: Dict[str, int] = {}
    by_instance: Dict[str, int] = {}
    for s in samples:
        by_status[str(s.status)] = by_status.get(str(s.status), 0) + 1
        by_instance[s.instance] = by_instance.get(s.instance, 0) + 1
    r = lambda v: None if v is None else round(v, 2)  # noqa: E731
    return {
        "requests": n,
        "errors": errors,
        "no_response": by_status.get("0", 0),
        "error_rate_percent": round(100.0 * errors / n, 4) if n else None,
        "throughput_rps": round(n / seconds, 2) if seconds > 0 else None,
        "latency_ms": {"p50": r(percentile(lat, .50)), "p90": r(percentile(lat, .90)),
                       "p95": r(percentile(lat, .95)), "p99": r(percentile(lat, .99)),
                       "max": r(lat[-1] if lat else None), "mean": r(sum(lat) / len(lat) if lat else None)},
        "status_codes": dict(sorted(by_status.items())),
        "per_instance": dict(sorted(by_instance.items())),
    }


@dataclass
class RunResult:
    target: str
    phases: List[Phase]
    started_at: float                       # unix time
    duration: float
    samples: List[Sample]
    markers: List[dict] = field(default_factory=list)

    def window(self, start: float = 0.0, end: float = float("inf"), pool: Optional[str] = None) -> List[Sample]:
        return [s for s in self.samples if start <= s.t < end and (pool is None or s.pool == pool)]

    def summary(self, start: float = 0.0, end: Optional[float] = None) -> dict:
        end = self.duration if end is None else end
        span = max(end - start, 1e-9)
        win = self.window(start, end)
        pools = sorted({s.pool for s in win} | set(POOL_NAMES.values()))
        out = {"overall": summarize(win, span), "pools": {}}
        for p in pools:
            sub = [s for s in win if s.pool == p]
            if sub or p in POOL_NAMES.values():
                out["pools"][p] = summarize(sub, span)
        served = sum(v["requests"] for k, v in out["pools"].items() if k in POOL_NAMES.values())
        out["share_percent"] = {p: round(100.0 * out["pools"][p]["requests"] / served, 3) if served else None
                                for p in POOL_NAMES.values()}
        return out

    def phase_summaries(self) -> List[dict]:
        out, t = [], 0.0
        for i, ph in enumerate(self.phases):
            out.append({"phase": ph.name, "start": round(t, 2), "end": round(t + ph.duration, 2),
                        "concurrency": ph.concurrency, "rps_target": ph.rps,
                        **self.summary(t, t + ph.duration)["overall"]})
            t += ph.duration
        return out

    def timeline(self, bucket: float = 1.0) -> List[dict]:
        rows = []
        nb = int(math.ceil(self.duration / bucket))
        buckets: List[List[Sample]] = [[] for _ in range(nb)]
        for s in self.samples:
            i = int(s.t // bucket)
            if 0 <= i < nb:
                buckets[i].append(s)
        instances = sorted({s.instance for s in self.samples if s.instance != "none"})
        for i, b in enumerate(buckets):
            lat = sorted(s.ms for s in b if s.status)
            row = {"t": round(i * bucket, 3), "requests": len(b),
                   "errors": sum(1 for s in b if is_error(s.status)),
                   "no_response": sum(1 for s in b if s.status == 0),
                   "p95_ms": None if not lat else round(percentile(lat, .95), 2)}
            for p in POOL_NAMES.values():
                row[f"{p}_requests"] = sum(1 for s in b if s.pool == p)
                row[f"{p}_errors"] = sum(1 for s in b if s.pool == p and is_error(s.status))
            for inst in instances:
                row[f"inst_{inst}"] = sum(1 for s in b if s.instance == inst)
            rows.append(row)
        return rows

    def write_timeline_csv(self, path: str, bucket: float = 1.0) -> None:
        rows = self.timeline(bucket)
        if not rows:
            return
        cols = list(rows[0].keys())
        for r in rows:
            cols += [k for k in r if k not in cols]
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)

    def to_json(self) -> dict:
        return {"target": self.target, "started_at": self.started_at, "duration_seconds": round(self.duration, 3),
                "phases": [asdict(p) for p in self.phases], "markers": self.markers,
                "summary": self.summary(), "per_phase": self.phase_summaries()}


# ------------------------------------------------------------------- engine
class LoadRun:
    """Runs phases in the background; `mark()` records scenario events on the same clock."""

    def __init__(self, phases: List[Phase], host: str = "127.0.0.1", port: int = 8080, path: str = "/loadtest",
                 timeout: float = 5.0) -> None:
        if not phases:
            raise ValueError("at least one phase is required")
        self.phases = phases
        self.host, self.port, self.path, self.timeout = host, port, path, timeout
        self.max_workers = max(p.concurrency for p in phases)
        self._samples: List[List[Sample]] = [[] for _ in range(self.max_workers)]
        self._active = 0
        self._rps: Optional[float] = None
        self._phase = 0
        self._stop = threading.Event()
        self._t0 = 0.0
        self._wall0 = 0.0
        self.markers: List[dict] = []
        self._threads: List[threading.Thread] = []
        self._controller: Optional[threading.Thread] = None
        self.duration = 0.0

    # -- clock / markers
    def now(self) -> float:
        return time.monotonic() - self._t0

    def mark(self, label: str, **detail) -> float:
        t = round(self.now(), 3)
        self.markers.append({"t": t, "event": label, **detail})
        return t

    # -- request
    def _connect(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    def _worker(self, idx: int) -> None:
        out = self._samples[idx]
        conn: Optional[http.client.HTTPConnection] = None
        next_at = 0.0
        while not self._stop.is_set():
            if idx >= self._active:
                if conn is not None:
                    conn.close()
                    conn = None
                time.sleep(0.02)
                continue
            if self._rps:
                interval = self._active / self._rps
                now = time.monotonic()
                if next_at > now:
                    time.sleep(min(next_at - now, 0.2))
                    continue
                next_at = max(next_at + interval, now)
            if conn is None:
                conn = self._connect()
            t = time.monotonic()
            status, pool, inst = 0, "none", "none"
            try:
                conn.request("GET", self.path, headers={"Connection": "keep-alive"})
                resp = conn.getresponse()
                resp.read()
                status = resp.status
                pool = POOL_NAMES.get(resp.getheader("X-Gateway-Pool", ""), "none")
                inst = resp.getheader("X-Gateway-Instance") or "none"
                if inst == "<NOSRV>":
                    inst = "none"
                if resp.will_close:
                    conn.close()
                    conn = None
            except (OSError, http.client.HTTPException, socket.timeout):
                if conn is not None:
                    conn.close()
                conn = None
            done = time.monotonic()
            out.append(Sample(round(t - self._t0, 4), round((done - t) * 1000.0, 3), status, pool, inst,
                              self._phase))
            if status == 0:
                time.sleep(0.01)     # don't spin on a refused connection
        if conn is not None:
            conn.close()

    def _control(self) -> None:
        start = time.monotonic()
        t = start
        for i, ph in enumerate(self.phases):
            self._phase, self._rps, self._active = i, ph.rps, ph.concurrency
            t += ph.duration
            while not self._stop.is_set() and time.monotonic() < t:
                time.sleep(min(0.05, max(0.0, t - time.monotonic())))
            if self._stop.is_set():
                break
        self._stop.set()

    def start(self) -> "LoadRun":
        self._t0 = time.monotonic()
        self._wall0 = time.time()
        self._threads = [threading.Thread(target=self._worker, args=(i,), daemon=True)
                         for i in range(self.max_workers)]
        self._controller = threading.Thread(target=self._control, daemon=True)
        self._controller.start()
        for th in self._threads:
            th.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def wait(self) -> RunResult:
        assert self._controller is not None, "start() first"
        self._controller.join()
        self.duration = self.now()
        for th in self._threads:
            th.join(timeout=self.timeout + 2)
        samples = sorted((s for lst in self._samples for s in lst), key=lambda s: s.t)
        # Requests started after the planned end are excluded so phases are exact.
        samples = [s for s in samples if s.t < self.duration]
        return RunResult(f"http://{self.host}:{self.port}{self.path}", self.phases, self._wall0,
                         self.duration, samples, list(self.markers))

    def run(self) -> RunResult:
        return self.start().wait()


# ---------------------------------------------------------------------- CLI
def build_profile(args: argparse.Namespace) -> List[Phase]:
    if args.profile == "sustained":
        return sustained(args.duration, args.concurrency, args.rps)
    if args.profile == "ramp":
        return ramp(args.duration, args.start, args.end)
    if args.profile == "spike":
        third = args.duration / 3
        return spike(args.concurrency, args.peak, third, third, third)
    return steps(tuple(int(c) for c in args.levels.split(",")), args.duration / len(args.levels.split(",")))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--profile", choices=sorted(PROFILES), default="sustained")
    ap.add_argument("--duration", type=float, default=30, help="total seconds")
    ap.add_argument("--concurrency", type=int, default=8, help="sustained / spike base concurrency")
    ap.add_argument("--rps", type=float, default=None, help="sustained: total target rate (default: max)")
    ap.add_argument("--start", type=int, default=1, help="ramp: start concurrency")
    ap.add_argument("--end", type=int, default=32, help="ramp: end concurrency")
    ap.add_argument("--peak", type=int, default=48, help="spike: peak concurrency")
    ap.add_argument("--levels", default="2,8,16,32", help="steps: comma-separated concurrency levels")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--json", help="write the machine-readable report here")
    ap.add_argument("--csv", help="write the per-second timeline here")
    args = ap.parse_args(argv)

    result = LoadRun(build_profile(args), args.host, args.port).run()
    report = result.to_json()
    s = report["summary"]
    o = s["overall"]
    print(f"{o['requests']} requests in {report['duration_seconds']}s  "
          f"({o['throughput_rps']} req/s)  errors {o['errors']} ({o['error_rate_percent']}%)")
    print(f"latency ms  p50 {o['latency_ms']['p50']}  p95 {o['latency_ms']['p95']}  "
          f"p99 {o['latency_ms']['p99']}  max {o['latency_ms']['max']}")
    for pool, ps in s["pools"].items():
        print(f"  {pool:14} {ps['requests']:7} req  {s['share_percent'].get(pool, '-')}%  "
              f"errors {ps['errors']}  p95 {ps['latency_ms']['p95']} ms")
    if args.json:
        with open(args.json, "w") as f:
            json.dump(report, f, indent=2)
    if args.csv:
        result.write_timeline_csv(args.csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
