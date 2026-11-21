"""Unit tests for loadgen.py and the suite's helpers (no Docker needed).

    .venv/bin/pytest traffic-generator/test_loadgen.py -q
"""

import json
import random
import socket
import struct
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import loadgen  # noqa: E402
from harness import parse_prometheus, metric_sum  # noqa: E402
from loadgen import LoadRun, Phase, Sample, percentile, summarize  # noqa: E402
from run_load_suite import Scenario, junit, parse_set  # noqa: E402


class Stub(BaseHTTPRequestHandler):
    """Mimics HAProxy + simulators: routing headers, configurable 5xx and resets."""
    protocol_version = "HTTP/1.1"
    error_rate = 0.0
    drop_rate = 0.0

    def do_GET(self):
        r = random.random()
        if r < Stub.drop_rate:
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            self.connection.close()
            self.close_connection = True
            return
        pool = "apigee_opdk_pool" if random.random() < 0.5 else "apigee_hybrid_pool"
        status = 503 if r < Stub.drop_rate + Stub.error_rate else 200
        body = b"{}"
        self.send_response(status)
        self.send_header("X-Gateway-Pool", pool)
        self.send_header("X-Gateway-Instance", pool.split("_")[1] + "-1")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def stub():
    Stub.error_rate = Stub.drop_rate = 0.0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield srv.server_address[1]
    srv.shutdown()


def test_profiles_shapes():
    assert [p.concurrency for p in loadgen.sustained(10, 4)] == [4]
    r = loadgen.ramp(8, 1, 8, steps=8)
    assert [p.concurrency for p in r] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert sum(p.duration for p in r) == pytest.approx(8)
    s = loadgen.spike(2, 20, 1, 2, 3)
    assert [(p.concurrency, p.duration) for p in s] == [(2, 1), (20, 2), (2, 3)]
    assert [p.concurrency for p in loadgen.steps((1, 5), 2)] == [1, 5]


def test_percentile_and_summary():
    assert percentile([], 0.95) is None
    assert percentile(list(range(1, 101)), 0.95) == 95
    samples = [Sample(0.1, 10, 200, "APIGEE_OPDK", "o1", 0), Sample(0.2, 20, 503, "APIGEE_HYBRID", "h1", 0),
               Sample(0.3, 5000, 0, "none", "none", 0), Sample(0.4, 30, 404, "APIGEE_OPDK", "o1", 0)]
    s = summarize(samples, 2.0)
    assert s["requests"] == 4 and s["errors"] == 2 and s["no_response"] == 1     # 404 is not an error
    assert s["error_rate_percent"] == 50 and s["throughput_rps"] == 2
    assert s["latency_ms"]["max"] == 30            # no-response samples excluded from latency
    assert s["per_instance"] == {"h1": 1, "none": 1, "o1": 2}


def test_run_attributes_pools_and_respects_phases(stub):
    run = LoadRun([Phase("a", 0.6, 2), Phase("b", 0.6, 4)], port=stub, path="/x").start()
    run.mark("midpoint")
    res = run.wait()
    assert res.samples and all(s.t < res.duration for s in res.samples)
    summ = res.summary()
    assert summ["overall"]["errors"] == 0
    assert set(summ["pools"]) >= {"APIGEE_OPDK", "APIGEE_HYBRID"}
    assert 30 < summ["share_percent"]["APIGEE_OPDK"] < 70
    phases = res.phase_summaries()
    assert [p["concurrency"] for p in phases] == [2, 4] and all(p["requests"] > 0 for p in phases)
    assert {s.phase for s in res.samples} == {0, 1}
    assert res.markers[0]["event"] == "midpoint"
    tl = res.timeline(0.5)
    assert sum(r["requests"] for r in tl) == summ["overall"]["requests"]
    assert "APIGEE_OPDK_requests" in tl[0]
    json.dumps(res.to_json())


def test_errors_and_resets_are_recorded(stub, tmp_path):
    Stub.error_rate, Stub.drop_rate = 0.2, 0.2
    res = LoadRun([Phase("x", 1.0, 3)], port=stub).run()
    s = res.summary()["overall"]
    assert s["no_response"] > 0 and s["status_codes"].get("503", 0) > 0
    assert 20 < s["error_rate_percent"] < 60
    out = tmp_path / "t.csv"
    res.write_timeline_csv(str(out))
    assert out.read_text().startswith("t,requests,errors")


def test_paced_rate_is_respected(stub):
    res = LoadRun([Phase("paced", 2.0, 4, rps=50)], port=stub).run()
    assert 70 <= res.summary()["overall"]["requests"] <= 115


def test_unreachable_target_records_no_response():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]              # closed port
    res = LoadRun([Phase("x", 0.3, 1)], port=port).run()
    o = res.summary()["overall"]
    assert o["requests"] > 0 and o["no_response"] == o["requests"]


def test_prometheus_parser():
    text = ('# HELP tm_x x\n# TYPE tm_x counter\ntm_x{platform="A",code_class="5xx"} 3\n'
            'tm_x{platform="A",code_class="2xx"} 7\ntm_x{platform="B",code_class="2xx"} 1\ntm_y 2.5\n'
            'tm_h_bucket{le="+Inf"} 4\n')
    s = parse_prometheus(text)
    assert metric_sum(s, "tm_x", platform="A") == 10 and metric_sum(s, "tm_y") == 2.5
    assert metric_sum(s, "tm_h_bucket", le="+Inf") == 4


def test_scenario_checks_and_junit():
    sc = Scenario("demo", "d", {})
    assert sc.check("a", 1, "<=", 2) and not sc.check("b", 5, "between", [1, 4]) and not sc.check("c", None, ">=", 0)
    assert sc.verdict == "FAIL"
    xml = junit([sc])
    assert 'failures="1"' in xml and "b: 5 not between [1, 4]" in xml
    ok = Scenario("ok", "", {})
    ok.check("x", True, "==", True)
    assert ok.verdict == "PASS"
    err = Scenario("err", "", {})
    err.error = "boom"
    assert err.verdict == "ERROR" and 'errors="1"' in junit([err])


def test_parse_set_overrides():
    cfg = {"sustained": {"duration_seconds": 60}}
    parse_set(["sustained.duration_seconds=5", "sustained.name=abc"], cfg)
    assert cfg["sustained"] == {"duration_seconds": 5, "name": "abc"}
    with pytest.raises(SystemExit):
        parse_set(["unknown.x=1"], cfg)


def test_default_thresholds_cover_every_scenario():
    from run_load_suite import SCENARIOS
    cfg = json.loads((Path(__file__).parent / "load_thresholds.json").read_text())
    assert set(SCENARIOS) == {k for k in cfg if not k.startswith("_")}
