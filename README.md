# Muraqib Telemetry (`مُرَاقِب`) — Real-Time Infrastructure & Service Observability Stack

**Muraqib Telemetry** is a production-styled infrastructure and synthetic service observability platform built with **FastAPI**, **Prometheus exposition (`/metrics`)**, **anti-fatigue alert hysteresis**, **SQLite WAL incident persistence**, **L1 → L2 diagnostic triage runbooks**, and a **Dual-Mode Industrial Control-Room UI** (AR/EN + Dark/Light).

- **Live Browser Lab (GitHub Pages):** `https://tareq0001.github.io/muraqib-telemetry/`
- **GitHub Repository:** `https://github.com/Tareq0001/muraqib-telemetry`

---

## Architecture

```
+-----------------------------------------------------------------------+
|  Dual-Mode UI (docs/index.html)                                       |
|  - Live FastAPI Mode: polls /api/snapshot, triggers /api/simulate     |
|  - Browser Lab Mode: standalone client-side hysteresis + Canvas lab   |
+-----------------------------------+-----------------------------------+
                                    | HTTP / JSON / PromQL Scrape
+-----------------------------------v-----------------------------------+
|  FastAPI Observability Server (app/main.py)                           |
|  - GET /health       - GET /metrics (Prometheus v0.0.4)               |
|  - GET /api/snapshot - POST /api/probe (HTTP/TCP synthetic prober)    |
|  - POST /api/simulate (controlled hysteresis fault injection)         |
|  - GET /api/incidents & POST /api/incidents/{id}/triage (L1 -> L2)    |
+----------+------------------------+------------------------+----------+
           |                        |                        |
+----------v----------+  +----------v----------+  +----------v----------+
| TelemetryCollector  |  | HysteresisEngine    |  | MuraqibStorage      |
| (app/collector.py)  |  | (app/alerts.py)     |  | (app/storage.py)    |
| psutil + stdlib     |  | OK -> PENDING ->    |  | SQLite WAL tables:  |
| fallback + TCP/HTTP |  | FIRING -> RESOLVED  |  | samples, targets,   |
| latency probing     |  | + Runbook catalog   |  | incidents           |
+---------------------+  +---------------------+  +---------------------+
           ^
           | Scrapes /metrics every 5s
+----------+------------------------------------------------------------+
|  Docker Compose Stack (deploy/prometheus.yml + alert_rules.yml +      |
|  grafana-dashboard.json)                                              |
+-----------------------------------------------------------------------+
```

---

## Key Engineering Features

1. **Prometheus `/metrics` Exposition (`text/plain; version=0.0.4`):**
   Exposes `muraqib_cpu_usage_percent`, `muraqib_memory_usage_percent`, `muraqib_disk_usage_percent`, `muraqib_io_wait_percent`, `muraqib_load_average_1m`, `muraqib_target_up`, `muraqib_target_latency_ms`, `muraqib_active_alerts`, and `muraqib_alert_rule_state`.
2. **Anti-Fatigue Alert Hysteresis (`app/alerts.py`):**
   Prevents single-spike false positives (`Alert Fatigue`) using `OK -> PENDING -> FIRING -> RESOLVED -> OK` transitions with `consecutive_breaches`, `recovery_breaches`, and cooldown windows.
3. **Resilient Host Collector & Real HTTP/TCP Prober (`app/collector.py`):**
   Uses `psutil` with a graceful zero-dependency standard-library fallback, plus real HTTP (`httpx`) and TCP (`socket.create_connection`) round-trip latency probing.
4. **SQLite WAL Persistence & L1 → L2 Triage (`app/storage.py`):**
   Stores bounded telemetry history, probe targets, and incidents with parameterized SQL, MTTA/MTTR timestamps, and OS-specific Linux/Windows diagnostic runbooks.
5. **Dual-Mode Bilingual UI (`docs/index.html`):**
   Automatically connects to FastAPI when served locally/in Docker, or runs a full interactive Browser Lab on GitHub Pages with **AR/EN** (`RTL`/`LTR`) and **Dark/Light** toggles.

---

## Quickstart

### Local Python (FastAPI + Uvicorn)

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Open `http://127.0.0.1:8000` for the Control-Room UI, or `http://127.0.0.1:8000/metrics` for Prometheus metrics.

### Full Observability Stack (Docker Compose)

```bash
docker compose up --build
```

- **Muraqib API & UI:** `http://localhost:8000`
- **Prometheus:** `http://localhost:9090`
- **Grafana:** `http://localhost:3000` (import `deploy/grafana-dashboard.json`)

---

## API Reference

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Dual-Mode Control-Room UI (`docs/index.html`) |
| `GET` | `/health` | Service health, uptime, SQLite WAL status |
| `GET` | `/metrics` | Prometheus text exposition format (`0.0.4`) |
| `GET` | `/api/snapshot` | Live telemetry, probe statuses, alert states, samples, incidents |
| `POST` | `/api/probe` | Execute real HTTP/TCP probe and persist target |
| `POST` | `/api/simulate` | Inject controlled fault scenario (`cpu_spike`, `memory_pressure`, `disk_full`, `io_wait`, `latency_spike`, `endpoint_down`, `ups_battery`, `reset`) |
| `GET` | `/api/incidents` | List incidents with MTTA/MTTR metrics |
| `POST` | `/api/incidents/{id}/triage` | `ACKNOWLEDGE`, `ESCALATE_L2`, or `RESOLVE` with operator notes |

---

## Running Tests

```bash
python -m pytest -v
```

All automated tests in `tests/test_muraqib.py` verify Prometheus formatting, hysteresis anti-fatigue transitions, SQLite WAL persistence, L1→L2 triage, local HTTP/TCP probing, and stdlib collector fallback.
