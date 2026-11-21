"""Spec test 16: unauthorized management requests."""

import pytest

from app.main import create_app
from app.settings import Settings, SettingsError
from conftest import AUTH, TOKEN, seed

ENDPOINTS = [
    ("GET", "/api/v1/gateways", None), ("POST", "/api/v1/gateways", {"name": "x", "platform_type": "APIGEE_OPDK"}),
    ("GET", "/api/v1/gateways/1", None), ("GET", "/api/v1/pools", None),
    ("POST", "/api/v1/pools", {"name": "p", "platform_id": 1}), ("GET", "/api/v1/pools/1", None),
    ("GET", "/api/v1/instances", None), ("GET", "/api/v1/instances/1", None),
    ("POST", "/api/v1/instances", {"name": "i", "pool_id": 1, "host": "h", "port": 1}),
    ("PATCH", "/api/v1/instances/1", {"enabled": False}), ("DELETE", "/api/v1/instances/1", None),
    ("GET", "/api/v1/traffic/config", None), ("PUT", "/api/v1/traffic/config", {"opdk_weight": 0, "hybrid_weight": 100}),
    ("POST", "/api/v1/traffic/rollback", None), ("GET", "/api/v1/traffic/history", None),
    ("GET", "/api/v1/traffic/policies", None), ("GET", "/api/v1/health", None),
    ("GET", "/api/v1/health/gateways", None), ("GET", "/api/v1/health/instances", None),
    ("GET", "/api/v1/audit", None),
]


@pytest.mark.parametrize("method,path,body", ENDPOINTS)
def test_16_unauthorized_requests(env, client, method, path, body):
    seed(client)
    before = (env.fake.policy(), len(env.fake.commands))
    r = client.request(method, path, json=body)
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    r = client.request(method, path, json=body, headers={"Authorization": "Bearer wrong-token-000000000"})
    assert r.status_code == 403
    r = client.request(method, path, json=body, headers={"Authorization": f"Basic {TOKEN}"})
    assert r.status_code == 401
    after_cmds = env.fake.commands[before[1]:]
    assert env.fake.policy() == before[0]
    assert not [c for c in after_cmds if not c.startswith("show")]   # nothing mutated


def test_unauthenticated_invalid_body_gets_401_not_422(client):
    r = client.put("/api/v1/traffic/config", json={"opdk_weight": "bad"})
    assert r.status_code == 401


def test_public_endpoints_reveal_nothing(client):
    assert client.get("/livez").json() == {"status": "ok"}
    assert client.get("/docs").status_code == 200
    spec = client.get("/openapi.json").json()
    assert "AdminBearer" in spec["components"]["securitySchemes"]


@pytest.mark.parametrize("token", ["", "short", " " * 20])
def test_service_refuses_to_start_without_strong_token(tmp_path, token):
    with pytest.raises(SettingsError):
        Settings(api_token=token.strip(), database_path=tmp_path / "x.db")


def test_socket_path_not_leaked(tmp_path):
    from fastapi.testclient import TestClient
    s = Settings(api_token=TOKEN, database_path=tmp_path / "x.db", run_background=False,
                 haproxy_socket="/nonexistent/secret-admin.sock")
    with TestClient(create_app(s)) as c:
        r = c.get("/api/v1/traffic/config", headers=AUTH)
        assert "secret-admin" not in r.text
        h = c.get("/api/v1/health", headers=AUTH).json()
        assert h["haproxy"]["reachable"] is False and "secret-admin" not in str(h)
