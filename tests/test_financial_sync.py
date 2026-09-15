import datetime as dt
import base64
import decimal
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.financial_sync import (
    ACCOUNT_SQL,
    ACTIVE_COMPANIES,
    CONTROL_TOLERANCE,
    CompanyConfig,
    SourceValidationError,
    build_company_payloads,
    canonical_hash,
    canonical_json,
    source_hash,
    choose_last_periods,
    map_account_row,
    validate_controls,
)
from scripts.publish_financials import CredentialError, load_credentials, publish, validate_document

D = decimal.Decimal


class CalendarTests(unittest.TestCase):
    def test_last_24_periods_include_only_completed_periods(self):
        calendar = [
            {"year": year, "period": month, "start": dt.date(year, month, 1),
             "end": (dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1))}
            for year in (2024, 2025, 2026) for month in range(1, 13)
        ]
        selected = choose_last_periods(calendar, dt.date(2026, 9, 14), 24)
        self.assertEqual(24, len(selected))
        self.assertEqual((2024, 9), (selected[0]["year"], selected[0]["period"]))
        self.assertEqual((2026, 8), (selected[-1]["year"], selected[-1]["period"]))

    def test_future_postings_are_excluded_until_period_end(self):
        calendar = [
            {"year": 2026, "period": 8, "start": dt.date(2026, 8, 1), "end": dt.date(2026, 8, 31)},
            {"year": 2026, "period": 9, "start": dt.date(2026, 9, 1), "end": dt.date(2026, 9, 30)},
        ]
        selected = choose_last_periods(calendar, dt.date(2026, 9, 14), 24)
        self.assertEqual([8], [period["period"] for period in selected])

    def test_period_zero_is_never_selected(self):
        calendar = [
            {"year": 2026, "period": 0, "start": dt.date(2025, 12, 31), "end": dt.date(2025, 12, 31)},
            {"year": 2026, "period": 1, "start": dt.date(2026, 1, 1), "end": dt.date(2026, 1, 31)},
        ]
        self.assertEqual([], choose_last_periods(calendar, dt.date(2026, 1, 10), 24))


