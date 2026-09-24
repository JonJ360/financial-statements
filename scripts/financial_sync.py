"""Read-only multi-company Dynamics GP financial-statement extraction."""
from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import decimal
import hashlib
import ipaddress
import json
import os
import re
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

D = decimal.Decimal
ZERO = D("0.00000")
CONTROL_TOLERANCE = D("0.01")
MIN_FISCAL_YEAR = 2020
MAPPING_VERSION = "3.1.0"
CONTROL_VERSION = "1.1.0"
MAIN_SERVER = "192.168.1.25,49934"
SMI_SERVER = "192.168.1.25,50497"
CREDENTIAL_TARGET = "Hermes/ARCRM/SMI-SQL"


@dataclass(frozen=True)
class CompanyConfig:
    code: str
    name: str
    server: str
    database: str


_COMPANY_NAMES = {
    "BBMTS": "Butcher Block Meats LLC", "DMERC": "Dilworth Mercantile",
    "FFCAP": "Fourth Fargo Capital LLC", "FTSOL": "Four Tine Solution LLC",
    "LPPAR": "LP Parkers House LLC", "MILT": "Milton LLC (The Milton)",
    "OLH": "Opatril Land Holdings LLC", "OPAT": "Opatril Properties, LLC",
    "OZAK1": "OZ Alaska 1 LLC", "OZDEV": "OZ Development LLC",
    "RDSC": "Rail District Sign Company LLC", "REB": "360 Sales LLC",
    "RRCOF": "Roasted Rail Coffee House, LLC", "RSS": "Real Steel Solutions LLC",
    "SEAMX": "Sea Max LLC", "SFAB": "Structural Fab LLC", "SOL": "360 Solutions LLC",
    "SPFAR": "Structural Properties, LLP (Fargo)", "UNIFO": "Uniform Unit — Dickinson",
    "UUFGO": "Uniform Unit — Fargo", "VGA": "Vintage Garden LLC",
    "VPENG": "Ope It's Cold, LLC dba Vampire Penguin", "SMI": "SMI",
}
ACTIVE_COMPANIES = tuple(
    CompanyConfig(code, name, SMI_SERVER if code == "SMI" else MAIN_SERVER, code)
    for code, name in _COMPANY_NAMES.items()
)

BS_CATEGORIES = frozenset({1, 3, 4, 5, 7, 9, 10, 11, 12, 13, 14, 16, 23, 25, 27, 30})
IS_CATEGORIES = frozenset({31, 32, 33, 35, 36, 37, 38, 39, 40, 42, 43})
NONFINANCIAL_CATEGORIES = frozenset({48})
EMPLOYEE_TERMS = (
    "PAYROLL", "FICA", "WAGES", "SALARIES", "WORKERS COMP", "WORKER'S COMP",
    "UNEMPLOYMENT", "PROFIT SHARING", "OFFICE STAFF", "HUMAN RESOURCES",
    "HEALTH INSURANCE",
)
MAPPING_SPEC = {
    "balance_sheet_categories": sorted(BS_CATEGORIES),
    "income_statement_categories": sorted(IS_CATEGORIES),
    "nonfinancial_categories": sorted(NONFINANCIAL_CATEGORIES),
    "balance_sections": {
        "cash": [1], "current_assets": [3, 4, 5, 7],
        "property_plant_equipment": [9], "accumulated_depreciation": [10],
        "intangible_assets": [11], "other_assets": [12],
        "current_liabilities": [13, 16], "notes_payable": [14],
        "equity": [23, 25, 27, 30],
    },
    "balance_description_rules": {
        "notes_payable_officer": "current_liabilities",
        "notes_payable_bank": "long_term_liabilities",
        "unrealized_gain": "equity",
    },
    "income_sections": {
        "revenue": [31, 32], "cost_of_revenue": [33],
        "employee_related_expenses": [36, 37],
        "other_operating_expenses": [35, 40], "other_expense": [38],
        "income_tax": [39], "other_income": [43],
    },
    "income_description_rules": {
        "discounts_given": "cost_of_revenue",
        "category_39_income_tax_description": "income_tax",
        "category_39_employee_terms": list(EMPLOYEE_TERMS),
        "category_39_default": "other_operating_expenses",
        "category_37_draw_or_distribution": "equity_distribution",
        "category_42_income_tax_description": "income_tax",
        "category_42_employee_terms": list(EMPLOYEE_TERMS),
        "category_42_gain_or_loss_on_sale": "other_expense",
        "category_42_default": "other_operating_expenses",
    },
    "account_display_amount": "SQL ytd totals already include period-zero opening; balance-sheet debit-less-credit for assets and credit-less-debit for liabilities/equity",
    "synthetic_current_earnings": "income-statement SQL ytd debit-less-credit, excluding equity distributions, reversed into equity display sign; SQL ytd already includes period-zero opening",
    "synthetic_equity_distributions": "category 37 draw/distribution SQL balances reclassified out of net income and into equity",
    "signs": {
        "revenue_and_other_income": "credit_less_debit",
        "expenses": "debit_less_credit",
        "assets": "debit_less_credit",
        "liabilities_and_equity": "credit_less_debit",
    },
    "smi_consolidation": "first_hyphen_delimited_account_segment",
}
CONTROL_SPEC = {
    "tolerance": str(CONTROL_TOLERANCE),
    "posting_type_enum": [0, 1],
    "typical_balance_enum": [0, 1],
    "checks": [
        "summary_detail", "debits_credits", "balance_sheet_plus_pl_ytd",
        "balance_sheet_equation", "duplicate_account_index", "nonfinancial_zero",
        "posting_category", "unmapped_nonzero", "kpi_statement_bridge", "period_coverage",
    ],
}


class SourceValidationError(RuntimeError):
    pass


class CredentialError(RuntimeError):
    pass


class _CredentialW(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]


