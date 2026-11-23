"""FastAPI entrypoint, Prometheus /metrics exporter, telemetry APIs, and Dual-Mode UI server."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from app import __version__
from app.alerts import HysteresisAlertEngine
from app.collector import TelemetryCollector
from app.storage import MuraqibStorage

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = str(ROOT_DIR / "data" / "muraqib.db")
DEFAULT_UI_PATH = ROOT_DIR / "docs" / "index.html"


class ProbeRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    target_url: str = Field(..., min_length=3, max_length=300)
    protocol: Literal["auto", "http", "tcp"] = "auto"
    timeout_s: float = Field(default=2.0, ge=0.2, le=10.0)


class SimulateRequest(BaseModel):
    scenario: Literal[
        "cpu_spike",
        "memory_pressure",
        "disk_full",
        "latency_spike",
        "endpoint_down",
        "io_wait",
        "ups_battery",
        "reset",
    ]
    ticks: int = Field(default=1, ge=1, le=10)


class TriageRequest(BaseModel):
    action: Literal["ACKNOWLEDGE", "ESCALATE_L2", "RESOLVE"]
    notes: str = Field(default="", max_length=1000)
    operator: str = Field(default="L1-NOC", min_length=1, max_length=80)


def _escape_prom_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_prometheus_metrics(
    telemetry: dict[str, Any],
    targets: list[dict[str, Any]],
    alert_states: list[dict[str, Any]],
) -> str:
    """Render Prometheus text exposition format (version=0.0.4)."""
    active_alerts = sum(1 for a in alert_states if a["state"] == "FIRING")
    lines = [
        "# HELP muraqib_cpu_usage_percent Host CPU utilization percentage.",
        "# TYPE muraqib_cpu_usage_percent gauge",
        f"muraqib_cpu_usage_percent {float(telemetry['cpu_percent']):.2f}",
        "# HELP muraqib_memory_usage_percent Host physical memory utilization percentage.",
        "# TYPE muraqib_memory_usage_percent gauge",
        f"muraqib_memory_usage_percent {float(telemetry['memory_percent']):.2f}",
        "# HELP muraqib_disk_usage_percent Host root filesystem utilization percentage.",
        "# TYPE muraqib_disk_usage_percent gauge",
        f"muraqib_disk_usage_percent {float(telemetry['disk_percent']):.2f}",
        "# HELP muraqib_io_wait_percent Host CPU time waiting on I/O operations.",
        "# TYPE muraqib_io_wait_percent gauge",
        f"muraqib_io_wait_percent {float(telemetry['io_wait_percent']):.2f}",
        "# HELP muraqib_load_average_1m Host 1-minute load average.",
        "# TYPE muraqib_load_average_1m gauge",
        f"muraqib_load_average_1m {float(telemetry['load_avg_1m']):.2f}",
        "# HELP muraqib_target_up Probe availability status (1 = up, 0 = down).",
        "# TYPE muraqib_target_up gauge",
    ]
    for tgt in targets:
        lbl_name = _escape_prom_label(str(tgt["name"]))
        lbl_proto = _escape_prom_label(str(tgt["protocol"]))
        up_val = 1 if tgt["up"] else 0
        lines.append(f'muraqib_target_up{{target="{lbl_name}",protocol="{lbl_proto}"}} {up_val}')

    lines.extend(
        [
            "# HELP muraqib_target_latency_ms Probe round-trip latency in milliseconds.",
            "# TYPE muraqib_target_latency_ms gauge",
        ]
    )
    for tgt in targets:
        lbl_name = _escape_prom_label(str(tgt["name"]))
        lbl_proto = _escape_prom_label(str(tgt["protocol"]))
        lines.append(
            f'muraqib_target_latency_ms{{target="{lbl_name}",protocol="{lbl_proto}"}} {float(tgt["latency_ms"]):.2f}'
        )

    lines.extend(
        [
            "# HELP muraqib_active_alerts Number of currently firing hysteresis alert rules.",
            "# TYPE muraqib_active_alerts gauge",
            f"muraqib_active_alerts {active_alerts}",
            "# HELP muraqib_alert_rule_state Alert rule state code (0=OK, 1=PENDING, 2=FIRING, 3=RESOLVED).",
            "# TYPE muraqib_alert_rule_state gauge",
        ]
    )
    state_code_map = {"OK": 0, "PENDING": 1, "FIRING": 2, "RESOLVED": 3}
    for alert in alert_states:
        rule_lbl = _escape_prom_label(str(alert["rule"]))
        sev_lbl = _escape_prom_label(str(alert["severity"]))
        code = state_code_map.get(str(alert["state"]), 0)
        lines.append(f'muraqib_alert_rule_state{{rule="{rule_lbl}",severity="{sev_lbl}"}} {code}')

    return "\n".join(lines) + "\n"


def _compute_mtta_mttr(incident: dict[str, Any]) -> dict[str, float | None]:
    def _parse(ts: str | None) -> datetime | None:
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts)
        except ValueError:
            return None

    opened = _parse(incident.get("opened_at"))
    acked = _parse(incident.get("acknowledged_at"))
    resolved = _parse(incident.get("resolved_at"))
    mtta = round((acked - opened).total_seconds(), 2) if (opened and acked) else None
    mttr = round((resolved - opened).total_seconds(), 2) if (opened and resolved) else None
    return {"mtta_seconds": mtta, "mttr_seconds": mttr}


def create_app(
    *,
    db_path: str | None = None,
    collector: TelemetryCollector | None = None,
    ui_path: Path | None = None,
) -> FastAPI:
    """Create and wire a Muraqib FastAPI application instance."""
    resolved_db = db_path or os.environ.get("MURAQIB_DB_PATH") or DEFAULT_DB_PATH
    storage = MuraqibStorage(resolved_db)
    tele_collector = collector or TelemetryCollector()
    alert_engine = HysteresisAlertEngine(storage)
    html_path = ui_path or DEFAULT_UI_PATH

    app = FastAPI(
        title="Muraqib Telemetry (مُرَاقِب)",
        description="Real-Time Infrastructure & Service Observability Stack with Prometheus Exporter and Alert Hysteresis.",
        version=__version__,
    )
    app.state.storage = storage
    app.state.collector = tele_collector
    app.state.alert_engine = alert_engine

    def _run_evaluation_cycle(*, persist_sample: bool = True) -> dict[str, Any]:
        host = tele_collector.collect_host_metrics()
        targets_raw = storage.list_probe_targets()
        probe_summary = tele_collector.effective_probe_summary(targets_raw)
        combined = {
            **host,
            "endpoint_down_count": probe_summary["endpoint_down_count"],
            "max_latency_ms": probe_summary["max_latency_ms"],
        }
        if persist_sample:
            storage.insert_sample(host)
        alerts = alert_engine.evaluate(combined)
        return {
            "telemetry": host,
            "probes": probe_summary["targets"],
            "alerts": alerts,
            "active_alerts": sum(1 for a in alerts if a["state"] == "FIRING"),
            "pending_alerts": sum(1 for a in alerts if a["state"] == "PENDING"),
            "simulation": tele_collector.simulation.to_dict(),
        }

    @app.get("/", response_class=HTMLResponse)
    def serve_ui() -> HTMLResponse:
        if html_path.exists():
            return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
        return HTMLResponse(
            content="<h1>Muraqib Telemetry</h1><p>UI bundle not found at docs/index.html.</p>",
            status_code=200,
        )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "muraqib-telemetry",
            "version": __version__,
            "uptime_seconds": tele_collector.uptime_seconds(),
            "storage_mode": f"sqlite-{storage.journal_mode()}",
            "psutil_available": tele_collector.use_psutil,
            "active_alerts": alert_engine.active_firing_count(),
        }

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> PlainTextResponse:
        host = tele_collector.collect_host_metrics()
        probe_summary = tele_collector.effective_probe_summary(storage.list_probe_targets())
        body = render_prometheus_metrics(
            host,
            probe_summary["targets"],
            alert_engine.current_states(),
        )
        return PlainTextResponse(
            content=body,
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/api/snapshot")
    def api_snapshot(evaluate: bool = Query(default=True)) -> dict[str, Any]:
        if evaluate:
            cycle = _run_evaluation_cycle(persist_sample=True)
        else:
            host = tele_collector.collect_host_metrics()
            probe_summary = tele_collector.effective_probe_summary(storage.list_probe_targets())
            alerts = alert_engine.current_states()
            cycle = {
                "telemetry": host,
                "probes": probe_summary["targets"],
                "alerts": alerts,
                "active_alerts": sum(1 for a in alerts if a["state"] == "FIRING"),
                "pending_alerts": sum(1 for a in alerts if a["state"] == "PENDING"),
                "simulation": tele_collector.simulation.to_dict(),
            }

        samples = storage.list_samples(limit=30)
        incidents = storage.list_incidents(limit=25)
        prom_preview = render_prometheus_metrics(
            cycle["telemetry"],
            cycle["probes"],
            cycle["alerts"],
        )
        return {
            **cycle,
            "samples": samples,
            "incidents": incidents,
            "prometheus_preview": prom_preview,
        }

    @app.post("/api/probe")
    def api_probe(req: ProbeRequest) -> dict[str, Any]:
        try:
            probed = tele_collector.probe_endpoint(
                name=req.name,
                target_url=req.target_url,
                protocol=req.protocol,
                timeout_s=req.timeout_s,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        saved = storage.upsert_probe_target(probed)
        cycle = _run_evaluation_cycle(persist_sample=False)
        return {
            "probe": saved,
            "alerts": cycle["alerts"],
            "active_alerts": cycle["active_alerts"],
            "incidents": storage.list_incidents(limit=10),
        }

    @app.post("/api/simulate")
    def api_simulate(req: SimulateRequest) -> dict[str, Any]:
        try:
            sim_state = tele_collector.simulation.apply(req.scenario)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        last_cycle: dict[str, Any] = {}
        for _ in range(req.ticks):
            last_cycle = _run_evaluation_cycle(persist_sample=True)

        return {
            "simulation": sim_state,
            "ticks_executed": req.ticks,
            **last_cycle,
            "samples": storage.list_samples(limit=30),
            "incidents": storage.list_incidents(limit=25),
        }

    @app.get("/api/incidents")
    def api_incidents(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
        items = storage.list_incidents(limit=limit)
        for inc in items:
            inc.update(_compute_mtta_mttr(inc))
        return {"count": len(items), "incidents": items}

    @app.post("/api/incidents/{incident_id}/triage")
    def api_triage_incident(incident_id: int, req: TriageRequest) -> dict[str, Any]:
        updated = storage.triage_incident(
            incident_id,
            action=req.action,
            notes=req.notes,
            operator=req.operator,
        )
        if not updated:
            raise HTTPException(status_code=404, detail=f"Incident {incident_id} not found")
        updated.update(_compute_mtta_mttr(updated))
        return {
            "incident": updated,
            "triage_runbook": updated.get("runbook", {}),
        }

    return app


app = create_app()
