"""Machine stats for the kiosk, read from inside the MCP container.

/proc and /sys inside a container show the host's uptime, load, memory, temperatures and batteries; the state
directory is mounted at /state, and /etc/os-release of the host at /host/os-release.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from .weather import STATE_DIR


def _read(path: str | Path, default: str = "") -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def _cpu_pct(sample_s: float = 0.25) -> float | None:
    def snap():
        f = [int(x) for x in _read("/proc/stat").splitlines()[0].split()[1:]]
        return sum(f), f[3] + (f[4] if len(f) > 4 else 0)
    try:
        t0, i0 = snap()
        time.sleep(sample_s)
        t1, i1 = snap()
        return round(100 * (1 - (i1 - i0) / max(t1 - t0, 1)), 1)
    except (IndexError, ValueError):
        return None


def _temps() -> dict:
    out = {}
    names = {"x86_pkg_temp": "cpu_package", "acpitz": "acpi", "pch_skylake": "chipset", "pch_cannonlake": "chipset",
             "iwlwifi_1": "wifi", "B0D4": "cpu_b0d4"}
    for z in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        kind = _read(z / "type")
        if kind.startswith("INT3400"):  # a policy zone with a fixed dummy value, not a sensor
            continue
        try:
            out[names.get(kind, kind)] = round(int(_read(z / "temp", "0")) / 1000, 1)
        except ValueError:
            continue
    return out


def _power() -> dict:
    out = {"ac": None, "batteries": []}
    for p in sorted(Path("/sys/class/power_supply").glob("*")):
        kind = _read(p / "type")
        if kind == "Mains":
            out["ac"] = _read(p / "online") == "1"
        elif kind == "Battery":
            b = {"name": p.name, "percent": int(_read(p / "capacity", "0") or 0), "status": _read(p / "status")}
            full = _read(p / "energy_full") or _read(p / "charge_full")
            design = _read(p / "energy_full_design") or _read(p / "charge_full_design")
            if full and design and int(design):
                b["health_pct"] = round(100 * int(full) / int(design))
            cycles = _read(p / "cycle_count")
            if cycles.isdigit() and int(cycles):
                b["cycles"] = int(cycles)
            out["batteries"].append(b)
    return out


def _dir_size(path: Path, pattern: str) -> tuple[int, int]:
    n = size = 0
    for f in path.glob(pattern):
        try:
            size += f.stat().st_size
            n += 1
        except OSError:
            pass
    return n, size


def machine() -> dict:
    up = float((_read("/proc/uptime", "0 0").split() or ["0"])[0])
    load = _read("/proc/loadavg").split()[:3]
    mem = {}
    for line in _read("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        if k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
            mem[k] = int(v.split()[0]) // 1024
    os_name = next((ln.split("=", 1)[1].strip('"') for ln in _read("/host/os-release").splitlines()
                    if ln.startswith("PRETTY_NAME=")), None)
    try:
        st = os.statvfs(STATE_DIR)
        disk = {"free_gb": round(st.f_bavail * st.f_frsize / 1e9, 1),
                "total_gb": round(st.f_blocks * st.f_frsize / 1e9, 1)}
    except OSError:
        disk = None
    gifs = _dir_size(STATE_DIR / "gifs", "*.gif")
    caps = _dir_size(STATE_DIR / "captures", "*")
    return {
        "os": os_name, "kernel": _read("/proc/sys/kernel/osrelease") or None,
        "uptime_hours": round(up / 3600, 1),
        "cpu": {"cores": os.cpu_count(), "busy_pct": _cpu_pct(),
                "load_avg": [float(x) for x in load] if load else None},
        "memory_mb": {"total": mem.get("MemTotal"), "available": mem.get("MemAvailable"),
                      "swap_used": (mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)) or 0},
        "temperatures_c": _temps(),
        "power": _power(),
        "disk": disk,
        "saved_gifs": {"count": gifs[0], "mb": round(gifs[1] / 1e6, 1)},
        "captures": {"count": caps[0], "mb": round(caps[1] / 1e6, 1)},
    }
