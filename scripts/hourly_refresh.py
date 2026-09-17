"""Refresh and atomically publish all GP financial statements.

Designed for a silent hourly scheduler: success writes only the local status file;
failures are logged and re-raised so the scheduler can alert.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

try:
    from scripts.publish_financials import load_credentials, rpc
except ModuleNotFoundError:  # Direct execution from scripts/.
    from publish_financials import load_credentials, rpc

ROOT = Path(r"C:\Users\jonj\OneDrive\360\360 Solutions\Clients\360 Solutions LLC\A.I\GitHub\financial-statements")
PYTHON = sys.executable
SNAPSHOT = ROOT / "data" / "financial-statements.json"
CREDENTIALS = Path(r"C:\Users\jonj\AppData\Local\hermes\arcrm\credentials\current-ar.json")
STATUS_LOG = Path(r"C:\Users\jonj\AppData\Local\hermes\logs\financial-statements-refresh.json")
LOCK_FILE = Path(r"C:\Users\jonj\AppData\Local\hermes\locks\financial-statements-refresh.lock")
COMMAND_TIMEOUT_SECONDS = 1200
LOCK_STALE_SECONDS = 7200


def run_command(args: list[str]) -> str:
    result = subprocess.run(
        args,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "unknown failure").strip())
    return result.stdout.strip()


def _write_status(value: dict) -> None:
    STATUS_LOG.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATUS_LOG.with_suffix(STATUS_LOG.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(STATUS_LOG)


def _compact(payload: dict, keys: tuple[str, ...]) -> dict:
    return {key: payload[key] for key in keys if key in payload}


def read_current_hash() -> str | None:
    credentials = load_credentials(CREDENTIALS)
    result = rpc(
        credentials["supabase_url"],
        credentials["publishable_key"],
        credentials["operator_verification_key"],
        "financial_verify_current",
        {},
    )
    row = result[0] if isinstance(result, list) and result else result or {}
    if row.get("is_current") is not True:
        raise RuntimeError("current financial run could not be verified")
    value = row.get("source_sha256")
    return str(value) if value else None


def _acquire_lock() -> int | None:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        try:
            descriptor = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, f"pid={os.getpid()} at={dt.datetime.now(dt.timezone.utc).isoformat()}".encode())
            return descriptor
        except FileExistsError:
            try:
                age = dt.datetime.now().timestamp() - LOCK_FILE.stat().st_mtime
            except FileNotFoundError:
                continue
            if attempt == 0 and age > LOCK_STALE_SECONDS:
                LOCK_FILE.unlink(missing_ok=True)
                continue
            return None
    return None


def refresh(
    run_command: Callable[[list[str]], str] = run_command,
    current_hash_reader: Callable[[], str | None] = read_current_hash,
) -> bool:
    descriptor = _acquire_lock()
    if descriptor is None:
        return False
    try:
        extract_raw = run_command([
            PYTHON, "scripts/financial_sync.py", "--output", str(SNAPSHOT),
        ])
        extract = json.loads(extract_raw)
        source_hash = extract.get("source_sha256")
        if source_hash and current_hash_reader() == source_hash:
            _write_status({
                "ok": True,
                "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "extract": _compact(extract, (
                    "as_of", "company_count", "period_count", "source_sha256",
                    "mapping_version", "control_version",
                )),
                "publish": {
                    "source_sha256": source_hash,
                    "verified": True,
                    "unchanged": True,
                },
            })
            return True
        publish_raw = run_command([
            PYTHON, "scripts/publish_financials.py", "--snapshot", str(SNAPSHOT),
            "--credentials", str(CREDENTIALS),
        ])
        publish = json.loads(publish_raw)
        if publish.get("verified") is not True:
            raise RuntimeError("publisher did not return verified=true")
        _write_status({
            "ok": True,
            "at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "extract": _compact(extract, (
                "as_of", "company_count", "period_count", "source_sha256",
                "mapping_version", "control_version",
            )),
            "publish": _compact(publish, (
                "run_id", "source_sha256", "period_count", "verified",
            )),
        })
        return True
    except Exception as exc:
        _write_status({
            "ok": False,
            "at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "error": str(exc),
        })
        raise
    finally:
        os.close(descriptor)
        LOCK_FILE.unlink(missing_ok=True)


def main() -> int:
    refresh()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
