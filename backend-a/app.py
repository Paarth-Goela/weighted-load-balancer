"""
Local SIMULATOR of an Apigee gateway runtime endpoint.

This is NOT Apigee. It only returns JSON that identifies which simulated
gateway platform/instance served the request, so that HAProxy traffic
distribution can be tested locally. It does not implement API proxies,
policies, authentication, analytics or any other Apigee behaviour.

Configuration (environment variables):
  BACKEND_LABEL   legacy label used by Milestone 1/2 tooling ("Backend A")
  BACKEND_VERSION response "version" field
  SIM_PLATFORM    APIGEE_OPDK | APIGEE_HYBRID
  SIM_INSTANCE    simulated instance name, e.g. opdk-router-1
  SIM_ROLE        human-readable role, e.g. "OPDK Router (simulated)"
  SIM_HEALTH_PATH readiness path (default /healthz)

Simulation-only fault injection (lets tests show that a TCP connect alone
does not prove readiness, and that passing health checks does not prove the
gateway serves API traffic correctly):
  GET  /__sim/health                     -> {"ready": true|false}
  POST /__sim/health  {"ready": false}   -> readiness path returns 503,
                                            normal API paths keep answering 200
  GET  /__sim/faults                     -> {"error_rate": 0.0, "latency_ms": 0}
  POST /__sim/faults  {"error_rate": 0.3, "latency_ms": 250}
                                         -> normal API paths fail with HTTP 500
                                            for ~30% of requests and are delayed
                                            250 ms. Readiness is NOT affected.
"""

import json
import os
import random
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8000"))

BACKEND_LABEL = os.environ.get("BACKEND_LABEL", "Backend A")
BACKEND_VERSION = os.environ.get("BACKEND_VERSION", "1.0")
SIM_PLATFORM = os.environ.get("SIM_PLATFORM", "APIGEE_OPDK")
SIM_INSTANCE = os.environ.get("SIM_INSTANCE", socket.gethostname())
SIM_ROLE = os.environ.get("SIM_ROLE", "OPDK Router (simulated)")
SIM_HEALTH_PATH = os.environ.get("SIM_HEALTH_PATH", "/healthz")

_state_lock = threading.Lock()
_ready = True
_faults = {"error_rate": 0.0, "latency_ms": 0}


class SimulatorHandler(BaseHTTPRequestHandler):
    def _send(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Simulated-Gateway", SIM_PLATFORM)
        self.end_headers()
        self.wfile.write(body)

    def _identity(self):
        return {
            "backend": BACKEND_LABEL,
            "version": BACKEND_VERSION,
            "platform": SIM_PLATFORM,
            "instance": SIM_INSTANCE,
            "role": SIM_ROLE,
            "simulated": True,
            "hostname": socket.gethostname(),
        }

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        with _state_lock:
            ready = _ready
            faults = dict(_faults)
        if path == "/__sim/faults":
            self._send(200, {**faults, **self._identity()})
        elif path == "/__sim/health":
            self._send(200, {"ready": ready, **self._identity()})
        elif path == SIM_HEALTH_PATH:
            self._send(200 if ready else 503, {"ready": ready, "path": path, **self._identity()})
        else:
            if faults["latency_ms"]:
                time.sleep(faults["latency_ms"] / 1000.0)
            if faults["error_rate"] and random.random() < faults["error_rate"]:
                self._send(500, {"error": "simulated gateway error", **self._identity()})
                return
            self._send(200, {
                "message": "Request successfully processed",
                **self._identity(),
                "path": self.path,
            })

    def do_POST(self):
        global _ready
        path = self.path.split("?", 1)[0]
        if path == "/__sim/faults":
            self._set_faults()
            return
        if path != "/__sim/health":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
            ready = data["ready"]
            if not isinstance(ready, bool):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            self._send(400, {"error": 'body must be {"ready": true|false}'})
            return
        with _state_lock:
            _ready = ready
        print(f"[{SIM_INSTANCE}] simulated readiness set to {ready}", flush=True)
        self._send(200, {"ready": ready, **self._identity()})

    def _set_faults(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
            rate = float(data.get("error_rate", 0.0))
            latency = int(data.get("latency_ms", 0))
            if not (0.0 <= rate <= 1.0 and 0 <= latency <= 10000):
                raise ValueError
        except (ValueError, TypeError):
            self._send(400, {"error": 'body must be {"error_rate": 0..1, "latency_ms": 0..10000}'})
            return
        with _state_lock:
            _faults.update(error_rate=rate, latency_ms=latency)
        print(f"[{SIM_INSTANCE}] simulated faults set to {_faults}", flush=True)
        self._send(200, {**_faults, **self._identity()})

    def log_message(self, format, *args):
        print(f"[{SIM_INSTANCE}] {self.command} {self.path} {format % args}", flush=True)


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), SimulatorHandler)
    print(
        f"{SIM_ROLE} '{SIM_INSTANCE}' ({SIM_PLATFORM}, SIMULATOR - not Apigee) "
        f"listening on {HOST}:{PORT}",
        flush=True,
    )
    server.serve_forever()