def read_windows_credential(target: str = CREDENTIAL_TARGET) -> tuple[str, str]:
    if os.name != "nt":
        raise CredentialError("Windows Credential Manager is required")
    api = ctypes.WinDLL("Advapi32.dll")
    pointer = ctypes.POINTER(_CredentialW)()
    if not api.CredReadW(target, 1, 0, ctypes.byref(pointer)):
        raise CredentialError(f"credential not found: {target}")
    try:
        credential = pointer.contents
        username = credential.UserName or ""
        password = ctypes.wstring_at(credential.CredentialBlob, credential.CredentialBlobSize // 2)
    finally:
        api.CredFree(pointer)
    if not username or not password:
        raise CredentialError("credential is incomplete")
    return username, password


def connect_company(company: CompanyConfig, target: str = CREDENTIAL_TARGET) -> Any:
    if company not in ACTIVE_COMPANIES:
        raise SourceValidationError("company is not allowlisted")
    username, password = read_windows_credential(target)
    try:
        import pyodbc
        installed = pyodbc.drivers()
        if "ODBC Driver 18 for SQL Server" in installed:
            driver = "ODBC Driver 18 for SQL Server"
            transport = "Encrypt=yes;TrustServerCertificate=yes;"
        else:
            # Match the established read-only SQL viewer on the private GP LAN.
            host = company.server.split(",", 1)[0]
            if not ipaddress.ip_address(host).is_private or "SQL Server" not in installed:
                raise SourceValidationError(
                    "encrypted ODBC is unavailable and legacy access is restricted to a private SQL host"
                )
            driver = "SQL Server"
            transport = "Encrypt=no;TrustServerCertificate=yes;"
        return pyodbc.connect(
            f"DRIVER={{{driver}}};" f"SERVER={company.server};DATABASE={company.database};"
            f"UID={username};PWD={password};{transport}"
            "APP=Hermes GP Financial Statements Read Only;ApplicationIntent=ReadOnly;",
            timeout=15,
        )
    finally:
        password = ""


CALENDAR_SQL = """
WITH origin_checks AS (
  SELECT p.YEAR1, p.PERIODID, COUNT(*) origin_row_count,
         SUM(CASE WHEN o.CLOSED IS NULL OR o.CLOSED NOT IN (0,1)
                       OR o.SERIES NOT BETWEEN 2 AND 7 THEN 1 ELSE 0 END) origin_invalid_count,
         SUM(CASE WHEN o.CLOSED <> CASE o.SERIES
             WHEN 2 THEN p.PSERIES_1 WHEN 3 THEN p.PSERIES_2
             WHEN 4 THEN p.PSERIES_3 WHEN 5 THEN p.PSERIES_4
             WHEN 6 THEN p.PSERIES_5 WHEN 7 THEN p.PSERIES_6
             END THEN 1 ELSE 0 END) origin_mismatch_count
  FROM dbo.SY40100 p
  JOIN dbo.SY40100 o ON o.YEAR1=p.YEAR1 AND o.PERIODID=p.PERIODID AND o.SERIES<>0
  WHERE p.SERIES=0 AND p.PERIODID>0
  GROUP BY p.YEAR1,p.PERIODID
)
SELECT CAST(p.YEAR1 AS int) fiscal_year, CAST(p.PERIODID AS int) fiscal_period,
       CAST(p.PERIODDT AS date) period_start, CAST(p.PERDENDT AS date) period_end,
       p.PSERIES_1,p.PSERIES_2,p.PSERIES_3,p.PSERIES_4,p.PSERIES_5,p.PSERIES_6,
       COALESCE(o.origin_row_count,0) origin_row_count,
       COALESCE(o.origin_invalid_count,0) origin_invalid_count,
       COALESCE(o.origin_mismatch_count,0) origin_mismatch_count
FROM dbo.SY40100 p
JOIN dbo.SY40101 y ON y.YEAR1=p.YEAR1
LEFT JOIN origin_checks o ON o.YEAR1=p.YEAR1 AND o.PERIODID=p.PERIODID
WHERE p.PERIODID > 0 AND p.SERIES=0
  AND p.PERIODDT >= y.FSTFSCDY AND p.PERDENDT <= y.LSTFSCDY
ORDER BY fiscal_year, fiscal_period
"""

ACCOUNT_SQL = """
WITH summary_rows AS (
  SELECT s.ACTINDX, s.YEAR1, s.PERIODID, s.DEBITAMT, s.CRDTAMNT
  FROM dbo.GL10110 s WHERE s.Ledger_ID=1
  UNION ALL
  SELECT h.ACTINDX, h.YEAR1, h.PERIODID, h.DEBITAMT, h.CRDTAMNT
  FROM dbo.GL10111 h
  WHERE h.Ledger_ID=1 AND NOT EXISTS (
    SELECT 1 FROM dbo.GL10110 s
    WHERE s.ACTINDX=h.ACTINDX AND s.YEAR1=h.YEAR1
      AND s.PERIODID=h.PERIODID AND s.Ledger_ID=1
  )
), summary AS (
  SELECT ACTINDX, YEAR1, PERIODID, SUM(DEBITAMT) debit, SUM(CRDTAMNT) credit
  FROM summary_rows GROUP BY ACTINDX, YEAR1, PERIODID
), opening AS (
  SELECT ACTINDX, YEAR1, SUM(debit) opening_debit, SUM(credit) opening_credit
  FROM summary WHERE PERIODID=0 GROUP BY ACTINDX,YEAR1
), detail AS (
  SELECT ACTINDX, YEAR1, PERIODID, SUM(DEBITAMT) debit, SUM(CRDTAMNT) credit
  FROM (
    SELECT ACTINDX,OPENYEAR YEAR1,PERIODID,DEBITAMT,CRDTAMNT FROM dbo.GL20000 WHERE Ledger_ID=1
    UNION ALL
    SELECT ACTINDX,HSTYEAR YEAR1,PERIODID,DEBITAMT,CRDTAMNT FROM dbo.GL30000 WHERE Ledger_ID=1
  ) source_detail WHERE PERIODID>0
  GROUP BY ACTINDX,YEAR1,PERIODID
), periods AS (
  SELECT p.YEAR1,p.PERIODID FROM dbo.SY40100 p
  JOIN dbo.SY40101 y ON y.YEAR1=p.YEAR1
  WHERE p.PERIODID>0 AND p.SERIES=0
    AND p.PERIODDT>=y.FSTFSCDY AND p.PERDENDT<=y.LSTFSCDY
)
SELECT a.ACTINDX account_index, LTRIM(RTRIM(n.ACTNUMST)) account_number,
       LTRIM(RTRIM(a.ACTDESCR)) account_description, CAST(a.PSTNGTYP AS int) posting_type,
       CAST(a.TPCLBLNC AS int) typical_balance,
       CAST(a.ACCATNUM AS int) category, LTRIM(RTRIM(cat.ACCATDSC)) category_description,
       CAST(p.YEAR1 AS int) fiscal_year, CAST(p.PERIODID AS int) fiscal_period,
       COALESCE(s.debit,0) period_debit, COALESCE(s.credit,0) period_credit,
       COALESCE(o.opening_debit,0) opening_debit, COALESCE(o.opening_credit,0) opening_credit,
       COALESCE(o.opening_debit,0)+SUM(COALESCE(s.debit,0)) OVER (PARTITION BY a.ACTINDX,p.YEAR1 ORDER BY p.PERIODID) ytd_debit,
       COALESCE(o.opening_credit,0)+SUM(COALESCE(s.credit,0)) OVER (PARTITION BY a.ACTINDX,p.YEAR1 ORDER BY p.PERIODID) ytd_credit,
       COALESCE(d.debit,0) detail_period_debit, COALESCE(d.credit,0) detail_period_credit
FROM dbo.GL00100 a
JOIN dbo.GL00105 n ON n.ACTINDX=a.ACTINDX
LEFT JOIN dbo.GL00102 cat ON cat.ACCATNUM=a.ACCATNUM
CROSS JOIN periods p
LEFT JOIN summary s ON s.ACTINDX=a.ACTINDX AND s.YEAR1=p.YEAR1 AND s.PERIODID=p.PERIODID
LEFT JOIN opening o ON o.ACTINDX=a.ACTINDX AND o.YEAR1=p.YEAR1
LEFT JOIN detail d ON d.ACTINDX=a.ACTINDX AND d.YEAR1=p.YEAR1 AND d.PERIODID=p.PERIODID
WHERE p.YEAR1 BETWEEN ? AND ?
ORDER BY p.YEAR1,p.PERIODID,a.ACTINDX
"""


def _date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime): return value.date()
    if isinstance(value, dt.date): return value
    return dt.date.fromisoformat(str(value)[:10])


