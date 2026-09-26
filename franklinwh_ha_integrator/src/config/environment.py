"""Environment detection — ha_addon | docker | dev"""
import os
from pathlib import Path
from typing import Literal

Environment = Literal["ha_addon", "docker", "dev"]


def detect_environment() -> Environment:
    """
    Detect which runtime environment we're in using structured heuristics.
    
    Priority order:
      1. ha_addon  — Supervisor tokens or known addon paths (e.g. /data/options.json)
      2. docker    — .dockerenv or container hooks within cgroups/mountinfo
      3. dev       — Bare metal / local native OS testing
    """
    env = os.environ.get("APP_ENV")
    if env in ("ha_addon", "docker", "dev"):
        return env

    # ─── 1. Check HA Addon ───
    ha_indicators = ["SUPERVISOR_TOKEN", "HASSIO_TOKEN", "SUPERVISOR_API"]
    if any(os.environ.get(var) for var in ha_indicators):
        return "ha_addon"

    ha_paths = ["/data/options.json", "/var/run/secrets/hassio"]
    if any(Path(p).exists() for p in ha_paths):
        return "ha_addon"

    # ─── 2. Check Docker (Standalone) ───
    if Path("/.dockerenv").exists():
        return "docker"
        
    try:
        if Path("/proc/1/cgroup").exists():
            with open("/proc/1/cgroup", "r") as f:
                content = f.read().lower()
                if any(x in content for x in ("docker", "containerd", "kubepods")):
                    return "docker"
    except Exception:
        pass

    try:
        if Path("/proc/self/mountinfo").exists():
            with open("/proc/self/mountinfo", "r") as f:
                if "docker" in f.read().lower():
                    return "docker"
    except Exception:
        pass

    # ─── 3. Bare Metal / Dev ───
    return "dev"


def get_data_dir() -> Path:
    """Return the data directory for the current environment."""
    env = detect_environment()
    if env in ("ha_addon", "docker"):
        return Path("/data")
    return Path("./data")
