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
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

D = decimal.Decimal
ZERO = D("0.00000")
CONTROL_TOLERANCE = D("0.01")
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
    "SPFAR": "Structural Properties, LLP (Fargo)", "TPC": "The Perry Center",
    "TROP": "Tropic Paws LLC", "UNIFO": "Uniform Unit — Dickinson",
    "UUFGO": "Uniform Unit — Fargo", "VGA": "Vintage Garden LLC",
    "VPENG": "Ope It's Cold, LLC dba Vampire Penguin", "SMI": "SMI",
}
ACTIVE_COMPANIES = tuple(
    CompanyConfig(code, name, SMI_SERVER if code == "SMI" else MAIN_SERVER, code)
    for code, name in _COMPANY_NAMES.items()
)


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
SELECT CAST(p.YEAR1 AS int) fiscal_year, CAST(p.PERIODID AS int) fiscal_period,
       CAST(p.PERIODDT AS date) period_start, CAST(p.PERDENDT AS date) period_end
FROM dbo.SY40100 p
JOIN dbo.SY40101 y ON y.YEAR1=p.YEAR1
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


def choose_last_periods(calendar: Iterable[Mapping[str, Any]], as_of: dt.date, count: int = 24) -> list[dict[str, Any]]:
    eligible = [dict(p) for p in calendar if int(p["period"]) > 0 and _date(p["end"]) <= as_of]
    eligible.sort(key=lambda p: (_date(p["start"]), int(p["year"]), int(p["period"])))
    return eligible[-count:]


def map_account_row(row: Mapping[str, Any]) -> dict[str, Any]:
    posting_type = int(row.get("posting_type") or 0)
    typical_balance = int(row.get("typical_balance") or 0)
    debit, credit = _decimal(row.get("period_debit")), _decimal(row.get("period_credit"))
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
        "opening_debit": _money(row.get("opening_debit", 0)),
        "opening_credit": _money(row.get("opening_credit", 0)),
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


def _statement_metrics(accounts: list[dict[str, Any]]) -> dict[str, D]:
    is_rows = [row for row in accounts if row["posting_type"] == 1]
    bs_rows = [row for row in accounts if row["posting_type"] == 0]
    revenue = -sum((_raw_balance(row) for row in is_rows if row["category_id"] in (31, 32)), ZERO)
    cogs = sum((_raw_balance(row) for row in is_rows if row["category_id"] == 33), ZERO)
    expenses = sum((_raw_balance(row) for row in is_rows if 34 <= row["category_id"] <= 47), ZERO)
    net_income = -sum((_raw_balance(row) for row in is_rows), ZERO)
    current_earnings = -sum((_raw_balance(row, "ytd") for row in is_rows), ZERO)
    assets = sum((_raw_balance(row, "ytd") for row in bs_rows if 1 <= row["category_id"] <= 10), ZERO)
    liabilities = -sum((_raw_balance(row, "ytd") for row in bs_rows if 11 <= row["category_id"] <= 19), ZERO)
    equity = -sum((_raw_balance(row, "ytd") for row in bs_rows if 20 <= row["category_id"] <= 30), ZERO) + current_earnings
    return {"revenue": revenue, "cost_of_goods_sold": cogs, "gross_profit": revenue-cogs,
            "operating_expenses": expenses, "net_income": net_income, "assets": assets,
            "liabilities": liabilities, "equity": equity, "current_earnings": current_earnings}


