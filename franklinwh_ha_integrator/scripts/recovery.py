#!/usr/bin/env python3
"""Headless break-glass recovery / security control for FHAI (BKL-SEC-01).

Operates directly on SQLite, bypassing the application layer, so it works when
the UI is unreachable.

    python scripts/recovery.py status
    python scripts/recovery.py disable-security
    python scripts/recovery.py enable-security [--force]

⚠️  RUN THIS WHERE THE APP RUNS.

`get_data_dir()` resolves relative to the process, so on a Docker install a
host-side run edits a *different* database than the container is using and
reports success while the running app is unaffected. Verified on a live
install: host ./data/config.db had 1 user, the container's /data/config.db had
4. For Docker:

    docker exec <container> python /app/scripts/recovery.py <command>

`status` prints the resolved path and account count precisely so this mistake
is visible rather than silent.
"""
import sys
import sqlite3
import asyncio
import json
import os
from pathlib import Path

# Add project root to path so we can import services/config
sys.path.append(str(Path(__file__).parent.parent.absolute()))

from src.config.environment import get_data_dir
from src.services.security_checker import compute_security_snapshot_hash

SECURITY_KEYS = ("security_enabled", "tls_enabled", "mtls_enabled")


def _resolve_db() -> Path:
    """Resolve the config DB, failing loudly and flagging a likely wrong-host run."""
    db_path = (get_data_dir() / "config.db").resolve()

    if not db_path.exists():
        print(f"❌ SQLite database not found at {db_path}", file=sys.stderr)
        print("   If FHAI runs in Docker, run this INSIDE the container:", file=sys.stderr)
        print("     docker exec <container> python /app/scripts/recovery.py <command>", file=sys.stderr)
        sys.exit(1)

    in_container = Path("/.dockerenv").exists() or os.environ.get("FHAI_IN_CONTAINER")
    print(f"🔍 Database: {db_path}")
    if not in_container:
        print("⚠️  Not running inside a container. If FHAI runs in Docker this is")
        print("   probably the WRONG database — the container has its own volume.")
        print("   Check the account count below matches what you expect.")
    return db_path


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _get_cfg(conn: sqlite3.Connection, key: str):
    """Read an app_config value, decoding the JSON the app layer writes.

    The app stores every value through json.dumps (db.set_config_value), so a
    raw read yields '"true"', not 'true'. Rows written raw by older versions of
    this script are returned unchanged so status still reads a legacy database.
    """
    try:
        row = conn.execute("SELECT value FROM app_config WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return row["value"]
    except sqlite3.Error:
        return None


def _get_flag(conn: sqlite3.Connection, key: str) -> bool:
    """Read a boolean app_config flag, tolerating every encoding in the wild.

    Values reach this table as real JSON booleans (app layer), as the strings
    "true"/"false" (older versions of this script), or as 0/1 — so normalise
    rather than assuming any one of them.
    """
    val = _get_cfg(conn, key)
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    return str(val).strip().lower() in ("true", "1", "yes")


def _users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    try:
        return list(conn.execute(
            "SELECT username, role, dashboard, must_change_pw, totp_enabled FROM users ORDER BY username"
        ))
    except sqlite3.Error:
        return []


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS security_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            event TEXT NOT NULL,
            source TEXT NOT NULL,
            detail TEXT NOT NULL
        )
    """)


async def _reseal(conn: sqlite3.Connection, db_path: Path, event: str, detail: str) -> str:
    """Recompute the security snapshot and append an audit entry.

    Without the reseal the next startup sees a changed config and raises a
    tamper alarm for a change the operator made deliberately.
    """
    print("🔑 Computing fresh security snapshot fingerprint...")
    from src.services.db import set_db_path
    set_db_path(db_path)
    new_hash = await compute_security_snapshot_hash()
    # json.dumps is mandatory: db.get_config_value() runs json.loads() on every
    # value it reads, so a bare hex digest raises JSONDecodeError and takes the
    # whole startup tamper check down with it — silently disabling the check
    # this script exists to keep honest. Cost a live install 11 days of a dead
    # SECURITY_TAMPER_ALERT before anyone noticed (2026-08-18 → 2026-08-29).
    conn.execute(
        "INSERT OR REPLACE INTO app_config (key, value) VALUES ('last_security_snapshot_hash', ?)",
        (json.dumps(new_hash),),
    )
    conn.execute(
        "INSERT INTO security_audit_log (event, source, detail) VALUES (?, ?, ?)",
        (event, "recovery_script", f"{detail} Snapshot signature: {new_hash[:16]}"),
    )
    conn.commit()
    return new_hash


# ── status ───────────────────────────────────────────────────────────────────

async def status() -> None:
    db_path = _resolve_db()
    conn = _connect(db_path)

    print("\n── Security ─────────────────────────────────────────")
    enabled = _get_flag(conn, "security_enabled")
    for key in SECURITY_KEYS:
        print(f"  {key:<20} {str(_get_flag(conn, key)).lower()}")
    print(f"  {'jwt_secret_key':<20} {'set' if _get_cfg(conn, 'jwt_secret_key') else 'not set'}")

    env_override = os.environ.get("FWH_DISABLE_SECURITY", "").lower() in ("true", "1", "yes")
    if env_override:
        print("  FWH_DISABLE_SECURITY is set — auth is forced OFF regardless of the above")

    users = _users(conn)
    print(f"\n── Accounts ({len(users)}) ──────────────────────────────────")
    if not users:
        print("  (none — enabling security would lock everyone out)")
    for u in users:
        flags = []
        if u["must_change_pw"]:
            flags.append("must_change_pw")
        if u["totp_enabled"]:
            flags.append("totp")
        print(f"  {u['username']:<26} role={u['role']:<10} dashboard={u['dashboard']:<9} "
              f"{' '.join(flags)}")

    print("\n── Effective posture ────────────────────────────────")
    if env_override or not enabled:
        print("  🔓 OPEN — every route is reachable without credentials.")
    else:
        print("  🔒 ENFORCED — a valid session or API token is required.")

    # Roles ARE enforced as of Stage 1 (router-level tiers), but only once
    # authentication is on — with security disabled the middleware bypasses
    # every request before authorization is ever consulted. Distinguish the two
    # so the reason for open access is accurate.
    non_admin = [u["username"] for u in users if u["role"] != "admin"]
    if non_admin:
        if env_override or not enabled:
            print(f"  ⚠️  Roles are enforced, but authentication is OFF — so these")
            print(f"     non-admin accounts are moot; every request bypasses auth")
            print(f"     entirely: {', '.join(non_admin)}")
        else:
            print(f"  Non-admin accounts (restricted): {', '.join(non_admin)}")

    unrotated = [u["username"] for u in users if u["role"] == "admin" and u["must_change_pw"]]
    if unrotated:
        print(f"  ⚠️  Admin account(s) still on the generated startup password: {', '.join(unrotated)}")
        print(f"     That one-time password was printed to the log at first boot. If it is")
        print(f"     lost, nobody can log in — do not enable security until it is changed.")

    conn.close()


# ── disable ──────────────────────────────────────────────────────────────────

async def disable_security() -> None:
    db_path = _resolve_db()
    conn = _connect(db_path)
    _ensure_tables(conn)

    for key in SECURITY_KEYS:
        conn.execute(
            "INSERT OR REPLACE INTO app_config (key, value) VALUES (?, ?)",
            (key, json.dumps(False)),
        )
    conn.commit()
    print("🔓 security_enabled, tls_enabled and mtls_enabled set to false.")

    new_hash = await _reseal(conn, db_path, "BREAK_GLASS_DEACTIVATION",
                             "Headless emergency bypass executed.")
    conn.close()
    print(f"✅ Recovery complete. Snapshot: {new_hash[:16]}")
    print("   Restart the app for the change to take effect.")


# ── enable ───────────────────────────────────────────────────────────────────

async def enable_security(force: bool = False) -> None:
    db_path = _resolve_db()
    conn = _connect(db_path)
    _ensure_tables(conn)

    # Pre-flight. Enabling auth with no usable admin credential locks the
    # operator out of their own energy system, and the recovery path for that
    # is this same script — which they may not be able to reach easily on a
    # remote or Add-on install.
    problems: list[str] = []
    users = _users(conn)
    admins = [u for u in users if u["role"] == "admin"]

    if not users:
        problems.append("no user accounts exist at all")
    elif not admins:
        problems.append("no account has role='admin'")
    elif all(u["must_change_pw"] for u in admins):
        problems.append(
            "every admin is still on the generated startup password "
            f"({', '.join(u['username'] for u in admins)}) — if that one-time "
            "password is lost, enabling security locks you out"
        )

    if problems:
        print("\n⛔ Refusing to enable security:")
        for p in problems:
            print(f"   • {p}")
        print("\n   Fix first — with security still OFF you can set a password via:")
        print("     curl -X POST localhost:8099/api/security/users/admin/change-password \\")
        print("          -H 'Content-Type: application/json' -d '{\"new_password\":\"...\"}'")
        print("   then verify it before enabling:")
        print("     curl -X POST localhost:8099/api/security/login \\")
        print("          -H 'Content-Type: application/json' -d '{\"username\":\"admin\",\"password\":\"...\"}'")
        if not force:
            print("\n   Override with --force if you are certain.")
            conn.close()
            sys.exit(2)
        print("\n   --force given — proceeding anyway.")

    conn.execute(
        "INSERT OR REPLACE INTO app_config (key, value) VALUES (?, ?)",
        ("security_enabled", json.dumps(True)),
    )
    conn.commit()
    print("🔒 security_enabled set to true. (TLS/mTLS left unchanged.)")

    new_hash = await _reseal(conn, db_path, "SECURITY_ENABLED_VIA_RECOVERY",
                             "Security enabled from the recovery script.")
    conn.close()

    print(f"✅ Done. Snapshot: {new_hash[:16]}")
    print("   Restart the app for the change to take effect.")
    print("\n   If you get locked out, either:")
    print("     docker exec <container> python /app/scripts/recovery.py disable-security")
    print("   or set FWH_DISABLE_SECURITY=true in the environment and restart.")


async def clear_mfa(username: str | None = None) -> None:
    """Drop a user's MFA enrolment. The other half of break-glass.

    `reset-admin-password` restores the password and nothing else, so on an
    account with TOTP enabled it produced a login that still demanded a code
    from a device that may be at the bottom of a river. The docstring there
    promised "you can never be permanently locked out"; with MFA on, that was
    not true, and this is the command that makes it true again.

    The secret is cleared along with the flag. Leaving a stale secret would let
    the old device keep generating valid codes for a re-enrolment that the user
    believes is fresh.
    """
    if not username:
        print("clear-mfa requires --username", file=sys.stderr)
        sys.exit(1)

    db_path = _resolve_db()
    print(f"🔍 Database: {db_path}")
    conn = _connect(db_path)
    _ensure_tables(conn)

    row = conn.execute(
        "SELECT username, totp_enabled, totp_secret FROM users WHERE username = ?",
        (username,),
    ).fetchone()

    if not row:
        print(f"❌ No such account: {username!r}", file=sys.stderr)
        print("   Run `status` to list accounts.", file=sys.stderr)
        conn.close()
        sys.exit(1)

    if not row["totp_enabled"] and not row["totp_secret"]:
        print(f"✅ {username!r} has no MFA enrolment — nothing to clear.")
        conn.close()
        return

    conn.execute(
        "UPDATE users SET totp_enabled = 0, totp_secret = NULL, "
        "updated_at = datetime('now') WHERE username = ?",
        (username,),
    )
    conn.commit()

    print(f"✅ MFA cleared for {username!r}.")
    print("   They sign in with a password alone until they enrol again.")

    # Reseal, or the next start raises a tamper alarm for a change we just
    # made on purpose.
    await _reseal(conn, db_path, "TOTP_CLEARED_BY_RECOVERY",
                  f"MFA enrolment cleared for {username} via recovery CLI")
    conn.close()


async def reset_admin_password(username: str | None = None, password: str | None = None,
                               clear_mfa_too: bool = False) -> None:
    """Restore admin access unconditionally — the true anti-lockout path.

    Neither existing recovery procedure resets a credential: both
    `disable-security` and FWH_DISABLE_SECURITY merely turn authentication OFF,
    which gets you in but leaves the instance unsecured until you notice. The
    guide has always claimed §7 covers "a forgotten password"; this is the
    command that actually does it.

    It cannot fail to produce a usable login:
      * named account missing        -> created, as admin
      * account exists but not admin -> promoted
      * no password supplied         -> a strong one is generated and printed
      * MFA enrolled                 -> --clear-mfa drops it; without the flag
                                        the reset still succeeds but login will
                                        demand a code, and we say so loudly

    must_change_pw is cleared deliberately. `enable-security` refuses while
    every admin still carries the generated first-boot password, so leaving the
    flag set would reset the credential and then block the very next step.
    """
    import secrets as _secrets
    from src.services.crypto import hash_password

    db_path = _resolve_db()
    conn = _connect(db_path)
    _ensure_tables(conn)

    username = username or (_get_cfg(conn, "admin_username") or "admin")
    generated = password is None
    if generated:
        password = "fhai-" + _secrets.token_urlsafe(12)
    elif len(password) < 8:
        print("❌ Password must be at least 8 characters.", file=sys.stderr)
        conn.close()
        sys.exit(1)

    hashed = hash_password(password)
    row = conn.execute(
        "SELECT username, role, totp_enabled FROM users WHERE username = ?", (username,)
    ).fetchone()

    had_mfa = bool(row["totp_enabled"]) if row else False

    if row:
        if clear_mfa_too:
            conn.execute(
                "UPDATE users SET password_hash = ?, role = 'admin', must_change_pw = 0, "
                "totp_enabled = 0, totp_secret = NULL, "
                "updated_at = datetime('now') WHERE username = ?",
                (hashed, username),
            )
        else:
            conn.execute(
                "UPDATE users SET password_hash = ?, role = 'admin', must_change_pw = 0, "
                "updated_at = datetime('now') WHERE username = ?",
                (hashed, username),
            )
        action = "reset" if row["role"] == "admin" else f"reset and promoted from {row['role']!r}"
    else:
        conn.execute(
            "INSERT INTO users (username, password_hash, role, dashboard, must_change_pw) "
            "VALUES (?, ?, 'admin', 'standard', 0)",
            (username, hashed),
        )
        action = "created"
    conn.commit()

    await _reseal(conn, db_path, "ADMIN_PASSWORD_RESET",
                  f"Admin credential {action} for {username!r} via recovery script.")
    conn.close()

    print(f"✅ Admin account {username!r} {action}.")
    if generated:
        print("\n" + "*" * 62)
        print("  NEW ADMIN PASSWORD — shown once, store it now")
        print(f"  USERNAME: {username}")
        print(f"  PASSWORD: {password}")
        print("*" * 62 + "\n")
    if had_mfa and clear_mfa_too:
        print("   MFA enrolment was also cleared — password only at next sign-in.")
    elif had_mfa:
        # The reset worked and is still not enough. Saying so here is the whole
        # point: the failure is otherwise discovered at the login screen, by
        # someone who has just been told their access was restored.
        print("\n" + "!" * 62)
        print("  MFA IS STILL ENABLED ON THIS ACCOUNT")
        print("  The new password alone will NOT get you in — sign-in will ask")
        print("  for a code from the authenticator. If that device is lost, run:")
        print(f"    python scripts/recovery.py clear-mfa --username {username}")
        print("!" * 62 + "\n")

    print("   Verify before relying on it (works with security still off):")
    print(f"     curl -X POST localhost:8099/api/security/login \\")
    print(f"          -H 'Content-Type: application/json' \\")
    print(f"          -d '{{\"username\":\"{username}\",\"password\":\"...\"}}'")


USAGE = """Usage: python scripts/recovery.py <command>

  status                    show security posture and accounts (read-only)
  disable-security          break-glass: force auth off
  enable-security [--force] turn auth on, with lockout pre-flight
  reset-admin-password [--username U] [--password P] [--clear-mfa]
                            restore admin access; creates or promotes the
                            account if needed, generates a password if none
                            is given. Add --clear-mfa if the authenticator is
                            also lost — a password reset alone does NOT clear
                            MFA, and login will still demand a code.
  clear-mfa --username U    drop MFA enrolment for one account, leaving the
                            password alone. For a lost or wiped authenticator.

In Docker, run inside the container:
  docker exec <container> python /app/scripts/recovery.py status
"""


def _flag(name: str) -> str | None:
    """Read `--name value` from argv, or None."""
    args = sys.argv[2:]
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def main() -> None:
    if len(sys.argv) < 2:
        print(USAGE, file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]
    force = "--force" in sys.argv[2:]

    if cmd == "status":
        asyncio.run(status())
    elif cmd == "disable-security":
        asyncio.run(disable_security())
    elif cmd == "enable-security":
        asyncio.run(enable_security(force=force))
    elif cmd == "reset-admin-password":
        asyncio.run(reset_admin_password(
            _flag("--username"), _flag("--password"),
            clear_mfa_too="--clear-mfa" in sys.argv[2:],
        ))
    elif cmd == "clear-mfa":
        asyncio.run(clear_mfa(_flag("--username")))
    else:
        print(f"Unknown command: {cmd}\n", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