class MappingAndPayloadTests(unittest.TestCase):
    def account(self, number="1000-00", category=1, posting_type=0, period_debit="10", period_credit="0",
                ytd_debit="10", ytd_credit="0", typical_balance=0, **extra):
        row = {
            "account_index": 1, "account_number": number, "account_description": "Cash",
            "posting_type": posting_type, "category": category, "category_description": "Cash",
            "typical_balance": typical_balance,
            "period_debit": D(period_debit), "period_credit": D(period_credit),
            "ytd_debit": D(ytd_debit), "ytd_credit": D(ytd_credit),
        }
        row.update(extra)
        return map_account_row(row)

    def test_mapping_uses_pstngtyp_and_preserves_formatted_number_and_category(self):
        account = self.account(number="0010-200-30", posting_type=1, category=7, typical_balance=1)
        self.assertEqual("0010-200-30", account["account_number"])
        self.assertEqual(1, account["posting_type"])
        self.assertEqual(7, account["category_id"])
        self.assertEqual("-10.00000", account["period_balance"])

    def test_mapping_uses_tpclblnc_not_statement_type_for_normal_display(self):
        debit_normal_expense = self.account(category=34, posting_type=1, typical_balance=0)
        credit_normal_revenue = self.account(category=31, posting_type=1, typical_balance=1)
        self.assertEqual("10.00000", debit_normal_expense["period_balance"])
        self.assertEqual("-10.00000", credit_normal_revenue["period_balance"])
        self.assertEqual(0, debit_normal_expense["typical_balance"])

    def test_account_sql_includes_period_zero_opening_and_tpclblnc(self):
        compact = " ".join(ACCOUNT_SQL.upper().split())
        self.assertIn("TPCLBLNC", compact)
        self.assertIn("PERIODID=0", compact)
        self.assertIn("OPENING_DEBIT", compact)

    def test_payload_contains_is_bs_kpis_trend_and_bs_ending_balance(self):
        periods = [
            {"year": 2026, "period": 1, "start": dt.date(2026, 1, 1), "end": dt.date(2026, 1, 31)},
            {"year": 2026, "period": 2, "start": dt.date(2026, 2, 1), "end": dt.date(2026, 2, 28)},
        ]
        rows = {
            (2026, 1): [self.account(), self.account(number="4000", category=31, posting_type=1, period_debit="0", period_credit="10", ytd_debit="0", ytd_credit="10")],
            (2026, 2): [self.account(period_debit="5", ytd_debit="15"), self.account(number="4000", category=31, posting_type=1, period_debit="0", period_credit="5", ytd_debit="0", ytd_credit="15")],
        }
        payloads = build_company_payloads(CompanyConfig("SFAB", "Structural Fab LLC", "server", "SFAB"), periods, rows)
        second = payloads[1]["payload"]
        self.assertEqual({"income_statement", "balance_sheet", "kpis", "trend"}, set(second["statement"]))
        self.assertEqual("15.00000", second["statement"]["balance_sheet"][0]["ending_balance"])
        self.assertEqual(2, len(second["statement"]["trend"]))
        self.assertEqual("5.00000", second["statement"]["kpis"]["revenue"])
        self.assertEqual(canonical_json(second), payloads[1]["payload_canonical"])
        self.assertEqual(canonical_hash(second), payloads[1]["payload_sha256"])

    def test_gp_categories_kpis_current_earnings_and_balance_sheet_reconcile(self):
        period = {"year": 2026, "period": 1, "start": dt.date(2026, 1, 1), "end": dt.date(2026, 1, 31)}
        rows = {(2026, 1): [
            self.account(number="1000", category=1, ytd_debit="115", period_debit="115"),
            self.account(number="2000", category=11, typical_balance=1, ytd_debit="0", ytd_credit="50", period_debit="0", period_credit="50"),
            self.account(number="3000", category=20, typical_balance=1, ytd_debit="0", ytd_credit="20", period_debit="0", period_credit="20"),
            self.account(number="4000", category=31, posting_type=1, typical_balance=1, ytd_debit="0", ytd_credit="100", period_debit="0", period_credit="100"),
            self.account(number="4010", category=32, posting_type=1, typical_balance=0, ytd_debit="10", ytd_credit="0", period_debit="10", period_credit="0"),
            self.account(number="5000", category=33, posting_type=1, typical_balance=0, ytd_debit="40", ytd_credit="0", period_debit="40", period_credit="0"),
            self.account(number="6000", category=34, posting_type=1, typical_balance=0, ytd_debit="5", ytd_credit="0", period_debit="5", period_credit="0"),
        ]}
        payload = build_company_payloads(CompanyConfig("SFAB", "Structural Fab LLC", "server", "SFAB"), [period], rows)[0]["payload"]
        statement = payload["statement"]
        self.assertEqual({
            "revenue": "90.00000", "cost_of_goods_sold": "40.00000",
            "gross_profit": "50.00000", "operating_expenses": "5.00000",
            "net_income": "45.00000", "assets": "115.00000",
            "liabilities": "50.00000", "equity": "65.00000",
        }, statement["kpis"])
        earnings = [row for row in statement["balance_sheet"] if row.get("synthetic")]
        self.assertEqual(1, len(earnings))
        self.assertEqual("Current Earnings", earnings[0]["account_description"])
        self.assertEqual("45.00000", earnings[0]["ending_balance"])
        self.assertLessEqual(abs(D(statement["kpis"]["assets"]) - D(statement["kpis"]["liabilities"]) - D(statement["kpis"]["equity"])), CONTROL_TOLERANCE)
        self.assertEqual("90.00000", statement["trend"][0]["revenue"])
        self.assertEqual("40.00000", statement["trend"][0]["cost_of_goods_sold"])

    def test_controls_fail_closed_on_any_required_invariant(self):
        valid = {
            "summary_period_debit": D("10"), "summary_period_credit": D("10"),
            "detail_period_debit": D("10"), "detail_period_credit": D("10"),
            "bs_raw_ytd": D("10"), "pl_raw_ytd": D("-10"),
            "balance_sheet_equation": D("0"),
            "missing_account_count": 0, "missing_category_count": 0,
        }
        self.assertTrue(validate_controls(valid)["passed"])
        for field in valid:
            broken = dict(valid)
            broken[field] = 1 if field.startswith("missing") else D("11")
            with self.subTest(field=field):
                with self.assertRaises(SourceValidationError):
                    validate_controls(broken)

    def test_company_allowlist_is_exact_and_excludes_sol1(self):
        expected = "BBMTS DMERC FFCAP FTSOL LPPAR MILT OLH OPAT OZAK1 OZDEV RDSC REB RRCOF RSS SEAMX SFAB SOL SPFAR TPC TROP UNIFO UUFGO VGA VPENG SMI".split()
        self.assertEqual(expected, [company.code for company in ACTIVE_COMPANIES])
        self.assertNotIn("SOL1", [company.code for company in ACTIVE_COMPANIES])
        self.assertEqual("360 Sales LLC", next(c.name for c in ACTIVE_COMPANIES if c.code == "REB"))
        self.assertEqual("Sea Max LLC", next(c.name for c in ACTIVE_COMPANIES if c.code == "SEAMX"))

    def test_hash_is_order_independent_for_json_objects(self):
        self.assertEqual(canonical_hash({"b": 2, "a": 1}), canonical_hash({"a": 1, "b": 2}))

    def test_period_envelope_and_source_hash_use_ordered_payload_hash_concatenation(self):
        payloads = [
            {"company_code": "B", "fiscal_year": 2026, "fiscal_period": 1, "statement": {}},
            {"company_code": "A", "fiscal_year": 2026, "fiscal_period": 2, "statement": {}},
        ]
        envelopes = [{
            "company_code": payload["company_code"],
            "company_name": payload["company_code"],
            "fiscal_year": payload["fiscal_year"],
            "fiscal_period": payload["fiscal_period"],
            "period_start": "2026-01-01",
            "period_end": "2026-01-31",
            "payload": payload,
            "payload_canonical": canonical_json(payload),
            "payload_sha256": canonical_hash(payload),
        } for payload in payloads]
        expected = hashlib.sha256("".join(item["payload_sha256"] for item in reversed(envelopes)).encode()).hexdigest()
        self.assertEqual(expected, source_hash(envelopes))
        self.assertEqual(expected, source_hash(list(reversed(envelopes))))


