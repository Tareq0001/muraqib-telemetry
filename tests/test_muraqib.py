"""Comprehensive pytest suite for Muraqib Telemetry (مُرَاقِب)."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import os
from pathlib import Path
import sys
import threading

from fastapi.testclient import TestClient
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.collector import TelemetryCollector
from app.main import create_app


class _OkHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    db_file = tmp_path / "test_muraqib.db"
    app = create_app(db_path=str(db_file))
    with TestClient(app) as tc:
        yield tc


def test_health_and_ui_serving(client: TestClient) -> None:
    health = client.get("/health")
    assert health.status_code == 200
    payload = health.json()
    assert payload["status"] == "ok"
    assert payload["service"] == "muraqib-telemetry"
    assert payload["storage_mode"] == "sqlite-wal"
    assert payload["uptime_seconds"] >= 0.0

    ui = client.get("/")
    assert ui.status_code == 200
    assert "Muraqib Telemetry" in ui.text
    assert "muraqib_cpu_usage_percent" in ui.text


def test_prometheus_metrics_exposition_format(client: TestClient) -> None:
    # Seed at least one snapshot first
    snap = client.get("/api/snapshot")
    assert snap.status_code == 200

    res = client.get("/metrics")
    assert res.status_code == 200
    assert "text/plain" in res.headers["content-type"]
    assert "version=0.0.4" in res.headers["content-type"]

    body = res.text
    for expected in (
        "# HELP muraqib_cpu_usage_percent",
        "# TYPE muraqib_cpu_usage_percent gauge",
        "muraqib_cpu_usage_percent ",
        "muraqib_memory_usage_percent ",
        "muraqib_disk_usage_percent ",
        'muraqib_target_up{target="api-gateway-health",protocol="http"}',
        'muraqib_target_latency_ms{target="api-gateway-health",protocol="http"}',
        "muraqib_active_alerts ",
    ):
        assert expected in body


def test_hysteresis_prevents_alert_fatigue_and_fires_on_sustained_breach(client: TestClient) -> None:
    # 1. Single spike -> PENDING only, zero firing incidents
    single = client.post("/api/simulate", json={"scenario": "cpu_spike", "ticks": 1})
    assert single.status_code == 200
    single_data = single.json()
    cpu_rule = next(a for a in single_data["alerts"] if a["rule"] == "HighCPUUtilization")
    assert cpu_rule["state"] == "PENDING"
    assert cpu_rule["breach_count"] == 1
    assert single_data["active_alerts"] == 0
    assert len(single_data["incidents"]) == 0

    # 2. Transient spike clears -> returns to OK cleanly (no alert fatigue)
    recovered = client.post("/api/simulate", json={"scenario": "reset", "ticks": 1})
    assert recovered.status_code == 200
    cpu_after_reset = next(a for a in recovered.json()["alerts"] if a["rule"] == "HighCPUUtilization")
    assert cpu_after_reset["state"] == "OK"
    assert cpu_after_reset["breach_count"] == 0
    assert len(recovered.json()["incidents"]) == 0

    # 3. Sustained breach (3 consecutive ticks) -> FIRING + SQLite incident created
    sustained = client.post("/api/simulate", json={"scenario": "cpu_spike", "ticks": 3})
    assert sustained.status_code == 200
    sus_data = sustained.json()
    cpu_firing = next(a for a in sus_data["alerts"] if a["rule"] == "HighCPUUtilization")
    assert cpu_firing["state"] == "FIRING"
    assert cpu_firing["severity"] == "critical"
    assert sus_data["active_alerts"] >= 1
    assert len(sus_data["incidents"]) == 1
    inc = sus_data["incidents"][0]
    assert inc["rule_name"] == "HighCPUUtilization"
    assert inc["state"] == "FIRING"
    assert "top -b" in " ".join(inc["runbook"]["linux_commands"])

    # 4. Sustained recovery (2 healthy ticks) -> RESOLVED
    resolved = client.post("/api/simulate", json={"scenario": "reset", "ticks": 2})
    assert resolved.status_code == 200
    res_data = resolved.json()
    cpu_resolved = next(a for a in res_data["alerts"] if a["rule"] == "HighCPUUtilization")
    assert cpu_resolved["state"] == "RESOLVED"
    assert res_data["incidents"][0]["state"] == "RESOLVED"
    assert res_data["incidents"][0]["resolved_at"] is not None


def test_sqlite_wal_persistence_and_l1_l2_triage(tmp_path: Path) -> None:
    db_file = tmp_path / "persist.db"
    app1 = create_app(db_path=str(db_file))
    with TestClient(app1) as c1:
        sim = c1.post("/api/simulate", json={"scenario": "memory_pressure", "ticks": 3})
        assert sim.status_code == 200
        inc_id = sim.json()["incidents"][0]["id"]

        ack = c1.post(
            f"/api/incidents/{inc_id}/triage",
            json={"action": "ACKNOWLEDGE", "notes": "Checking RSS via vmstat", "operator": "Tareq-L1"},
        )
        assert ack.status_code == 200
        assert ack.json()["incident"]["triage_status"] == "ACKNOWLEDGED"
        assert ack.json()["incident"]["mtta_seconds"] is not None

        esc = c1.post(
            f"/api/incidents/{inc_id}/triage",
            json={"action": "ESCALATE_L2", "notes": "Heap growing rapidly, paging SRE", "operator": "Tareq-L1"},
        )
        assert esc.status_code == 200
        esc_inc = esc.json()["incident"]
        assert esc_inc["triage_status"] == "ESCALATED_L2"
        assert "Tareq-L1" in esc_inc["triage_notes"]
        assert len(esc.json()["triage_runbook"]["linux_commands"]) >= 2

    # Re-instantiate app against same SQLite DB to verify durability
    app2 = create_app(db_path=str(db_file))
    with TestClient(app2) as c2:
        listed = c2.get("/api/incidents")
        assert listed.status_code == 200
        assert listed.json()["count"] == 1
        assert listed.json()["incidents"][0]["triage_status"] == "ESCALATED_L2"

        missing = c2.post("/api/incidents/9999/triage", json={"action": "ACKNOWLEDGE"})
        assert missing.status_code == 404


def test_real_local_http_and_tcp_probing(client: TestClient) -> None:
    server = HTTPServer(("127.0.0.1", 0), _OkHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        http_probe = client.post(
            "/api/probe",
            json={
                "name": "ephemeral-http-svc",
                "target_url": f"http://127.0.0.1:{port}/health",
                "protocol": "http",
                "timeout_s": 2.0,
            },
        )
        assert http_probe.status_code == 200
        assert http_probe.json()["probe"]["up"] is True
        assert http_probe.json()["probe"]["status_code"] == 200
        assert http_probe.json()["probe"]["latency_ms"] >= 0.0

        tcp_probe = client.post(
            "/api/probe",
            json={
                "name": "ephemeral-tcp-svc",
                "target_url": f"tcp://127.0.0.1:{port}",
                "protocol": "tcp",
                "timeout_s": 2.0,
            },
        )
        assert tcp_probe.status_code == 200
        assert tcp_probe.json()["probe"]["up"] is True
        assert tcp_probe.json()["probe"]["protocol"] == "tcp"
    finally:
        server.shutdown()
        server.server_close()

    bad_probe = client.post(
        "/api/probe",
        json={"name": "bad", "target_url": "ftp://example.com/file", "protocol": "auto"},
    )
    assert bad_probe.status_code == 400


def test_stdlib_fallback_collector_and_validation(tmp_path: Path) -> None:
    fallback_collector = TelemetryCollector(use_psutil=False)
    app = create_app(db_path=str(tmp_path / "fallback.db"), collector=fallback_collector)
    with TestClient(app) as tc:
        snap = tc.get("/api/snapshot")
        assert snap.status_code == 200
        tele = snap.json()["telemetry"]
        assert tele["collector_backend"] == "stdlib-fallback"
        assert 0.0 <= tele["cpu_percent"] <= 100.0
        assert 0.0 <= tele["memory_percent"] <= 100.0
        assert 0.0 <= tele["disk_percent"] <= 100.0

        invalid_sim = tc.post("/api/simulate", json={"scenario": "invalid_mode"})
        assert invalid_sim.status_code == 422
