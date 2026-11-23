"""SQLite WAL persistence layer for telemetry samples, probe targets, and incidents."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class MuraqibStorage:
    """Thread-safe SQLite WAL repository using parameterized queries."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self._seed_default_targets()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recorded_at TEXT NOT NULL,
                    cpu_percent REAL NOT NULL,
                    memory_percent REAL NOT NULL,
                    disk_percent REAL NOT NULL,
                    load_avg_1m REAL NOT NULL,
                    io_wait_percent REAL NOT NULL,
                    ups_on_battery INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT 'live'
                );

                CREATE INDEX IF NOT EXISTS idx_samples_recorded_at
                    ON samples(recorded_at DESC);

                CREATE TABLE IF NOT EXISTS probe_targets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    target_url TEXT NOT NULL,
                    protocol TEXT NOT NULL DEFAULT 'http',
                    up INTEGER NOT NULL DEFAULT 1,
                    latency_ms REAL NOT NULL DEFAULT 0.0,
                    status_code INTEGER,
                    last_error TEXT,
                    checked_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS incidents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_name TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    state TEXT NOT NULL,
                    metric_name TEXT NOT NULL,
                    metric_value REAL NOT NULL,
                    threshold REAL NOT NULL,
                    summary TEXT NOT NULL,
                    triage_status TEXT NOT NULL DEFAULT 'OPEN',
                    triage_notes TEXT NOT NULL DEFAULT '',
                    runbook_json TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    acknowledged_at TEXT,
                    resolved_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_incidents_state_opened
                    ON incidents(state, opened_at DESC);
                """
            )

    def _seed_default_targets(self) -> None:
        now = utc_now_iso()
        defaults = [
            ("api-gateway-health", "http://127.0.0.1:8000/health", "http", 1, 4.2, 200, None, now),
            ("core-postgres-tcp", "tcp://127.0.0.1:5432", "tcp", 1, 1.8, None, None, now),
            ("edge-cache-redis", "tcp://127.0.0.1:6379", "tcp", 1, 1.1, None, None, now),
        ]
        with self._connect() as conn:
            for row in defaults:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO probe_targets
                        (name, target_url, protocol, up, latency_ms, status_code, last_error, checked_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    row,
                )

    def journal_mode(self) -> str:
        with self._connect() as conn:
            row = conn.execute("PRAGMA journal_mode;").fetchone()
            return str(row[0]).lower() if row else "unknown"

    def insert_sample(self, sample: dict[str, Any]) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO samples
                    (recorded_at, cpu_percent, memory_percent, disk_percent,
                     load_avg_1m, io_wait_percent, ups_on_battery, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sample["recorded_at"],
                    float(sample["cpu_percent"]),
                    float(sample["memory_percent"]),
                    float(sample["disk_percent"]),
                    float(sample["load_avg_1m"]),
                    float(sample["io_wait_percent"]),
                    1 if sample.get("ups_on_battery") else 0,
                    str(sample.get("source", "live")),
                ),
            )
            # Keep bounded ring buffer in SQLite (last 500 samples)
            conn.execute(
                """
                DELETE FROM samples
                WHERE id NOT IN (
                    SELECT id FROM samples ORDER BY id DESC LIMIT 500
                )
                """
            )
            return int(cursor.lastrowid)

    def list_samples(self, limit: int = 30) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, recorded_at, cpu_percent, memory_percent, disk_percent,
                       load_avg_1m, io_wait_percent, ups_on_battery, source
                FROM samples
                ORDER BY id DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        result = [dict(r) for r in reversed(rows)]
        for item in result:
            item["ups_on_battery"] = bool(item["ups_on_battery"])
        return result

    def upsert_probe_target(self, target: dict[str, Any]) -> dict[str, Any]:
        checked_at = target.get("checked_at") or utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO probe_targets
                    (name, target_url, protocol, up, latency_ms, status_code, last_error, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    target_url = excluded.target_url,
                    protocol = excluded.protocol,
                    up = excluded.up,
                    latency_ms = excluded.latency_ms,
                    status_code = excluded.status_code,
                    last_error = excluded.last_error,
                    checked_at = excluded.checked_at
                """,
                (
                    target["name"],
                    target["target_url"],
                    target["protocol"],
                    1 if target["up"] else 0,
                    float(target["latency_ms"]),
                    target.get("status_code"),
                    target.get("last_error"),
                    checked_at,
                ),
            )
            row = conn.execute(
                "SELECT * FROM probe_targets WHERE name = ?",
                (target["name"],),
            ).fetchone()
        return self._format_probe_row(dict(row))

    def list_probe_targets(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM probe_targets ORDER BY id ASC"
            ).fetchall()
        return [self._format_probe_row(dict(r)) for r in rows]

    @staticmethod
    def _format_probe_row(row: dict[str, Any]) -> dict[str, Any]:
        row["up"] = bool(row["up"])
        return row

    def get_open_incident_for_rule(self, rule_name: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM incidents
                WHERE rule_name = ? AND state = 'FIRING'
                ORDER BY id DESC LIMIT 1
                """,
                (rule_name,),
            ).fetchone()
        return self._format_incident_row(dict(row)) if row else None

    def create_incident(
        self,
        *,
        rule_name: str,
        severity: str,
        metric_name: str,
        metric_value: float,
        threshold: float,
        summary: str,
        runbook: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now_iso()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO incidents
                    (rule_name, severity, state, metric_name, metric_value, threshold,
                     summary, triage_status, triage_notes, runbook_json, opened_at, updated_at)
                VALUES (?, ?, 'FIRING', ?, ?, ?, ?, 'OPEN', '', ?, ?, ?)
                """,
                (
                    rule_name,
                    severity,
                    metric_name,
                    float(metric_value),
                    float(threshold),
                    summary,
                    json.dumps(runbook, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM incidents WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        return self._format_incident_row(dict(row))

    def update_firing_incident(
        self,
        incident_id: int,
        *,
        severity: str,
        metric_value: float,
        summary: str,
    ) -> dict[str, Any] | None:
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE incidents
                SET severity = ?, metric_value = ?, summary = ?, updated_at = ?
                WHERE id = ?
                """,
                (severity, float(metric_value), summary, now, int(incident_id)),
            )
            row = conn.execute(
                "SELECT * FROM incidents WHERE id = ?",
                (int(incident_id),),
            ).fetchone()
        return self._format_incident_row(dict(row)) if row else None

    def resolve_incident_for_rule(self, rule_name: str) -> dict[str, Any] | None:
        existing = self.get_open_incident_for_rule(rule_name)
        if not existing:
            return None
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE incidents
                SET state = 'RESOLVED',
                    triage_status = CASE WHEN triage_status = 'OPEN' THEN 'RESOLVED' ELSE triage_status END,
                    resolved_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, now, existing["id"]),
            )
            row = conn.execute(
                "SELECT * FROM incidents WHERE id = ?",
                (existing["id"],),
            ).fetchone()
        return self._format_incident_row(dict(row)) if row else None

    def get_incident(self, incident_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM incidents WHERE id = ?",
                (int(incident_id),),
            ).fetchone()
        return self._format_incident_row(dict(row)) if row else None

    def list_incidents(self, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM incidents ORDER BY id DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        return [self._format_incident_row(dict(r)) for r in rows]

    def triage_incident(
        self,
        incident_id: int,
        *,
        action: str,
        notes: str = "",
        operator: str = "L1-NOC",
    ) -> dict[str, Any] | None:
        existing = self.get_incident(incident_id)
        if not existing:
            return None

        now = utc_now_iso()
        action_upper = action.strip().upper()
        status_map = {
            "ACKNOWLEDGE": "ACKNOWLEDGED",
            "ACKNOWLEDGED": "ACKNOWLEDGED",
            "ESCALATE_L2": "ESCALATED_L2",
            "ESCALATED_L2": "ESCALATED_L2",
            "RESOLVE": "RESOLVED",
            "RESOLVED": "RESOLVED",
        }
        new_triage_status = status_map.get(action_upper, "ACKNOWLEDGED")
        new_state = "RESOLVED" if new_triage_status == "RESOLVED" else existing["state"]
        ack_at = existing["acknowledged_at"] or now
        resolved_at = existing["resolved_at"] or (now if new_triage_status == "RESOLVED" else None)

        note_line = f"[{now}] ({operator} -> {new_triage_status})"
        if notes.strip():
            note_line = f"{note_line}: {notes.strip()}"
        combined_notes = (
            f"{existing['triage_notes']}\n{note_line}".strip()
            if existing["triage_notes"]
            else note_line
        )

        with self._connect() as conn:
            conn.execute(
                """
                UPDATE incidents
                SET state = ?,
                    triage_status = ?,
                    triage_notes = ?,
                    acknowledged_at = ?,
                    resolved_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    new_state,
                    new_triage_status,
                    combined_notes,
                    ack_at,
                    resolved_at,
                    now,
                    int(incident_id),
                ),
            )
            row = conn.execute(
                "SELECT * FROM incidents WHERE id = ?",
                (int(incident_id),),
            ).fetchone()
        return self._format_incident_row(dict(row)) if row else None

    @staticmethod
    def _format_incident_row(row: dict[str, Any]) -> dict[str, Any]:
        try:
            row["runbook"] = json.loads(row.pop("runbook_json"))
        except (KeyError, ValueError, TypeError):
            row["runbook"] = {}
        return row
