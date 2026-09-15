"""Static contract tests for the GitHub Pages financial statements client."""
from html.parser import HTMLParser
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "index.html"


class Markup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids: set[str] = set()
        self.scripts: list[dict[str, str | None]] = []
        self.meta: list[dict[str, str | None]] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(values["id"])
        if tag == "script":
            self.scripts.append(values)
        if tag == "meta":
            self.meta.append(values)

    def handle_data(self, data):
        self.text.append(data)


def source() -> str:
    assert INDEX.exists(), "index.html must exist"
    return INDEX.read_text(encoding="utf-8")


def markup() -> Markup:
    parsed = Markup()
    parsed.feed(source())
    return parsed


def test_single_page_has_required_financial_statement_controls():
    parsed = markup()
    required_ids = {
        "authGate", "password", "loginBtn", "signOutBtn",
        "companySelect", "periodSelect", "incomeTab", "balanceTab",
        "incomeStatement", "balanceSheet", "accountDrilldown",
        "downloadExcel", "downloadPdf", "trendChart", "positionChart",
        "statusMessage",
    }
    assert required_ids <= parsed.ids
    visible_text = " ".join(parsed.text)
    for label in (
        "Financial Statements", "Income Statement", "Balance Sheet",
        "Download Excel", "Download PDF", "V1.1",
    ):
        assert label in visible_text


def test_client_uses_supabase_password_auth_and_only_required_rpcs():
    html = source()
    assert "window.supabase" in html and "createClient" in html
    assert "signInWithPassword" in html
    assert ".auth.signOut()" in html
    assert "financial_read_current" not in html
    assert re.search(r"\.rpc\(\s*['\"]financial_statement_catalog['\"]\s*\)", html)
    assert re.search(r"\.rpc\(\s*['\"]financial_statement_period['\"]\s*,\s*\{\s*p_company_code\s*:", html)
    for parameter in ("p_company_code", "p_fiscal_year", "p_fiscal_period"):
        assert parameter in html
    assert "smi_sales_current_snapshot" not in html


def test_jon_login_matches_other_apps_pin_flow():
    html = source()
    assert 'label for="password">PIN</label>' in html
    assert "const AUTH_EMAIL='jonj@360-llc.com'" in html
    assert "email:AUTH_EMAIL" in html
    assert 'id="email"' not in html


def test_client_fails_closed_without_session_or_when_rpc_fails():
    html = source()
    assert re.search(r"if\s*\(\s*!session(?:\?\.)?(?:access_token)?", html)
    assert "showAuthGate" in html
    assert "clearFinancialData" in html
    assert re.search(r"if\s*\(\s*(?:catalogResult|result|periodResult)\.error\s*\)\s*throw", html)
    assert re.search(r"catch\s*\([^)]*\)\s*\{[^}]*clearFinancialData", html, re.S)
    forbidden = (
        "mockData", "mock_data", "sampleData", "sample_data",
        "demoData", "demo_data", "data/financial", "localhost",
        "127.0.0.1",
    )
    assert not any(token in html for token in forbidden)


def test_financial_payload_is_not_written_to_browser_storage():
    html = source()
    assert "localStorage" not in html
    assert "indexedDB" not in html
    assert "sessionStorage" not in html
    assert re.search(r"persistSession\s*:\s*true", html)
    assert not re.search(r"state\.catalog\s*=.*payload", html)


def test_exports_are_real_pinned_libraries_and_spreadsheet_cells_are_sanitized():
    html = source()
    parsed = markup()
    srcs = [script.get("src") or "" for script in parsed.scripts]
    assert "vendor/xlsx-0.20.3.min.js" in srcs
    assert "vendor/jspdf-2.5.2.min.js" in srcs
    assert "vendor/jspdf-autotable-3.8.4.min.js" in srcs
    assert all(not re.match(r"https?://", src) for src in srcs)
    for src in srcs:
        if src:
            assert (ROOT / src).is_file(), f"vendored script is missing: {src}"
    assert "XLSX.writeFile" in html
    assert "jsPDF" in html and "autoTable" in html and ".save(" in html
    assert "sanitizeSpreadsheetCell" in html
    assert re.search(r"^[^\n]*[=+\-@]", html, re.M), "formula-leading characters must be handled"


def test_charts_and_drilldown_are_data_driven():
    html = source()
    assert "vendor/chart-4.4.7.min.js" in html
    for metric in (
        "revenue", "gross_profit", "expenses", "net_income",
        "assets", "liabilities", "equity",
    ):
        assert metric in html
    assert "renderTrendChart" in html
    assert "renderPositionChart" in html
    assert "renderDrilldown" in html
    assert re.search(r"addEventListener\(\s*['\"]click['\"]", html)


def test_client_consumes_exact_nested_backend_payload_and_balance_fields():
    html = source()
    assert "root.payload??root" in html
    assert "nested.statement" in html
    assert "statement.income_statement" in html
    assert "statement.balance_sheet" in html
    assert "statement.kpis" in html
    assert "statement.trend" in html
    assert "row.period_balance" in html
    assert "row.ending_balance" in html
    assert "fiscal_period" in html
    assert "period_id" not in html
    assert "root.payload??root" in html
    assert "Net Income" in html
    assert "Total Assets" in html
    assert "Total Liabilities &amp; Equity" in html
    assert "Statement total" not in html


def test_page_is_responsive_accessible_and_blue_white_without_purple():
    html = source()
    parsed = markup()
    assert any(meta.get("name") == "viewport" for meta in parsed.meta)
    assert "@media" in html
    assert "aria-live" in html
    assert ":focus-visible" in html
    assert "--blue" in html and "#ffffff" in html.lower()
    forbidden_colors = ("purple", "violet", "#800080", "#7c3aed", "#8b5cf6", "#a855f7")
    assert not any(color in html.lower() for color in forbidden_colors)


def test_authenticated_page_has_no_third_party_runtime_scripts_and_uses_csp():
    html = source()
    parsed = markup()
    assert 'http-equiv="Content-Security-Policy"' in html
    assert "script-src 'self' 'unsafe-inline'" in html
    assert "connect-src 'self' https://inhwadbibwkakacdvoxu.supabase.co" in html
    assert "cdn.jsdelivr.net" not in html
    assert "cdn.sheetjs.com" not in html
    assert all(not re.match(r"https?://", script.get("src") or "") for script in parsed.scripts)
    assert "vendor/supabase-2.49.1.min.js" in html