class PublisherTests(unittest.TestCase):
    @staticmethod
    def token(role):
        encoded = base64.urlsafe_b64encode(json.dumps({"role": role}).encode()).decode().rstrip("=")
        return f"header.{encoded}.signature"

    def credentials(self):
        return {
            "supabase_url": "https://x", "publishable_key": "pk",
            "current_ar_ingestion_key": self.token("ar_current_ingest"),
            "current_ar_promotion_key": self.token("ar_current_promoter"),
            "operator_verification_key": self.token("ar_current_operator"),
        }

    @staticmethod
    def document():
        payload = {
            "company_code": "SFAB", "company_name": "Structural Fab LLC",
            "fiscal_year": 2026, "fiscal_period": 8,
            "period_start": "2026-08-01", "period_end": "2026-08-31",
            "statement": {"trend": [{"fiscal_year": 2026, "fiscal_period": 8, "controls": {"passed": True}}]},
        }
        envelope = {key: payload[key] for key in ("company_code", "company_name", "fiscal_year", "fiscal_period", "period_start", "period_end")}
        envelope.update(payload=payload, payload_canonical=canonical_json(payload), payload_sha256=canonical_hash(payload))
        return {"run": {
            "source_sha256": source_hash([envelope]), "as_of": "2026-09-14",
            "company_count": 1, "period_count": 1,
            "manifest": [{"company_code": "SFAB", "fiscal_year": 2026, "fiscal_period": 8}],
        }, "periods": [envelope]}

    def test_credentials_forbid_service_role_and_require_three_existing_role_jwts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            path.write_text(json.dumps({"service_role_key": "forbidden"}), encoding="utf-8")
            with self.assertRaisesRegex(CredentialError, "service_role"):
                load_credentials(path)
            credentials = self.credentials()
            credentials["supabase_url"] = "https://example.supabase.co/"
            path.write_text(json.dumps(credentials), encoding="utf-8")
            credentials = load_credentials(path)
            self.assertEqual("https://example.supabase.co", credentials["supabase_url"])

    def test_publish_stages_batches_validates_promotes_then_verifies_pointer_and_hash(self):
        document = self.document()
        credentials = self.credentials()
        calls = []
        def fake_rpc(base, key, token, name, payload):
            calls.append((token, name, payload))
            if name == "financial_stage_run": return "run-id"
            if name == "financial_verify_current": return [{"run_id": "run-id", "source_sha256": document["run"]["source_sha256"], "is_current": True, "period_count": 1}]
            return None
        result = publish(document, credentials, rpc_call=fake_rpc, batch_size=1)
        self.assertEqual(["financial_stage_run", "financial_stage_period_batch", "financial_validate_run", "financial_promote_run", "financial_verify_current"], [c[1] for c in calls])
        self.assertEqual([credentials["current_ar_ingestion_key"]] * 3 + [credentials["current_ar_promotion_key"], credentials["operator_verification_key"]], [c[0] for c in calls])
        self.assertTrue(result["verified"])

    def test_preflight_rejects_tampering_duplicate_keys_bad_batch_and_wrong_jwt_before_rpc(self):
        document = self.document()
        calls = []
        with self.assertRaisesRegex(ValueError, "batch_size"):
            publish(document, self.credentials(), rpc_call=lambda *args: calls.append(args), batch_size=0)
        altered = json.loads(json.dumps(document)); altered["periods"][0]["payload"]["company_name"] = "ALTERED"
        with self.assertRaisesRegex(ValueError, "canonical"):
            validate_document(altered)
        duplicated = json.loads(json.dumps(document)); duplicated["periods"].append(duplicated["periods"][0]); duplicated["run"]["period_count"] = 2
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_document(duplicated)
        bad_credentials = self.credentials(); bad_credentials["current_ar_ingestion_key"] = self.token("service_role")
        with self.assertRaisesRegex(CredentialError, "role"):
            publish(document, bad_credentials, rpc_call=lambda *args: calls.append(args))
        self.assertEqual([], calls)

    def test_publish_rejects_verified_period_count_mismatch(self):
        document = self.document()
        def fake_rpc(base, key, token, name, payload):
            if name == "financial_stage_run": return "run-id"
            if name == "financial_verify_current":
                return [{"run_id": "run-id", "source_sha256": document["run"]["source_sha256"], "is_current": True, "period_count": 2}]
            return True
        with self.assertRaisesRegex(RuntimeError, "period_count"):
            publish(document, self.credentials(), rpc_call=fake_rpc)


