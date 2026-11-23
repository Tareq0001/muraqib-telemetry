"""Host telemetry collector (psutil with stdlib fallback) and HTTP/TCP prober."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import platform
import shutil
import socket
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from app.storage import utc_now_iso

try:
    import psutil  # type: ignore

    PSUTIL_AVAILABLE = True
except ImportError:  # pragma: no cover - tested via explicit fallback flag
    psutil = None
    PSUTIL_AVAILABLE = False


VALID_SCENARIOS = frozenset(
    {
        "cpu_spike",
        "memory_pressure",
        "disk_full",
        "latency_spike",
        "endpoint_down",
        "io_wait",
        "ups_battery",
        "reset",
    }
)


@dataclass
class SimulationState:
    """Controlled fault-injection state for testing alert hysteresis safely."""

    active_scenario: str = "none"
    overrides: dict[str, Any] = field(default_factory=dict)

    def apply(self, scenario: str) -> dict[str, Any]:
        key = scenario.strip().lower()
        if key not in VALID_SCENARIOS:
            raise ValueError(f"Unsupported simulation scenario: {scenario}")
        if key == "reset":
            self.active_scenario = "none"
            self.overrides.clear()
        else:
            self.active_scenario = key
            if key == "cpu_spike":
                self.overrides = {"cpu_percent": 94.6, "load_avg_1m": 6.4}
            elif key == "memory_pressure":
                self.overrides = {"memory_percent": 93.8}
            elif key == "disk_full":
                self.overrides = {"disk_percent": 96.4}
            elif key == "io_wait":
                self.overrides = {"io_wait_percent": 52.0, "cpu_percent": 82.0}
            elif key == "latency_spike":
                self.overrides = {"max_latency_ms": 940.0}
            elif key == "endpoint_down":
                self.overrides = {"force_endpoint_down": True}
            elif key == "ups_battery":
                self.overrides = {"ups_on_battery": True}
        return self.to_dict()

    def to_dict(self) -> dict[str, Any]:
        return {
            "active_scenario": self.active_scenario,
            "overrides": dict(self.overrides),
        }


class TelemetryCollector:
    """Collects host CPU/RAM/Disk/IOWait/Load metrics and probes network targets."""

    def __init__(self, *, use_psutil: bool | None = None) -> None:
        self.started_at = time.monotonic()
        self.use_psutil = PSUTIL_AVAILABLE if use_psutil is None else (use_psutil and PSUTIL_AVAILABLE)
        self.simulation = SimulationState()
        if self.use_psutil and psutil is not None:
            # Prime cpu_percent baseline so subsequent interval=None calls are accurate
            psutil.cpu_percent(interval=None)

    def uptime_seconds(self) -> float:
        return round(time.monotonic() - self.started_at, 2)

    def collect_host_metrics(self) -> dict[str, Any]:
        """Return live host telemetry (psutil or stdlib fallback) merged with any simulation."""
        if self.use_psutil and psutil is not None:
            sample = self._collect_psutil()
        else:
            sample = self._collect_stdlib_fallback()

        sample["recorded_at"] = utc_now_iso()
        sample["hostname"] = platform.node() or "muraqib-node"
        sample["os"] = f"{platform.system()} {platform.release()}".strip()
        sample["collector_backend"] = "psutil" if (self.use_psutil and psutil is not None) else "stdlib-fallback"
        sample["ups_on_battery"] = False
        sample["source"] = "live"

        if self.simulation.active_scenario != "none":
            sample["source"] = "simulated"
            for key in ("cpu_percent", "memory_percent", "disk_percent", "load_avg_1m", "io_wait_percent", "ups_on_battery"):
                if key in self.simulation.overrides:
                    sample[key] = self.simulation.overrides[key]

        return sample

    def _collect_psutil(self) -> dict[str, float]:
        assert psutil is not None
        cpu_pct = float(psutil.cpu_percent(interval=None))
        # Clamp initial 0.0 burst to non-negative float
        if cpu_pct <= 0.0:
            cpu_pct = float(psutil.cpu_percent(interval=0.05))
        mem_pct = float(psutil.virtual_memory().percent)
        disk_root = os.path.abspath(os.sep)
        disk_pct = float(psutil.disk_usage(disk_root).percent)

        try:
            load_1m = float(os.getloadavg()[0])
        except (AttributeError, OSError):
            cpu_count = max(1, psutil.cpu_count() or 1)
            load_1m = round((cpu_pct / 100.0) * cpu_count, 2)

        io_wait = 0.0
        times_pct = psutil.cpu_times_percent(interval=None)
        if hasattr(times_pct, "iowait"):
            io_wait = float(getattr(times_pct, "iowait", 0.0))

        return {
            "cpu_percent": round(cpu_pct, 2),
            "memory_percent": round(mem_pct, 2),
            "disk_percent": round(disk_pct, 2),
            "load_avg_1m": round(load_1m, 2),
            "io_wait_percent": round(io_wait, 2),
        }

    def _collect_stdlib_fallback(self) -> dict[str, float]:
        """Graceful zero-dependency fallback when psutil is absent."""
        disk_root = os.path.abspath(os.sep)
        usage = shutil.disk_usage(disk_root)
        disk_pct = round((usage.used / usage.total) * 100.0, 2) if usage.total else 0.0

        load_1m = 0.0
        try:
            load_1m = round(float(os.getloadavg()[0]), 2)
        except (AttributeError, OSError):
            load_1m = 0.45

        cpu_count = max(1, os.cpu_count() or 1)
        cpu_pct = self._read_proc_cpu_or_estimate(load_1m, cpu_count)
        mem_pct = self._read_memory_fallback()

        return {
            "cpu_percent": round(cpu_pct, 2),
            "memory_percent": round(mem_pct, 2),
            "disk_percent": disk_pct,
            "load_avg_1m": load_1m,
            "io_wait_percent": 0.0,
        }

    @staticmethod
    def _read_proc_cpu_or_estimate(load_1m: float, cpu_count: int) -> float:
        proc_stat = "/proc/stat"
        if os.path.exists(proc_stat):
            try:
                with open(proc_stat, "r", encoding="utf-8") as fh:
                    parts = fh.readline().split()
                if parts and parts[0] == "cpu" and len(parts) >= 5:
                    nums = [int(x) for x in parts[1:8]]
                    total = sum(nums)
                    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
                    if total > 0:
                        return min(100.0, max(0.0, ((total - idle) / total) * 100.0))
            except OSError:
                pass
        return min(100.0, round((load_1m / cpu_count) * 100.0, 2))

    @staticmethod
    def _read_memory_fallback() -> float:
        proc_meminfo = "/proc/meminfo"
        if os.path.exists(proc_meminfo):
            try:
                info: dict[str, int] = {}
                with open(proc_meminfo, "r", encoding="utf-8") as fh:
                    for line in fh:
                        if ":" in line:
                            k, v = line.split(":", 1)
                            tokens = v.strip().split()
                            if tokens and tokens[0].isdigit():
                                info[k.strip()] = int(tokens[0])
                total = info.get("MemTotal", 0)
                avail = info.get("MemAvailable", info.get("MemFree", 0))
                if total > 0:
                    return round(((total - avail) / total) * 100.0, 2)
            except OSError:
                pass
        if os.name == "nt":
            try:
                import ctypes

                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                    ]

                mem = MEMORYSTATUSEX()
                mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
                if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
                    return float(mem.dwMemoryLoad)
            except Exception:
                pass
        return 42.0

    def probe_endpoint(
        self,
        *,
        name: str,
        target_url: str,
        protocol: str = "auto",
        timeout_s: float = 2.0,
    ) -> dict[str, Any]:
        """Probe a real HTTP or TCP endpoint and return status & round-trip latency_ms."""
        cleaned_url = target_url.strip()
        if not cleaned_url:
            raise ValueError("target_url must not be empty")

        parsed = urlparse(cleaned_url)
        scheme = parsed.scheme.lower()
        proto = protocol.strip().lower()
        if proto == "auto":
            if scheme in ("http", "https"):
                proto = "http"
            elif scheme == "tcp" or (not scheme and ":" in cleaned_url):
                proto = "tcp"
            else:
                raise ValueError("Unsupported target scheme; use http://, https://, or tcp://host:port")

        if proto not in ("http", "tcp"):
            raise ValueError("protocol must be 'http', 'tcp', or 'auto'")

        timeout_s = max(0.2, min(float(timeout_s), 10.0))
        start = time.perf_counter()
        up = False
        status_code: int | None = None
        last_error: str | None = None

        if proto == "http":
            if scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError("HTTP probe requires a valid http:// or https:// URL")
            try:
                with httpx.Client(timeout=timeout_s, follow_redirects=False) as client:
                    resp = client.get(cleaned_url)
                status_code = int(resp.status_code)
                up = 200 <= status_code < 500
                if not up:
                    last_error = f"HTTP {status_code}"
            except Exception as exc:
                last_error = str(exc)
        else:
            host, port = self._parse_tcp_host_port(cleaned_url, parsed)
            try:
                with socket.create_connection((host, port), timeout=timeout_s):
                    up = True
            except Exception as exc:
                last_error = str(exc)

        latency_ms = round((time.perf_counter() - start) * 1000.0, 2)
        return {
            "name": name.strip() or cleaned_url,
            "target_url": cleaned_url,
            "protocol": proto,
            "up": up,
            "latency_ms": latency_ms,
            "status_code": status_code,
            "last_error": last_error,
            "checked_at": utc_now_iso(),
        }

    @staticmethod
    def _parse_tcp_host_port(raw: str, parsed: Any) -> tuple[str, int]:
        if parsed.scheme.lower() == "tcp":
            host = parsed.hostname
            port = parsed.port
        else:
            if ":" not in raw:
                raise ValueError("TCP target must include host:port")
            host_part, port_part = raw.rsplit(":", 1)
            host = host_part.strip("[]")
            port = int(port_part) if port_part.isdigit() else None
        if not host or not port or not (1 <= int(port) <= 65535):
            raise ValueError("Invalid TCP host or port")
        return host, int(port)

    def effective_probe_summary(self, probe_targets: list[dict[str, Any]]) -> dict[str, Any]:
        """Compute probe aggregates, applying active simulation overrides if enabled."""
        targets = [dict(t) for t in probe_targets]
        if self.simulation.active_scenario == "endpoint_down" and targets:
            targets[0]["up"] = False
            targets[0]["status_code"] = 503
            targets[0]["last_error"] = "Simulated 503 Service Unavailable"
        elif self.simulation.active_scenario == "latency_spike" and targets:
            targets[0]["latency_ms"] = float(self.simulation.overrides.get("max_latency_ms", 940.0))

        down_count = sum(1 for t in targets if not t["up"])
        max_latency = max((float(t["latency_ms"]) for t in targets), default=0.0)
        return {
            "targets": targets,
            "endpoint_down_count": down_count,
            "max_latency_ms": round(max_latency, 2),
        }