def _decimal(value: Any) -> D:
    try:
        return D(str(value or 0)).quantize(D("0.00001"))
    except decimal.InvalidOperation as exc:
        raise SourceValidationError("invalid numeric source value") from exc


def _money(value: Any) -> str:
    return format(_decimal(value), ".5f")


def monthly_recon_status(row: Mapping[str, Any]) -> dict[str, Any]:
    """Operational GP module closure, not an audit or evidence of who closed it."""
    flags = {f"PSERIES_{i}": row.get(f"PSERIES_{i}") for i in range(1, 7)}
    counts = [row.get(key) for key in (
        "origin_row_count", "origin_invalid_count", "origin_mismatch_count")]
    valid_counts = all(type(value) is int and value >= 0 for value in counts)
    total, invalid, mismatches = counts
    if not valid_counts or total == 0 or invalid:
        origin_status = "unknown"
    else:
        origin_status = "mixed" if mismatches else "consistent"
    if any(type(value) not in (int, bool) or value not in (0, 1) for value in flags.values()) or origin_status == "unknown":
        status = "unknown"
    elif origin_status == "mixed" or len(set(flags.values())) > 1:
        status = "mixed"
    else:
        status = "completed" if all(flags.values()) else "open"
    return {
        "status": status, "completed": status == "completed",
        "basis": "GP fiscal-period module closure",
        "module_flags": flags,
        "origin_check": {"status": origin_status, "row_count": total,
                         "invalid_count": invalid, "mismatch_count": mismatches},
        "interpretation": "Operational close indicator; not an audit or named-person attribution.",
    }


def choose_last_periods(
    calendar: Iterable[Mapping[str, Any]], as_of: dt.date, count: int | None = None,
    min_year: int = MIN_FISCAL_YEAR,
) -> list[dict[str, Any]]:
    """Include all configured fiscal months, regardless of date or closure.

    as_of is retained for caller compatibility, not period eligibility. Future
    months contain actual posted GP balances, never projections.
    """
    eligible = [
        dict(p) for p in calendar
        if int(p["period"]) > 0 and int(p["year"]) >= min_year
    ]
    eligible.sort(key=lambda p: (_date(p["start"]), int(p["year"]), int(p["period"])))
    if count is None:
        return eligible
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise ValueError("count must be a positive integer or None")
    return eligible[-count:]