class MigrationContractTests(unittest.TestCase):
    def test_migration_has_atomic_immutable_rls_rpc_and_jon_only_contract(self):
        migrations = sorted((ROOT / "supabase" / "migrations").glob("*.sql"))
        self.assertEqual(1, len(migrations))
        sql = migrations[0].read_text(encoding="utf-8").lower()
        for fragment in (
            "financial_runs", "financial_periods", "financial_current", "force row level security",
            "financial_stage_run", "financial_stage_period_batch", "financial_validate_run",
            "financial_promote_run", "financial_verify_current", "financial_statement_catalog",
            "financial_statement_period", "financial_rollback_current",
            "b605d98f-498e-4a94-94cf-e055ed2b5fcc", "revoke all on table",
            "ar_current_ingest", "ar_current_promoter", "ar_current_operator", "raise exception",
        ):
            self.assertIn(fragment, sql)
        self.assertIn("service_role", sql)
        self.assertIn("before update or delete", sql)

    def test_migration_hardens_definers_hashes_idempotency_manifest_and_one_period_reads(self):
        sql = next((ROOT / "supabase" / "migrations").glob("*.sql")).read_text(encoding="utf-8").lower()
        self.assertNotIn("set search_path=public", sql)
        self.assertGreaterEqual(sql.count("set search_path = ''"), 9)
        self.assertIn("unique (source_sha256)", sql)
        self.assertIn("on conflict (source_sha256) do nothing", sql)
        self.assertIn("on conflict (run_id, company_code, fiscal_year, fiscal_period) do nothing", sql)
        self.assertIn("payload_canonical", sql)
        self.assertIn("extensions.digest", sql)
        self.assertIn("string_agg", sql)
        self.assertIn("expected_manifest", sql)
        self.assertIn("previous_run_id", sql)
        self.assertIn("for update", sql)
        self.assertIn("jsonb_array_elements", sql)
        self.assertRegex(sql, r"revoke all on table[^;]+service_role")
        catalog_body = sql.split("financial_statement_catalog", 1)[1].split("create or replace function public.financial_statement_period", 1)[0]
        self.assertNotIn("payload", catalog_body)
        self.assertGreaterEqual(sql.count("auth.uid() is distinct from 'b605d98f-498e-4a94-94cf-e055ed2b5fcc'::uuid"), 2)
        self.assertNotIn("pg_catalog.coalesce(", sql)

    def test_stage_and_validate_take_exclusive_run_locks(self):
        sql = next((ROOT / "supabase" / "migrations").glob("*.sql")).read_text(encoding="utf-8").lower()
        stage = sql.split("financial_stage_period_batch", 1)[1].split("financial_validate_run", 1)[0]
        validate = sql.split("financial_validate_run", 1)[1].split("financial_promote_run", 1)[0]
        self.assertIn("for update", stage)
        self.assertIn("for update", validate)
        self.assertNotIn("for share", stage)
        self.assertNotIn("for share", validate)

    def test_source_connection_prefers_encryption_and_supports_existing_private_sql_viewer(self):
        source = (ROOT / "scripts" / "financial_sync.py").read_text(encoding="utf-8")
        self.assertIn("ODBC Driver 18 for SQL Server", source)
        self.assertIn("Encrypt=yes", source)
        self.assertIn('driver = "SQL Server"', source)
        self.assertIn("Encrypt=no", source)
        self.assertIn("ipaddress.ip_address", source)
        self.assertIn("is_private", source)
        self.assertIn("ApplicationIntent=ReadOnly", source)
        for table in ("SY40100", "SY40101", "GL10110", "GL10111", "GL20000", "GL30000", "GL00100", "GL00105", "GL00102"):
            self.assertIn(table, source)
            self.assertIn(f"dbo.{table}", source.split("def verify_read_only", 1)[1])
        self.assertIn("INSERT", source.split("def verify_read_only", 1)[1])
        self.assertIn("UPDATE", source.split("def verify_read_only", 1)[1])
        self.assertIn("DELETE", source.split("def verify_read_only", 1)[1])


if __name__ == "__main__":
    unittest.main()
