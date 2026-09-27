import asyncio
import os
import shlex
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.services.db import get_credentials, get_gateway

router = APIRouter(tags=["terminal"])

class TerminalCommand(BaseModel):
    command: str
    gateway_serial: str

@router.post("/terminal/execute")
async def execute_cli(req: TerminalCommand):
    cmd = req.command.strip()
    if not cmd:
        raise HTTPException(400, "Empty command")
        
    try:
        parts = shlex.split(cmd)
    except Exception as e:
        raise HTTPException(400, f"Invalid command syntax: {e}")
        
    base = parts[0]
    # If the user literally typed "franklinwh status" or "franklinwh-cli status", strip the binary prefix
    if base in ("franklinwh", "franklinwh-cli", "franklinwh_cli"):
        parts = parts[1:]
        if not parts:
            raise HTTPException(400, "Incomplete command")
        
    allowed_commands = {
        "status", "st", "discover", "disc", "mode", "tou", "raw", "metrics", 
        "monitor", "mon", "accessories", "acc", "sc", "smart-circuits", 
        "diag", "diagnostic", "bms", "battery", "support", "snapshot", "schema", "fetch"
    }
    
    command_verb = None
    for p in parts:
        if p in allowed_commands:
            command_verb = p
            break
            
    if not command_verb and not any(sw in parts for sw in ("--version", "-h", "--help")):
        raise HTTPException(400, "No permitted franklinwh-cli command found in input.")
        
    # Resolve short_id to full_serial
    gw_record = await get_gateway(req.gateway_serial)
    if not gw_record:
        raise HTTPException(400, "Unknown gateway selected.")
    full_serial = gw_record["full_serial"]

    # Inject credentials securely
    creds = await get_credentials(full_serial)
    if not creds:
        raise HTTPException(400, "No credentials found for this gateway. Please register them in the Gateways tab.")

    # Always prepend the true binary invocation and ensure credentials/gateway flags
    parts = [
        "franklinwh-cli",
        "--email", creds["email"],
        "--password", creds["password"],
        "--gateway", full_serial,
        "--no-color"
    ] + parts
        
    env = os.environ.copy()
    # We still keep these in ENV just in case the CLI checks them, but explicit args are safer
    env["FRANKLIN_USERNAME"] = creds["email"]
    env["FRANKLIN_PASSWORD"] = creds["password"]
    env["FRANKLIN_GATEWAY"] = full_serial
    env["NO_COLOR"] = "1"
    # ── Batch R follow-up (2026-08-02) — ResolvedCapabilities shim ─────
    # franklinwh_cloud/discovery.py evaluates a module-level return type
    # annotation (`-> ResolvedCapabilities:`) at import time, but the
    # class is only imported inside function scope in the current
    # upstream. FHAI's main process patches this via a builtins shim in
    # src/__init__.py, but subprocess.exec doesn't inherit builtins
    # mutations. The Terminal tab spawns franklinwh-cli as a subprocess,
    # so we inject the same shim via PYTHONPATH → sitecustomize.py so
    # Python's site machinery auto-imports it at interpreter startup.
    # See src/subprocess_shims/sitecustomize.py for the shim itself.
    _shim_dir = "/app/src/subprocess_shims"
    _existing_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{_shim_dir}:{_existing_path}" if _existing_path else _shim_dir

    try:
        proc = await asyncio.create_subprocess_exec(
            *parts,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env
        )
        
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=45.0)
            output = stdout.decode("utf-8", errors="replace")
        except asyncio.TimeoutError:
            proc.kill()
            output = "\n[Process Terminated: Timeout of 45s Exceeded. The command was hung or ran infinitely.]"
            
        return {"output": output, "exit_code": proc.returncode if proc.returncode is not None else 124}
        
    except FileNotFoundError:
        return {"output": "franklinwh-cli not found. Ensure franklinwh-cloud is installed.", "exit_code": 127}
    except Exception as e:
        return {"output": f"Execution failed: {e}", "exit_code": 1}
