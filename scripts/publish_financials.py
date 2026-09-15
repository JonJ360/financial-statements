"""Validate, stage, promote, and verify a financial-statement snapshot."""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping

REQUIRED = (
    "supabase_url", "publishable_key", "current_ar_ingestion_key",
    "current_ar_promotion_key", "operator_verification_key",
)
ROLE_BY_KEY = {
    "current_ar_ingestion_key": "ar_current_ingest",
    "current_ar_promotion_key": "ar_current_promoter",
    "operator_verification_key": "ar_current_operator",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
META_KEYS = (
    "company_code", "company_name", "fiscal_year", "fiscal_period",
    "period_start", "period_end",
)


class CredentialError(RuntimeError):
    pass


def _jwt_role(token: str) -> str:
    """Read only the non-secret role claim; never return or log the JWT."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError
        raw = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")))
        role = claims.get("role")
        if not isinstance(role, str) or not role:
            raise ValueError
        return role
    except (ValueError, UnicodeError, json.JSONDecodeError, base64.binascii.Error) as exc:
        raise CredentialError("credential is not a JWT with a valid role claim") from exc


def validate_credentials(credentials: Mapping[str, str]) -> None:
    if "service_role_key" in credentials:
        raise CredentialError("service_role credentials are forbidden")
    missing = [key for key in REQUIRED if not isinstance(credentials.get(key), str) or not credentials[key]]
    if missing:
        raise CredentialError("missing credentials: " + ", ".join(missing))
    for key, expected_role in ROLE_BY_KEY.items():
        token = credentials[key]
        if _jwt_role(token) != expected_role:
            raise CredentialError(f"{key} has an unexpected JWT role claim")


def load_credentials(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if "service_role_key" in data:
        raise CredentialError("service_role credentials are forbidden")
    missing = [key for key in REQUIRED if not isinstance(data.get(key), str) or not data[key]]
    if missing:
        raise CredentialError("missing credentials: " + ", ".join(missing))
    result = {key: (data[key].rstrip("/") if key == "supabase_url" else data[key]) for key in REQUIRED}
    validate_credentials(result)
    return result


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _period_key(period: Mapping[str, Any]) -> tuple[str, int, int]:
    try:
        return (str(period["company_code"]), int(period["fiscal_year"]), int(period["fiscal_period"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("period or manifest entry has invalid coordinates") from exc


def _source_hash(periods: list[Mapping[str, Any]]) -> str:
    concatenated = "".join(str(period["payload_sha256"]) for period in sorted(periods, key=_period_key))
    return hashlib.sha256(concatenated.encode("utf-8")).hexdigest()


def validate_document(document: Mapping[str, Any]) -> tuple[Mapping[str, Any], list[Mapping[str, Any]]]:
    """Recompute every hash and reject malformed snapshots before network access."""
    if not isinstance(document, Mapping) or not isinstance(document.get("run"), Mapping):
        raise ValueError("snapshot run metadata is required")
    if not isinstance(document.get("periods"), list) or not document["periods"]:
        raise ValueError("snapshot periods must be a non-empty array")
    run = document["run"]
    periods = list(document["periods"])
    keys: list[tuple[str, int, int]] = []
    for period in periods:
        if not isinstance(period, Mapping):
            raise ValueError("period envelope must be an object")
        payload = period.get("payload")
        supplied_canonical = period.get("payload_canonical")
        if not isinstance(payload, Mapping) or not isinstance(supplied_canonical, str):
            raise ValueError("period payload and payload_canonical are required")
        computed_canonical = canonical_json(payload)
        if supplied_canonical != computed_canonical:
            raise ValueError("period payload canonical text does not match payload")
        computed_sha = hashlib.sha256(supplied_canonical.encode("utf-8")).hexdigest()
        if period.get("payload_sha256") != computed_sha:
            raise ValueError("period payload_sha256 does not match canonical payload")
        for metadata_key in META_KEYS:
            if period.get(metadata_key) != payload.get(metadata_key):
                raise ValueError(f"period envelope metadata mismatch: {metadata_key}")
        key = _period_key(period)
        if not key[0].strip() or not str(period["company_name"]).strip() or key[1] <= 0 or key[2] <= 0:
            raise ValueError("period company and coordinates must be non-empty and positive")
        try:
            period_start = dt.date.fromisoformat(str(period["period_start"]))
            period_end = dt.date.fromisoformat(str(period["period_end"]))
        except ValueError as exc:
            raise ValueError("period dates must be ISO dates") from exc
        if period_end < period_start:
            raise ValueError("period_end must not precede period_start")
        keys.append(key)
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate period coordinates in snapshot input")

    manifest = run.get("manifest")
    if not isinstance(manifest, list) or not manifest:
        raise ValueError("run manifest must be a non-empty array")
    manifest_keys = [_period_key(item) for item in manifest if isinstance(item, Mapping)]
    if len(manifest_keys) != len(manifest) or len(manifest_keys) != len(set(manifest_keys)):
        raise ValueError("duplicate or invalid run manifest coordinates")
    if set(manifest_keys) != set(keys):
        raise ValueError("run manifest does not exactly match period coordinates")
    company_count = len({key[0] for key in keys})
    if int(run.get("company_count", -1)) != company_count:
        raise ValueError("run company_count does not match periods")
    if int(run.get("period_count", -1)) != len(periods):
        raise ValueError("run period_count does not match periods")
    source_sha = run.get("source_sha256")
    if not isinstance(source_sha, str) or not SHA256_RE.fullmatch(source_sha) or source_sha != _source_hash(periods):
        raise ValueError("run source_sha256 does not match ordered period payload hashes")
    if not isinstance(run.get("as_of"), str):
        raise ValueError("run as_of is required")
    return run, periods


def rpc(base: str, publishable: str, token: str, name: str, payload: dict[str, Any]) -> Any:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    for attempt in range(3):
        request = urllib.request.Request(
            f"{base}/rest/v1/rpc/{name}", data=body, method="POST",
            headers={"apikey": publishable, "Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            response_body = exc.read().decode(errors="replace")[:500]
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise RuntimeError(f"{name} failed: HTTP {exc.code} {response_body}") from None
        except (TimeoutError, urllib.error.URLError):
            if attempt == 2:
                raise RuntimeError(f"{name} failed after retries") from None
        time.sleep(2 ** attempt)
    raise RuntimeError(f"{name} failed without response")


def publish(
    document: Mapping[str, Any], credentials: Mapping[str, str], *,
    rpc_call: Callable[..., Any] = rpc, batch_size: int = 25,
) -> dict[str, Any]:
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
        raise ValueError("batch_size must be a positive integer")
    validate_credentials(credentials)
    run, periods = validate_document(document)
    base, key = credentials["supabase_url"], credentials["publishable_key"]
    ingest = credentials["current_ar_ingestion_key"]
    run_id = rpc_call(base, key, ingest, "financial_stage_run", {
        "p_source_sha256": run["source_sha256"], "p_as_of": run["as_of"],
        "p_company_count": run["company_count"], "p_period_count": run["period_count"],
        "p_manifest": run["manifest"],
    })
    if not run_id:
        raise RuntimeError("financial_stage_run returned no run ID")
    for offset in range(0, len(periods), batch_size):
        rpc_call(base, key, ingest, "financial_stage_period_batch", {
            "p_run_id": run_id, "p_periods": periods[offset:offset + batch_size],
        })
    rpc_call(base, key, ingest, "financial_validate_run", {"p_run_id": run_id})
    rpc_call(base, key, credentials["current_ar_promotion_key"], "financial_promote_run", {"p_run_id": run_id})
    result = rpc_call(base, key, credentials["operator_verification_key"], "financial_verify_current", {})
    row = result[0] if isinstance(result, list) and result else result or {}
    if str(row.get("run_id")) != str(run_id) or row.get("source_sha256") != run["source_sha256"] or row.get("is_current") is not True:
        raise RuntimeError("promoted run hash/current-pointer verification failed")
    if int(row.get("period_count", -1)) != int(run["period_count"]):
        raise RuntimeError("promoted run period_count verification failed")
    return {
        "run_id": str(run_id), "source_sha256": run["source_sha256"],
        "period_count": int(run["period_count"]), "verified": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=Path("data/financial-statements.json"))
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=25)
    args = parser.parse_args(argv)
    document = json.loads(args.snapshot.read_text(encoding="utf-8"))
    result = publish(document, load_credentials(args.credentials), batch_size=args.batch_size)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
