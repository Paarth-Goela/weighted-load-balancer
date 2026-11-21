"""
Local SIMULATOR of an Apigee gateway runtime endpoint.

This is NOT Apigee. It only returns JSON that identifies which simulated
gateway platform/instance served the request, so that HAProxy traffic
distribution can be tested locally. It does not implement API proxies,
policies, authentication, analytics or any other Apigee behaviour.

Configuration (environment variables):
  BACKEND_LABEL   legacy label used by Milestone 1/2 tooling ("Backend B")
  BACKEND_VERSION response "version" field
  SIM_PLATFORM    APIGEE_OPDK | APIGEE_HYBRID
  SIM_INSTANCE    simulated instance name, e.g. hybrid-ingress-1
  SIM_ROLE        human-readable role, e.g. "Hybrid Ingress (simulated)"
  SIM_HEALTH_PATH readiness path (default /healthz)

Simulation-only fault injection (lets tests show that a TCP connect alone
does not prove readiness, and that passing health checks does not prove the
gateway serves API traffic correctly):
  GET  /__sim/health                     -> {"ready": true|false}
  POST /__sim/health  {"ready": false}   -> readiness path returns 503,
                                            normal API paths keep answering 200
  GET  /__sim/faults                     -> current faults
  POST /__sim/faults  {"error_rate": 0.3, "latency_ms": 250,
                       "latency_jitter_ms": 100, "drop_rate": 0.05}
                                         -> normal API paths fail with HTTP 500
                                            for ~30% of requests, are delayed
                                            250 ms + uniform 0..100 ms, and ~5%
                                            of connections are reset (TCP RST)
                                            without any response - the closest
                                            application-level stand-in for packet
                                            loss / a crashing worker. Omitted
                                            fields are reset to 0. Readiness is
                                            NOT affected by any of these.
"""

import json
import os
import random
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8000"))

BACKEND_LABEL = os.environ.get("BACKEND_LABEL", "Backend B")
BACKEND_VERSION = os.environ.get("BACKEND_VERSION", "2.0")
SIM_PLATFORM = os.environ.get("SIM_PLATFORM", "APIGEE_HYBRID")
SIM_INSTANCE = os.environ.get("SIM_INSTANCE", socket.gethostname())
SIM_ROLE = os.environ.get("SIM_ROLE", "Hybrid Ingress (simulated)")
SIM_HEALTH_PATH = os.environ.get("SIM_HEALTH_PATH", "/healthz")

_state_lock = threading.Lock()
_ready = True
_faults = {"error_rate": 0.0, "latency_ms": 0, "latency_jitter_ms": 0, "drop_rate": 0.0}


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
            delay = faults["latency_ms"] + (random.uniform(0, faults["latency_jitter_ms"])
                                            if faults["latency_jitter_ms"] else 0)
            if delay:
                time.sleep(delay / 1000.0)
            if faults["drop_rate"] and random.random() < faults["drop_rate"]:
                self._reset_connection()
                return
            if faults["error_rate"] and random.random() < faults["error_rate"]:
                self._send(500, {"error": "simulated gateway error", **self._identity()})
                return
            self._send(200, {
                "message": "Request successfully processed",
                **self._identity(),
                "path": self.path,
            })

    def _reset_connection(self):
        """Abort with TCP RST and no HTTP response (SO_LINGER 0, then close)."""
        self.close_connection = True
        try:
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            self.connection.close()
        except OSError:
            pass

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
            jitter = int(data.get("latency_jitter_ms", 0))
            drop = float(data.get("drop_rate", 0.0))
            if not (0.0 <= rate <= 1.0 and 0 <= latency <= 10000 and 0 <= jitter <= 10000
                    and 0.0 <= drop <= 1.0):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            self._send(400, {"error": 'body must be {"error_rate": 0..1, "latency_ms": 0..10000, '
                                      '"latency_jitter_ms": 0..10000, "drop_rate": 0..1}'})
            return
        with _state_lock:
            _faults.update(error_rate=rate, latency_ms=latency, latency_jitter_ms=jitter, drop_rate=drop)
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
