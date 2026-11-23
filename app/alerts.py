"""Hysteresis alert engine (OK -> PENDING -> FIRING -> RESOLVED) & L1->L2 runbooks."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any

from app.storage import MuraqibStorage


@dataclass
class AlertRule:
    """Declarative threshold rule with anti-fatigue hysteresis."""

    name: str
    metric_name: str
    warning_threshold: float
    critical_threshold: float
    consecutive_breaches: int = 3
    recovery_breaches: int = 2
    cooldown_seconds: float = 15.0
    unit: str = "%"


RUNBOOK_CATALOG: dict[str, dict[str, Any]] = {
    "HighCPUUtilization": {
        "title": "High CPU Saturation Triage",
        "title_ar": "تشخيص تشبع المعالج المركزي (CPU)",
        "probable_cause": "Runaway worker loop, unindexed query storm, or sudden request spike saturating cores.",
        "probable_cause_ar": "حلقة معالجة متكررة، استعلامات غير مفهرسة، أو ارتفاع مفاجئ في الطلبات يستهلك الأنوية.",
        "l1_checks": [
            "Verify if CPU spike is sustained across 3 consecutive scrape windows (hysteresis).",
            "Identify top consuming PIDs and check whether Load Average exceeds logical core count.",
        ],
        "linux_commands": [
            "top -b -n 1 -o %CPU | head -n 20",
            "ps -eo pid,ppid,cmd,%mem,%cpu --sort=-%cpu | head -n 10",
            "mpstat -P ALL 1 3",
        ],
        "windows_commands": [
            "Get-Process | Sort-Object CPU -Descending | Select-Object -First 10 Name,Id,CPU",
            "Get-Counter '\\Processor(_Total)\\% Processor Time' -SampleInterval 1 -MaxSamples 3",
        ],
        "l2_escalation": "Escalate to L2 Backend/SRE with top PID thread dump if CPU remains >90% after service throttle.",
    },
    "HighMemoryPressure": {
        "title": "Memory Exhaustion & OOM Risk Triage",
        "title_ar": "تشخيص ضغط الذاكرة وخطر نفاد RAM",
        "probable_cause": "Unbounded cache growth, memory leak in worker process, or oversized payload buffers.",
        "probable_cause_ar": "نمو غير مقيد للذاكرة المؤقتة (Cache)، تسرب ذاكرة في الخدمة، أو معالجة حمولات ضخمة.",
        "l1_checks": [
            "Check active swap thrashing and verify RSS growth trend over recent samples.",
            "Confirm OOM killer has not terminated critical workers in dmesg/Event Viewer.",
        ],
        "linux_commands": [
            "free -h && vmstat 1 5",
            "ps -eo pid,cmd,%mem,rss --sort=-%mem | head -n 10",
            "dmesg -T | grep -i -E 'oom|killed process' | tail -n 10",
        ],
        "windows_commands": [
            "Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 10 Name,Id,@{N='MB';E={[math]::Round($_.WorkingSet64/1MB,1)}}",
            "Get-Counter '\\Memory\\% Committed Bytes In Use'",
        ],
        "l2_escalation": "Escalate to L2 with heap profile / process RSS snapshot before forcing graceful worker recycle.",
    },
    "DiskSpaceCritical": {
        "title": "Filesystem Capacity & WAL Growth Triage",
        "title_ar": "تشخيص امتلاء القرص وتضخم السجلات",
        "probable_cause": "Unrotated application logs, orphaned WAL/temp files, or container image bloat.",
        "probable_cause_ar": "تراكم ملفات السجلات دون تدوير، تضخم ملفات WAL المؤقتة، أو صور حاويات قديمة.",
        "l1_checks": [
            "Locate mount point >85% and check inode exhaustion in addition to byte usage.",
            "Identify large unlinked-but-open files held by running daemons.",
        ],
        "linux_commands": [
            "df -hT && df -ih",
            "du -sh /var/log/* /app/data/* 2>/dev/null | sort -hr | head -n 15",
            "lsof +L1 | head -n 15",
        ],
        "windows_commands": [
            "Get-Volume | Select-Object DriveLetter,FileSystemLabel,@{N='FreeGB';E={[math]::Round($_.SizeRemaining/1GB,2)}}",
            "Get-ChildItem C:\\Logs -Recurse -ErrorAction SilentlyContinue | Sort-Object Length -Descending | Select-Object -First 10 FullName,Length",
        ],
        "l2_escalation": "Escalate to L2 Infrastructure if storage expansion or database compaction is required.",
    },
    "HighIOWait": {
        "title": "Storage I/O Wait Bottleneck Triage",
        "title_ar": "تشخيص اختناق انتظار الإدخال/الإخراج (I/O Wait)",
        "probable_cause": "Heavy synchronous fsync bursts, degraded disk array, or unbuffered sequential scans.",
        "probable_cause_ar": "كتابة متزامنة كثيفة على القرص، تدهور وحدة التخزين، أو مسح كامل لجداول دون فهارس.",
        "l1_checks": [
            "Compare %iowait vs await queue latency on block devices.",
            "Verify database WAL checkpoint frequency and disk queue depth.",
        ],
        "linux_commands": [
            "iostat -xz 1 3",
            "iotop -bon 3 | head -n 20",
            "vmstat 1 5",
        ],
        "windows_commands": [
            "Get-Counter '\\PhysicalDisk(_Total)\\Avg. Disk Queue Length','\\PhysicalDisk(_Total)\\Avg. Disk sec/Transfer'",
        ],
        "l2_escalation": "Escalate to L2 DBA/Storage team with iostat await metrics and slow-query digest.",
    },
    "EndpointDown": {
        "title": "Service Endpoint Unreachable Triage",
        "title_ar": "تشخيص توقف نقطة خدمة حرجة",
        "probable_cause": "Process crash, closed listening socket, upstream firewall block, or 5xx backend failure.",
        "probable_cause_ar": "توقف العملية الخلفية، إغلاق المنفذ، حظر جدار الحماية، أو استجابة خطأ 5xx.",
        "l1_checks": [
            "Confirm target TCP handshake and HTTP health status from collector host.",
            "Check service systemd/container status and recent stderr logs.",
        ],
        "linux_commands": [
            "ss -tulpn | grep -E '8000|5432|6379'",
            "curl -i -sS --max-time 3 http://127.0.0.1:8000/health",
            "journalctl -u muraqib -n 50 --no-pager",
        ],
        "windows_commands": [
            "Test-NetConnection -ComputerName 127.0.0.1 -Port 8000",
            "Invoke-WebRequest -Uri http://127.0.0.1:8000/health -UseBasicParsing",
        ],
        "l2_escalation": "Escalate to L2 Service Owner immediately if automated health check fails after restart.",
    },
    "HighTargetLatency": {
        "title": "Elevated Endpoint Latency Triage",
        "title_ar": "تشخيص ارتفاع زمن استجابة الخدمة",
        "probable_cause": "Thread-pool starvation, downstream DB lock contention, or TLS/DNS resolution delay.",
        "probable_cause_ar": "تشبع خيوط المعالجة، تعارض أقفال قاعدة البيانات، أو بطء حل DNS/TLS.",
        "l1_checks": [
            "Isolate DNS/TCP connect time vs Time-To-First-Byte (TTFB).",
            "Check concurrent connection saturation and upstream dependency latency.",
        ],
        "linux_commands": [
            "curl -w 'dns:%{time_namelookup} connect:%{time_connect} ttfb:%{time_starttransfer} total:%{time_total}\\n' -o /dev/null -s http://127.0.0.1:8000/health",
            "ss -s",
        ],
        "windows_commands": [
            "Measure-Command { Invoke-WebRequest http://127.0.0.1:8000/health -UseBasicParsing }",
        ],
        "l2_escalation": "Escalate to L2 Application SRE with trace IDs and TTFB breakdown.",
    },
    "UPSOnBattery": {
        "title": "Power Redundancy / UPS Battery Triage",
        "title_ar": "تشخيص انقطاع التيار والعمل على بطارية UPS",
        "probable_cause": "Facility utility outage or PDU breaker trip transferring rack load to UPS battery.",
        "probable_cause_ar": "انقطاع مصدر الطاقة الرئيسي وتحول الخادم للعمل على بطارية الطوارئ.",
        "l1_checks": [
            "Check remaining UPS battery runtime and facility power status.",
            "Prepare graceful workload drain if battery drops below 25%.",
        ],
        "linux_commands": [
            "upsc ups@localhost 2>/dev/null || upower -d",
        ],
        "windows_commands": [
            "Get-CimInstance -ClassName Win32_Battery | Select-Object Status,EstimatedChargeRemaining,EstimatedRunTime",
        ],
        "l2_escalation": "Escalate immediately to L2 Data Center Facilities & Infrastructure On-Call.",
    },
}


class HysteresisAlertEngine:
    """Evaluates telemetry rules with sustained breach windows to prevent Alert Fatigue."""

    def __init__(self, storage: MuraqibStorage, rules: list[AlertRule] | None = None) -> None:
        self.storage = storage
        self.rules: list[AlertRule] = rules or [
            AlertRule("HighCPUUtilization", "cpu_percent", 75.0, 90.0, consecutive_breaches=3, recovery_breaches=2, unit="%"),
            AlertRule("HighMemoryPressure", "memory_percent", 80.0, 92.0, consecutive_breaches=3, recovery_breaches=2, unit="%"),
            AlertRule("DiskSpaceCritical", "disk_percent", 85.0, 95.0, consecutive_breaches=2, recovery_breaches=2, unit="%"),
            AlertRule("HighIOWait", "io_wait_percent", 25.0, 45.0, consecutive_breaches=3, recovery_breaches=2, unit="%"),
            AlertRule("EndpointDown", "endpoint_down_count", 1.0, 1.0, consecutive_breaches=2, recovery_breaches=2, unit="targets"),
            AlertRule("HighTargetLatency", "max_latency_ms", 250.0, 800.0, consecutive_breaches=3, recovery_breaches=2, unit="ms"),
            AlertRule("UPSOnBattery", "ups_on_battery_num", 1.0, 1.0, consecutive_breaches=2, recovery_breaches=2, unit="flag"),
        ]
        self._states: dict[str, dict[str, Any]] = {
            r.name: {
                "rule": r.name,
                "metric": r.metric_name,
                "state": "OK",
                "severity": "normal",
                "breach_count": 0,
                "recovery_count": 0,
                "last_value": 0.0,
                "threshold": r.warning_threshold,
                "consecutive_required": r.consecutive_breaches,
                "cooldown_until": 0.0,
                "last_incident_id": None,
            }
            for r in self.rules
        }

    def evaluate(self, metrics: dict[str, Any]) -> list[dict[str, Any]]:
        """Run one hysteresis evaluation cycle across all rules and persist firing incidents."""
        now_mono = time.monotonic()
        enriched = dict(metrics)
        enriched["ups_on_battery_num"] = 1.0 if metrics.get("ups_on_battery") else 0.0

        results: list[dict[str, Any]] = []
        for rule in self.rules:
            val = float(enriched.get(rule.metric_name, 0.0))
            st = self._states[rule.name]
            st["last_value"] = round(val, 2)

            is_critical = val >= rule.critical_threshold
            is_warning = val >= rule.warning_threshold
            breached = is_critical or is_warning
            sev = "critical" if is_critical else ("warning" if is_warning else "normal")

            if breached:
                st["recovery_count"] = 0
                st["breach_count"] += 1
                st["severity"] = sev
                st["threshold"] = rule.critical_threshold if is_critical else rule.warning_threshold

                if st["state"] in ("OK", "RESOLVED"):
                    if st["breach_count"] >= rule.consecutive_breaches:
                        self._transition_to_firing(rule, st, val, sev)
                    else:
                        st["state"] = "PENDING"
                elif st["state"] == "PENDING":
                    if st["breach_count"] >= rule.consecutive_breaches:
                        self._transition_to_firing(rule, st, val, sev)
                elif st["state"] == "FIRING":
                    open_inc = self.storage.get_open_incident_for_rule(rule.name)
                    if open_inc:
                        self.storage.update_firing_incident(
                            open_inc["id"],
                            severity=sev,
                            metric_value=val,
                            summary=self._format_summary(rule, val, sev),
                        )
                        st["last_incident_id"] = open_inc["id"]
            else:
                st["breach_count"] = 0
                if st["state"] == "PENDING":
                    # Single transient spike cleared before consecutive threshold -> Alert Fatigue prevented!
                    st["state"] = "OK"
                    st["severity"] = "normal"
                elif st["state"] == "FIRING":
                    st["recovery_count"] += 1
                    if st["recovery_count"] >= rule.recovery_breaches:
                        resolved = self.storage.resolve_incident_for_rule(rule.name)
                        if resolved:
                            st["last_incident_id"] = resolved["id"]
                        st["state"] = "RESOLVED"
                        st["severity"] = "normal"
                        st["cooldown_until"] = now_mono + rule.cooldown_seconds
                elif st["state"] == "RESOLVED":
                    if now_mono >= st["cooldown_until"]:
                        st["state"] = "OK"
                        st["recovery_count"] = 0

            results.append(dict(st))
        return results

    def _transition_to_firing(
        self,
        rule: AlertRule,
        st: dict[str, Any],
        val: float,
        sev: str,
    ) -> None:
        st["state"] = "FIRING"
        threshold = rule.critical_threshold if sev == "critical" else rule.warning_threshold
        summary = self._format_summary(rule, val, sev)
        open_inc = self.storage.get_open_incident_for_rule(rule.name)
        if open_inc:
            updated = self.storage.update_firing_incident(
                open_inc["id"],
                severity=sev,
                metric_value=val,
                summary=summary,
            )
            st["last_incident_id"] = updated["id"] if updated else open_inc["id"]
        else:
            runbook = RUNBOOK_CATALOG.get(rule.name, {})
            created = self.storage.create_incident(
                rule_name=rule.name,
                severity=sev,
                metric_name=rule.metric_name,
                metric_value=val,
                threshold=threshold,
                summary=summary,
                runbook=runbook,
            )
            st["last_incident_id"] = created["id"]

    @staticmethod
    def _format_summary(rule: AlertRule, val: float, sev: str) -> str:
        threshold = rule.critical_threshold if sev == "critical" else rule.warning_threshold
        return (
            f"{rule.name} [{sev.upper()}]: {rule.metric_name}={val:.2f}{rule.unit} "
            f"breached threshold {threshold:.2f}{rule.unit} for {rule.consecutive_breaches} consecutive checks."
        )

    def current_states(self) -> list[dict[str, Any]]:
        return [dict(v) for v in self._states.values()]

    def active_firing_count(self) -> int:
        return sum(1 for v in self._states.values() if v["state"] == "FIRING")