def _period_payload(company: CompanyConfig, period: Mapping[str, Any], accounts: list[dict[str, Any]], trend: list[dict[str, Any]]) -> dict[str, Any]:
    is_rows = [row for row in accounts if row["posting_type"] == 1]
    bs_rows = [row for row in accounts if row["posting_type"] == 0]
    metrics = _statement_metrics(accounts)
    current_earnings = metrics["current_earnings"]
    bs_rows = [*bs_rows, {
        "account_index": 0, "account_number": "", "account_description": "Current Earnings",
        "posting_type": 0, "typical_balance": 1, "category_id": 30, "category": "Current Earnings",
        "period_debit": "0.00000", "period_credit": "0.00000",
        "period_balance": _money(metrics["net_income"]), "opening_debit": "0.00000",
        "opening_credit": "0.00000", "ytd_debit": "0.00000", "ytd_credit": "0.00000",
        "ending_balance": _money(current_earnings), "detail_period_debit": "0.00000",
        "detail_period_credit": "0.00000", "synthetic": True,
    }]
    statement = {
        "income_statement": is_rows,
        "balance_sheet": bs_rows,
        "kpis": {key: _money(metrics[key]) for key in (
            "revenue", "cost_of_goods_sold", "gross_profit", "operating_expenses",
            "net_income", "assets", "liabilities", "equity")},
        "trend": trend,
    }
    payload = {
        "company_code": company.code, "company_name": company.name,
        "fiscal_year": int(period["year"]), "fiscal_period": int(period["period"]),
        "period_start": _date(period["start"]).isoformat(), "period_end": _date(period["end"]).isoformat(),
        "statement": statement,
    }
    canonical = canonical_json(payload)
    return {
        "company_code": payload["company_code"], "company_name": payload["company_name"],
        "fiscal_year": payload["fiscal_year"], "fiscal_period": payload["fiscal_period"],
        "period_start": payload["period_start"], "period_end": payload["period_end"],
        "payload": payload, "payload_canonical": canonical,
        "payload_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def build_company_payloads(company: CompanyConfig, periods: list[Mapping[str, Any]], rows_by_period: Mapping[tuple[int,int], list[Mapping[str,Any]]]) -> list[dict[str, Any]]:
    mapped: dict[tuple[int,int], list[dict[str,Any]]] = {}
    trend: list[dict[str,Any]] = []
    for period in periods:
        key=(int(period["year"]),int(period["period"]))
        accounts=[row if "category_id" in row else map_account_row(row) for row in rows_by_period.get(key, [])]
        if not accounts: raise SourceValidationError("company period has no accounts")
        controls = {
            "summary_period_debit": sum((_decimal(a["period_debit"]) for a in accounts),ZERO),
            "summary_period_credit": sum((_decimal(a["period_credit"]) for a in accounts),ZERO),
            "detail_period_debit": sum((_decimal(a["detail_period_debit"]) for a in accounts),ZERO),
            "detail_period_credit": sum((_decimal(a["detail_period_credit"]) for a in accounts),ZERO),
            "bs_raw_ytd": sum((_decimal(a["ytd_debit"])-_decimal(a["ytd_credit"]) for a in accounts if a["posting_type"]==0),ZERO),
            "pl_raw_ytd": sum((_decimal(a["ytd_debit"])-_decimal(a["ytd_credit"]) for a in accounts if a["posting_type"]==1),ZERO),
            "missing_account_count": sum(not a["account_number"] for a in accounts),
            "missing_category_count": sum(not a["category_id"] or not a["category"] for a in accounts),
        }
        metrics = _statement_metrics(accounts)
        controls["balance_sheet_equation"] = metrics["assets"]-metrics["liabilities"]-metrics["equity"]
        control_result=validate_controls(controls)
        mapped[key]=accounts
        trend.append({"fiscal_year":key[0],"fiscal_period":key[1], **{
            name: _money(metrics[name]) for name in (
                "revenue", "cost_of_goods_sold", "gross_profit", "operating_expenses", "net_income")}})
        trend[-1]["controls"] = control_result
    return [_period_payload(company,p,mapped[(int(p["year"]),int(p["period"]))],trend[:i+1]) for i,p in enumerate(periods)]


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


def extract_company(connection: Any, company: CompanyConfig, as_of: dt.date, period_count: int=24) -> list[dict[str,Any]]:
    cursor=connection.cursor(); verify_read_only(cursor)
    calendar_rows=_fetch(cursor,CALENDAR_SQL)
    calendar=[{"year":r["fiscal_year"],"period":r["fiscal_period"],"start":r["period_start"],"end":r["period_end"]} for r in calendar_rows]
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
    run={"as_of":as_of.isoformat(),"company_count":len(company_list),"period_count":len(period_payloads),"manifest":manifest}
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