def map_account_row(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        posting_type = int(row.get("posting_type"))
        typical_balance = int(row.get("typical_balance"))
    except (TypeError, ValueError) as exc:
        raise SourceValidationError("invalid posting_type or typical_balance enum") from exc
    if posting_type not in (0, 1):
        raise SourceValidationError(f"invalid posting_type enum: {posting_type}")
    if typical_balance not in (0, 1):
        raise SourceValidationError(f"invalid typical_balance enum: {typical_balance}")
    debit, credit = _decimal(row.get("period_debit")), _decimal(row.get("period_credit"))
    opening_debit, opening_credit = _decimal(row.get("opening_debit", 0)), _decimal(row.get("opening_credit", 0))
    ytd_debit, ytd_credit = _decimal(row.get("ytd_debit")), _decimal(row.get("ytd_credit"))
    sign = D("-1") if typical_balance == 1 else D("1")
    return {
        "account_index": int(row.get("account_index") or 0),
        "account_number": str(row.get("account_number") or "").strip(),
        "account_description": str(row.get("account_description") or "").strip(),
        "posting_type": posting_type,
        "typical_balance": typical_balance,
        "category_id": int(row.get("category") or 0),
        "category": str(row.get("category_description") or "").strip(),
        "period_debit": _money(debit), "period_credit": _money(credit),
        "period_balance": _money((debit-credit)*sign),
        "opening_debit": _money(opening_debit),
        "opening_credit": _money(opening_credit),
        "ytd_debit": _money(ytd_debit), "ytd_credit": _money(ytd_credit),
        "ending_balance": _money((ytd_debit-ytd_credit)*sign),
        "detail_period_debit": _money(row.get("detail_period_debit", debit)),
        "detail_period_credit": _money(row.get("detail_period_credit", credit)),
    }


def validate_controls(values: Mapping[str, Any]) -> dict[str, Any]:
    checks = {
        "summary_detail_debit": abs(_decimal(values["summary_period_debit"])-_decimal(values["detail_period_debit"])),
        "summary_detail_credit": abs(_decimal(values["summary_period_credit"])-_decimal(values["detail_period_credit"])),
        "debits_credits": abs(_decimal(values["summary_period_debit"])-_decimal(values["summary_period_credit"])),
        "balance_sheet_plus_pl_ytd": abs(_decimal(values["bs_raw_ytd"])+_decimal(values["pl_raw_ytd"])),
        "balance_sheet_equation": abs(_decimal(values["balance_sheet_equation"])),
    }
    counts = {key: int(values[key]) for key in ("missing_account_count", "missing_category_count")}
    if any(value > CONTROL_TOLERANCE for value in checks.values()) or any(counts.values()):
        raise SourceValidationError("company period controls failed: " + json.dumps({**{k: _money(v) for k,v in checks.items()}, **counts}, sort_keys=True))
    return {"passed": True, **{key: _money(value) for key,value in checks.items()}, **counts}


def canonical_json(value: Any) -> str:
    """Return the UTF-8 JSON text used by both extraction and SQL validation."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def period_key(period: Mapping[str, Any]) -> tuple[str, int, int]:
    return (str(period["company_code"]), int(period["fiscal_year"]), int(period["fiscal_period"]))


def source_hash(periods: Iterable[Mapping[str, Any]]) -> str:
    """Hash ordered lowercase payload SHA text, matching financial_validate_run."""
    ordered = sorted(periods, key=period_key)
    concatenated = "".join(str(period["payload_sha256"]) for period in ordered)
    return hashlib.sha256(concatenated.encode("utf-8")).hexdigest()


def _raw_balance(row: Mapping[str, Any], prefix: str = "period") -> D:
    return _decimal(row[f"{prefix}_debit"])-_decimal(row[f"{prefix}_credit"])


def _model_metadata() -> dict[str, str]:
    return {
        "mapping_version": MAPPING_VERSION,
        "mapping_sha256": canonical_hash(MAPPING_SPEC),
        "control_version": CONTROL_VERSION,
        "control_sha256": canonical_hash(CONTROL_SPEC),
    }


def _has_financial_value(row: Mapping[str, Any]) -> bool:
    fields = (
        "period_debit", "period_credit", "opening_debit", "opening_credit",
        "ytd_debit", "ytd_credit", "detail_period_debit", "detail_period_credit",
    )
    return any(_decimal(row.get(field, 0)) != ZERO for field in fields)


def _validate_accounts(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    indices: set[int] = set()
    financial: list[dict[str, Any]] = []
    for row in accounts:
        posting_type = row.get("posting_type")
        typical_balance = row.get("typical_balance")
        if posting_type not in (0, 1):
            raise SourceValidationError(f"invalid posting_type enum: {posting_type}")
        if typical_balance not in (0, 1):
            raise SourceValidationError(f"invalid typical_balance enum: {typical_balance}")
        account_index = int(row.get("account_index") or 0)
        if account_index in indices:
            raise SourceValidationError(f"duplicate account_index: {account_index}")
        indices.add(account_index)
        if not str(row.get("account_number") or "").strip():
            raise SourceValidationError(f"missing account_number for account_index {account_index}")
        if not str(row.get("category") or "").strip():
            raise SourceValidationError(
                f"missing category description for account {row.get('account_number', '')}"
            )
        category = int(row.get("category_id") or 0)
        nonzero = _has_financial_value(row)
        if category in NONFINANCIAL_CATEGORIES:
            if nonzero:
                raise SourceValidationError(
                    f"nonfinancial account has a nonzero balance: {row.get('account_number', '')}"
                )
            continue
        expected = BS_CATEGORIES if posting_type == 0 else IS_CATEGORIES
        opposite = IS_CATEGORIES if posting_type == 0 else BS_CATEGORIES
        if category in opposite:
            raise SourceValidationError(
                f"posting/category mismatch: account {row.get('account_number', '')} category {category}"
            )
        if category not in expected:
            if nonzero:
                raise SourceValidationError(
                    f"unmapped nonzero account: {row.get('account_number', '')} category {category}"
                )
            continue
        financial.append(row)
    return financial


def _income_section(row: Mapping[str, Any]) -> str:
    category = int(row["category_id"])
    description = str(row.get("account_description") or "").upper()
    if category == 32 and "DISCOUNTS GIVEN" in description:
        return "cost_of_revenue"
    if category in (31, 32):
        return "revenue"
    if category == 33:
        return "cost_of_revenue"
    if category == 37 and any(term in description for term in ("DRAW", "DISTRIBUTION")):
        return "equity_distribution"
    if category in (36, 37):
        return "employee_related_expenses"
    if category in (35, 40):
        return "other_operating_expenses"
    if category == 38:
        return "other_expense"
    if category == 39:
        if "INCOME TAX" in description:
            return "income_tax"
        if any(re.search(rf"\b{re.escape(term)}\b", description) for term in EMPLOYEE_TERMS):
            return "employee_related_expenses"
        return "other_operating_expenses"
    if category == 42:
        if "INCOME TAX" in description:
            return "income_tax"
        if any(re.search(rf"\b{re.escape(term)}\b", description) for term in EMPLOYEE_TERMS):
            return "employee_related_expenses"
        if any(term in description for term in ("LOSS/GAIN ON SALE", "GAIN ON SALE", "LOSS ON SALE")):
            return "other_expense"
        return "other_operating_expenses"
    if category == 43:
        return "other_income"
    raise SourceValidationError(
        f"unmapped income-statement category {category}: {description}"
    )


def _income_amount(row: Mapping[str, Any], prefix: str) -> D:
    amount = _raw_balance(row, prefix)
    return -amount if _income_section(row) in ("revenue", "other_income") else amount


def _balance_section(row: Mapping[str, Any]) -> str:
    category = int(row["category_id"])
    description = str(row.get("account_description") or "").upper()
    if category == 1:
        return "cash"
    if category in (3, 4, 5, 7):
        return "current_assets"
    if category == 9:
        return "property_plant_equipment"
    if category == 10:
        return "accumulated_depreciation"
    if category == 11:
        return "intangible_assets"
    if category == 12:
        return "other_assets"
    if category in (13, 16):
        return "current_liabilities"
    if category == 14:
        if "UNREALIZED GAIN" in description:
            return "equity"
        if "OFFICER" in description:
            return "current_liabilities"
        return "long_term_liabilities"
    if row.get("synthetic"):
        return str(row.get("balance_section") or "current_earnings")
    if category in (23, 25, 27, 30):
        return "equity"
    raise SourceValidationError(
        f"unmapped balance-sheet category {category}: {description}"
    )


def _balance_amount(row: Mapping[str, Any]) -> D:
    if row.get("synthetic"):
        return _decimal(row["ending_balance"])
    raw = _raw_balance(row, "ytd")
    section = _balance_section(row)
    asset_sections = {
        "cash", "current_assets", "property_plant_equipment",
        "accumulated_depreciation", "intangible_assets", "other_assets",
    }
    return raw if section in asset_sections else -raw


def _account_key(row: Mapping[str, Any], consolidate_smi: bool) -> str:
    number = str(row.get("account_number") or "")
    return number.split("-", 1)[0] if consolidate_smi else number


def _account_rows(
    current: list[dict[str, Any]], prior: list[dict[str, Any]], section: str,
    *, consolidate_smi: bool, balance_sheet: bool,
) -> list[dict[str, Any]]:
    classify = _balance_section if balance_sheet else _income_section
    current_groups: dict[str, list[dict[str, Any]]] = {}
    prior_groups: dict[str, list[dict[str, Any]]] = {}
    for row in current:
        if row["posting_type"] == (0 if balance_sheet else 1) and classify(row) == section:
            current_groups.setdefault(_account_key(row, consolidate_smi), []).append(row)
    for row in prior:
        if row["posting_type"] == (0 if balance_sheet else 1) and classify(row) == section:
            prior_groups.setdefault(_account_key(row, consolidate_smi), []).append(row)
    result = []
    for number in sorted(set(current_groups) | set(prior_groups)):
        current_detail, prior_detail = current_groups.get(number, []), prior_groups.get(number, [])
        representative = (current_detail or prior_detail)[0]
        if balance_sheet:
            values = {
                "current_balance": sum((_balance_amount(row) for row in current_detail), ZERO),
                "prior_balance": sum((_balance_amount(row) for row in prior_detail), ZERO),
            }
        else:
            values = {
                "current_period": sum((_income_amount(row, "period") for row in current_detail), ZERO),
                "current_ytd": sum((_income_amount(row, "ytd") for row in current_detail), ZERO),
                "prior_period": sum((_income_amount(row, "period") for row in prior_detail), ZERO),
                "prior_ytd": sum((_income_amount(row, "ytd") for row in prior_detail), ZERO),
            }
        if not any(value != ZERO for value in values.values()):
            continue
        label = str(representative.get("account_description") or number).strip()
        label = re.sub(r"\s*--+\s*$", "", label).strip()
        result.append({
            "id": f"{section}:account:{number}", "parent_id": section, "row_type": "account",
            "level": 2 if balance_sheet else 1,
            "label": label or number,
            "account_number": number, "values": {key: _money(value) for key, value in values.items()},
            "detail": current_detail if consolidate_smi else [],
        })
    return result


def _sum_values(rows: Iterable[Mapping[str, Any]], keys: Iterable[str]) -> dict[str, str]:
    rows = list(rows)
    return {key: _money(sum((_decimal(row["values"][key]) for row in rows), ZERO)) for key in keys}


def _presentation(
    company: CompanyConfig, current: list[dict[str, Any]], prior: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    consolidate = company.code == "SMI"
    income_keys = ("current_period", "current_ytd", "prior_period", "prior_ytd")
    income_sections = (
        ("revenue", "Revenue"), ("cost_of_revenue", "Cost of Revenue"),
        ("employee_related_expenses", "Employee-Related Expenses"),
        ("other_operating_expenses", "Other Operating Expenses"),
        ("other_income", "Other Income"), ("other_expense", "Other Expense"),
        ("income_tax", "Income Tax"),
    )
    section_rows: dict[str, dict[str, Any]] = {}
    children: dict[str, list[dict[str, Any]]] = {}
    for identifier, label in income_sections:
        children[identifier] = _account_rows(
            current, prior, identifier, consolidate_smi=consolidate, balance_sheet=False,
        )
        section_rows[identifier] = {
            "id": identifier, "parent_id": None, "row_type": "section", "level": 0,
            "label": label, "values": _sum_values(children[identifier], income_keys),
        }

    def income_total(identifier: str, label: str, values: Mapping[str, D]) -> dict[str, Any]:
        return {"id": identifier, "parent_id": None, "row_type": "subtotal", "level": 0,
                "label": label, "values": {key: _money(value) for key, value in values.items()}}

    value = lambda section, key: _decimal(section_rows[section]["values"][key])
    gross = income_total("gross_margin", "Gross Margin", {
        key: value("revenue", key) - value("cost_of_revenue", key) for key in income_keys
    })
    operating_expenses = income_total("total_operating_expenses", "Total Operating Expenses", {
        key: value("employee_related_expenses", key) + value("other_operating_expenses", key)
        for key in income_keys
    })
    operations = income_total("income_from_operations", "Income from Operations", {
        key: _decimal(gross["values"][key]) - _decimal(operating_expenses["values"][key])
        for key in income_keys
    })
    pretax = income_total("income_before_tax", "Income Before Taxes", {
        key: _decimal(operations["values"][key]) + value("other_income", key) - value("other_expense", key)
        for key in income_keys
    })
    net_income = income_total("net_income", "Net Income", {
        key: _decimal(pretax["values"][key]) - value("income_tax", key) for key in income_keys
    })
    income_rows = []
    for identifier in ("revenue", "cost_of_revenue"):
        income_rows.extend((section_rows[identifier], *children[identifier]))
    income_rows.append(gross)
    for identifier in ("employee_related_expenses", "other_operating_expenses"):
        if children[identifier]:
            income_rows.extend((section_rows[identifier], *children[identifier]))
    income_rows.extend((operating_expenses, operations))
    for identifier in ("other_income", "other_expense"):
        if children[identifier]:
            income_rows.extend((section_rows[identifier], *children[identifier]))
    income_rows.append(pretax)
    if children["income_tax"]:
        income_rows.extend((section_rows["income_tax"], *children["income_tax"]))
    income_rows.append(net_income)

    balance_keys = ("current_balance", "prior_balance")
    balance_sections = (
        ("cash", "Cash"), ("current_assets", "Current Assets"),
        ("property_plant_equipment", "Property, Plant & Equipment"),
        ("accumulated_depreciation", "Accumulated Depreciation"),
        ("intangible_assets", "Intangible Assets"), ("other_assets", "Other Assets"),
        ("current_liabilities", "Current Liabilities"),
        ("long_term_liabilities", "Long-Term Liabilities"), ("equity", "Equity"),
        ("equity_distributions", "Partner Distributions"),
        ("current_earnings", "Current Earnings"),
    )
    bs_sections: dict[str, dict[str, Any]] = {}
    bs_children: dict[str, list[dict[str, Any]]] = {}
    for identifier, label in balance_sections:
        bs_children[identifier] = _account_rows(
            current, prior, identifier, consolidate_smi=consolidate, balance_sheet=True,
        )
        bs_sections[identifier] = {
            "id": identifier, "parent_id": "assets" if identifier in {"cash", "current_assets", "property_plant_equipment", "accumulated_depreciation", "intangible_assets", "other_assets"} else ("liabilities" if "liabilities" in identifier else "equity_and_earnings"),
            "row_type": "section", "level": 1, "label": label,
            "values": _sum_values(bs_children[identifier], balance_keys),
        }
    asset_ids = ("cash", "current_assets", "property_plant_equipment", "accumulated_depreciation", "intangible_assets", "other_assets")
    liability_ids = ("current_liabilities", "long_term_liabilities")
    equity_ids = ("equity", "equity_distributions", "current_earnings")
    def bs_total(identifier: str, label: str, identifiers: Iterable[str]) -> dict[str, Any]:
        identifiers = tuple(identifiers)
        return {"id": identifier, "parent_id": None, "row_type": "subtotal", "level": 0,
                "label": label, "values": {key: _money(sum((_decimal(bs_sections[item]["values"][key]) for item in identifiers), ZERO)) for key in balance_keys}}
    total_assets = bs_total("total_assets", "Total Assets", asset_ids)
    total_liabilities = bs_total("total_liabilities", "Total Liabilities", liability_ids)
    total_equity = bs_total("total_equity", "Total Equity", equity_ids)
    total_le = {"id": "total_liabilities_and_equity", "parent_id": None, "row_type": "subtotal", "level": 0,
                "label": "Total Liabilities & Equity", "values": {
                    key: _money(_decimal(total_liabilities["values"][key]) + _decimal(total_equity["values"][key]))
                    for key in balance_keys}}
    balance_rows = [{"id": "assets", "parent_id": None, "row_type": "heading", "level": 0, "label": "Assets", "values": total_assets["values"]}]
    for identifier in asset_ids:
        if bs_children[identifier]:
            balance_rows.extend((bs_sections[identifier], *bs_children[identifier]))
    balance_rows.extend((total_assets, {"id": "liabilities", "parent_id": None, "row_type": "heading", "level": 0, "label": "Liabilities", "values": total_liabilities["values"]}))
    for identifier in liability_ids:
        if bs_children[identifier]:
            balance_rows.extend((bs_sections[identifier], *bs_children[identifier]))
    balance_rows.extend((total_liabilities, {"id": "equity_and_earnings", "parent_id": None, "row_type": "heading", "level": 0, "label": "Equity", "values": total_equity["values"]}))
    for identifier in equity_ids:
        if bs_children[identifier]:
            balance_rows.extend((bs_sections[identifier], *bs_children[identifier]))
    balance_rows.extend((total_equity, total_le))
    return {"income_statement": income_rows, "balance_sheet": balance_rows}


def _mark_missing_comparison(presentation: dict[str, list[dict[str, Any]]]) -> None:
    """Represent an unavailable prior year as null, never as a fabricated zero."""
    for row in presentation["income_statement"]:
        row["values"]["prior_period"] = None
        row["values"]["prior_ytd"] = None
    for row in presentation["balance_sheet"]:
        row["values"]["prior_balance"] = None


def _statement_metrics(accounts: list[dict[str, Any]]) -> dict[str, D]:
    is_rows = [row for row in accounts if row["posting_type"] == 1]
    bs_rows = [row for row in accounts if row["posting_type"] == 0]
    revenue = sum((_income_amount(row, "period") for row in is_rows if _income_section(row) == "revenue"), ZERO)
    cogs = sum((_income_amount(row, "period") for row in is_rows if _income_section(row) == "cost_of_revenue"), ZERO)
    expenses = sum((_income_amount(row, "period") for row in is_rows if _income_section(row) in ("employee_related_expenses", "other_operating_expenses")), ZERO)
    operating_is_rows = [row for row in is_rows if _income_section(row) != "equity_distribution"]
    distribution_rows = [row for row in is_rows if _income_section(row) == "equity_distribution"]
    net_income = -sum((_raw_balance(row) for row in operating_is_rows), ZERO)
    current_earnings = -sum((_raw_balance(row, "ytd") for row in operating_is_rows), ZERO)
    equity_distributions = -sum((_raw_balance(row, "ytd") for row in distribution_rows), ZERO)
    assets = sum((_balance_amount(row) for row in bs_rows if _balance_section(row) in {
        "cash", "current_assets", "property_plant_equipment",
        "accumulated_depreciation", "intangible_assets", "other_assets",
    }), ZERO)
    liabilities = sum((_balance_amount(row) for row in bs_rows if _balance_section(row) in {
        "current_liabilities", "long_term_liabilities",
    }), ZERO)
    equity = sum((_balance_amount(row) for row in bs_rows if _balance_section(row) == "equity"), ZERO) + current_earnings + equity_distributions
    return {"revenue": revenue, "cost_of_goods_sold": cogs, "gross_profit": revenue-cogs,
            "operating_expenses": expenses, "net_income": net_income, "assets": assets,
            "liabilities": liabilities, "equity": equity, "current_earnings": current_earnings}


def _with_current_earnings(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    metrics = _statement_metrics(accounts)
    distribution_rows = [
        row for row in accounts
        if row["posting_type"] == 1 and _income_section(row) == "equity_distribution"
    ]
    equity_distributions_period = -sum((_raw_balance(row) for row in distribution_rows), ZERO)
    equity_distributions_ytd = -sum((_raw_balance(row, "ytd") for row in distribution_rows), ZERO)
    result = [*accounts, {
        "account_index": 0, "account_number": "", "account_description": "Current Earnings",
        "posting_type": 0, "typical_balance": 1, "category_id": 30, "category": "Current Earnings",
        "period_debit": "0.00000", "period_credit": "0.00000",
        "period_balance": _money(metrics["net_income"]), "opening_debit": "0.00000",
        "opening_credit": "0.00000", "ytd_debit": "0.00000", "ytd_credit": "0.00000",
        "ending_balance": _money(metrics["current_earnings"]), "detail_period_debit": "0.00000",
        "detail_period_credit": "0.00000", "synthetic": True, "balance_section": "current_earnings",
    }]
    if equity_distributions_ytd != ZERO:
        result.append({
            "account_index": -1, "account_number": "", "account_description": "Partner Distributions",
            "posting_type": 0, "typical_balance": 1, "category_id": 30, "category": "Partner Distributions",
            "period_debit": "0.00000", "period_credit": "0.00000",
            "period_balance": _money(equity_distributions_period), "opening_debit": "0.00000",
            "opening_credit": "0.00000", "ytd_debit": "0.00000", "ytd_credit": "0.00000",
            "ending_balance": _money(equity_distributions_ytd), "detail_period_debit": "0.00000",
            "detail_period_credit": "0.00000", "synthetic": True, "balance_section": "equity_distributions",
        })
    return result


def validate_statement_bridge(statement: Mapping[str, Any]) -> dict[str, Any]:
    try:
        income = {row["id"]: row for row in statement["presentation"]["income_statement"]}
        balance = {row["id"]: row for row in statement["presentation"]["balance_sheet"]}
        kpis = statement["kpis"]
        bridges = {
            "revenue": _decimal(income["revenue"]["values"]["current_period"]) - _decimal(kpis["revenue"]),
            "cost_of_goods_sold": _decimal(income["cost_of_revenue"]["values"]["current_period"]) - _decimal(kpis["cost_of_goods_sold"]),
            "gross_profit": _decimal(income["gross_margin"]["values"]["current_period"]) - _decimal(kpis["gross_profit"]),
            "operating_expenses": _decimal(income["total_operating_expenses"]["values"]["current_period"]) - _decimal(kpis["operating_expenses"]),
            "net_income": _decimal(income["net_income"]["values"]["current_period"]) - _decimal(kpis["net_income"]),
            "assets": _decimal(balance["total_assets"]["values"]["current_balance"]) - _decimal(kpis["assets"]),
            "liabilities": _decimal(balance["total_liabilities"]["values"]["current_balance"]) - _decimal(kpis["liabilities"]),
            "equity": _decimal(balance["total_equity"]["values"]["current_balance"]) - _decimal(kpis["equity"]),
        }
    except (KeyError, TypeError) as exc:
        raise SourceValidationError("KPI-to-statement bridge is incomplete") from exc
    if any(abs(value) > CONTROL_TOLERANCE for value in bridges.values()):
        raise SourceValidationError("KPI-to-statement bridge failed: " + json.dumps(
            {key: _money(value) for key, value in bridges.items()}, sort_keys=True,
        ))
    return {"passed": True, **{key: _money(value) for key, value in bridges.items()}}


def _period_payload(
    company: CompanyConfig, period: Mapping[str, Any], accounts: list[dict[str, Any]],
    trend: list[dict[str, Any]], prior_accounts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    is_rows = [row for row in accounts if row["posting_type"] == 1]
    bs_rows = _with_current_earnings([row for row in accounts if row["posting_type"] == 0] + is_rows)
    # _with_current_earnings returns all source rows; retain only balance-sheet rows plus the synthetic row.
    bs_rows = [row for row in bs_rows if row["posting_type"] == 0]
    metrics = _statement_metrics(accounts)
    prior_accounts = prior_accounts or []
    prior_bs = _with_current_earnings(prior_accounts)
    prior_bs = [row for row in prior_bs if row["posting_type"] == 0]
    prior_synthetic = [row for row in prior_bs if row.get("synthetic")]
    presentation = _presentation(company, [*is_rows, *bs_rows], [*prior_accounts, *prior_synthetic])
    if not prior_accounts:
        _mark_missing_comparison(presentation)
    statement = {
        "income_statement": is_rows,
        "balance_sheet": bs_rows,
        "kpis": {key: _money(metrics[key]) for key in (
            "revenue", "cost_of_goods_sold", "gross_profit", "operating_expenses",
            "net_income", "assets", "liabilities", "equity")},
        "trend": trend,
        "presentation": presentation,
        "model_metadata": _model_metadata(),
        "comparison": {
            "fiscal_year": int(period["year"])-1,
            "fiscal_period": int(period["period"]),
            "available": bool(prior_accounts),
        },
    }
    statement["bridge_controls"] = validate_statement_bridge(statement)
    payload = {
        "company_code": company.code, "company_name": company.name,
        "fiscal_year": int(period["year"]), "fiscal_period": int(period["period"]),
        "period_start": _date(period["start"]).isoformat(), "period_end": _date(period["end"]).isoformat(),
        "statement": statement,
        "monthly_recon": monthly_recon_status(period.get("close_evidence", {})),
    }
    canonical = canonical_json(payload)
    return {
        "company_code": payload["company_code"], "company_name": payload["company_name"],
        "fiscal_year": payload["fiscal_year"], "fiscal_period": payload["fiscal_period"],
        "period_start": payload["period_start"], "period_end": payload["period_end"],
        "payload": payload, "payload_canonical": canonical,
        "payload_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def build_company_payloads(
    company: CompanyConfig, periods: list[Mapping[str, Any]],
    rows_by_period: Mapping[tuple[int, int], list[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    requested_keys = [(int(period["year"]), int(period["period"])) for period in periods]
    if len(requested_keys) != len(set(requested_keys)):
        raise SourceValidationError(f"{company.code}: duplicate requested period")
    supplied_keys = set(rows_by_period)
    if supplied_keys != set(requested_keys):
        missing = sorted(set(requested_keys)-supplied_keys)
        unexpected = sorted(supplied_keys-set(requested_keys))
        raise SourceValidationError(
            f"{company.code}: incomplete requested period coverage; missing={missing}, unexpected={unexpected}"
        )
    mapped: dict[tuple[int, int], list[dict[str, Any]]] = {}
    trend: list[dict[str, Any]] = []
    for period in periods:
        key = (int(period["year"]), int(period["period"]))
        source_rows = rows_by_period[key]
        if not source_rows:
            raise SourceValidationError(f"{company.code} {key}: incomplete requested period coverage (no accounts)")
        accounts = [row if "category_id" in row else map_account_row(row) for row in source_rows]
        accounts = _validate_accounts(accounts)
        if not accounts:
            raise SourceValidationError(f"{company.code} {key}: no mapped financial accounts")
        controls = {
            "summary_period_debit": sum((_decimal(a["period_debit"]) for a in accounts), ZERO),
            "summary_period_credit": sum((_decimal(a["period_credit"]) for a in accounts), ZERO),
            "detail_period_debit": sum((_decimal(a["detail_period_debit"]) for a in accounts), ZERO),
            "detail_period_credit": sum((_decimal(a["detail_period_credit"]) for a in accounts), ZERO),
            "bs_raw_ytd": sum((_raw_balance(a, "ytd") for a in accounts if a["posting_type"] == 0), ZERO),
            "pl_raw_ytd": sum((_raw_balance(a, "ytd") for a in accounts if a["posting_type"] == 1), ZERO),
            "missing_account_count": 0,
            "missing_category_count": 0,
        }
        metrics = _statement_metrics(accounts)
        controls["balance_sheet_equation"] = metrics["assets"]-metrics["liabilities"]-metrics["equity"]
        control_result = validate_controls(controls)
        mapped[key] = accounts
        trend.append({
            "fiscal_year": key[0], "fiscal_period": key[1],
            **{name: _money(metrics[name]) for name in (
                "revenue", "cost_of_goods_sold", "gross_profit", "operating_expenses", "net_income")},
            "controls": control_result,
        })
    return [
        _period_payload(
            company, period, mapped[(int(period["year"]), int(period["period"]))], trend[:index+1],
            mapped.get((int(period["year"])-1, int(period["period"])), []),
        )
        for index, period in enumerate(periods)
    ]


def _fetch(cursor: Any, sql: str, *params: Any) -> list[dict[str,Any]]:
    rows=cursor.execute(sql,*params).fetchall()
    names=[column[0] for column in cursor.description]
    return [dict(zip(names,row)) for row in rows]


def verify_read_only(cursor: Any) -> None:
    database_row = cursor.execute(
        "SELECT IS_SRVROLEMEMBER('sysadmin'), "
        "HAS_PERMS_BY_NAME(DB_NAME(),'DATABASE','CREATE TABLE')"
    ).fetchone()
    if tuple(int(value or 0) for value in database_row) != (0, 0):
        raise SourceValidationError("SQL login is outside reviewed read-only contract")

    table_rows = cursor.execute("""
SELECT source_table,
       HAS_PERMS_BY_NAME(source_table,'OBJECT','SELECT') can_select,
       HAS_PERMS_BY_NAME(source_table,'OBJECT','INSERT') can_insert,
       HAS_PERMS_BY_NAME(source_table,'OBJECT','UPDATE') can_update,
       HAS_PERMS_BY_NAME(source_table,'OBJECT','DELETE') can_delete
FROM (VALUES
  ('dbo.SY40100'), ('dbo.SY40101'), ('dbo.GL10110'), ('dbo.GL10111'),
  ('dbo.GL20000'), ('dbo.GL30000'), ('dbo.GL00100'), ('dbo.GL00105'),
  ('dbo.GL00102')
) AS required(source_table)
""").fetchall()
    if len(table_rows) != 9 or any(
        tuple(int(value or 0) for value in row[1:]) != (1, 0, 0, 0)
        for row in table_rows
    ):
        raise SourceValidationError(
            "SQL login lacks SELECT-only access on every required GP table"
        )


def extract_company(connection: Any, company: CompanyConfig, as_of: dt.date, period_count: int | None = None) -> list[dict[str,Any]]:
    cursor=connection.cursor(); verify_read_only(cursor)
    calendar_rows=_fetch(cursor,CALENDAR_SQL)
    calendar=[{"year":r["fiscal_year"],"period":r["fiscal_period"],"start":r["period_start"],"end":r["period_end"],"close_evidence":r} for r in calendar_rows]
    periods=choose_last_periods(calendar,as_of,period_count)
    if not periods: raise SourceValidationError("no eligible fiscal periods")
    raw=_fetch(cursor,ACCOUNT_SQL,min(p["year"] for p in periods),max(p["year"] for p in periods))
    selected={(int(p["year"]),int(p["period"])) for p in periods}; grouped={key:[] for key in selected}
    for row in raw:
        key=(int(row["fiscal_year"]),int(row["fiscal_period"]))
        if key in grouped: grouped[key].append(row)
    return build_company_payloads(company,periods,grouped)


def extract(companies: Iterable[CompanyConfig], as_of: dt.date) -> dict[str,Any]:
    company_list=list(companies); period_payloads=[]; failures={}
    for company in company_list:
        try:
            connection=connect_company(company)
            try: period_payloads.extend(extract_company(connection,company,as_of))
            finally: connection.close()
        except Exception as exc:
            failures[company.code]=f"{type(exc).__name__}: {exc}"
    if failures: raise SourceValidationError("one or more companies failed closed: "+json.dumps(failures,sort_keys=True))
    manifest = [
        {"company_code": code, "fiscal_year": year, "fiscal_period": period}
        for code, year, period in sorted(period_key(item) for item in period_payloads)
    ]
    run={"as_of":as_of.isoformat(),"company_count":len(company_list),"period_count":len(period_payloads),"manifest":manifest,**_model_metadata()}
    run["source_sha256"]=source_hash(period_payloads)
    return {"run":run,"periods":period_payloads}


def write_atomic(document: Mapping[str,Any], output: Path) -> None:
    output.parent.mkdir(parents=True,exist_ok=True); temporary=output.with_suffix(output.suffix+".tmp")
    temporary.write_text(json.dumps(document,indent=2,ensure_ascii=False),encoding="utf-8")
    os.chmod(temporary,0o600); temporary.replace(output)


def main(argv: list[str]|None=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--company",action="append",choices=[c.code for c in ACTIVE_COMPANIES]); parser.add_argument("--as-of",type=dt.date.fromisoformat,default=dt.date.today()); parser.add_argument("--output",type=Path,default=Path("data/financial-statements.json")); args=parser.parse_args(argv)
    selected=[c for c in ACTIVE_COMPANIES if not args.company or c.code in args.company]
    document=extract(selected,args.as_of); write_atomic(document,args.output)
    print(json.dumps({"output":str(args.output.resolve()),**document["run"]},indent=2)); return 0


if __name__ == "__main__": raise SystemExit(main())
