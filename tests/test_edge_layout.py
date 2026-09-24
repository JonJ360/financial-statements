"""Opt-in local Edge visual/export smoke: FINANCIAL_EDGE_VERIFY=1 pytest -s."""
import json
import os
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.getenv('FINANCIAL_EDGE_VERIFY') != '1', reason='Opt-in local Edge/snapshot verification')
def test_edge_statement_layout_and_downloads():
    from playwright.sync_api import sync_playwright
    snapshot = json.loads((ROOT / 'data/financial-statements.json').read_text())
    payload = max(snapshot['periods'], key=lambda p: len(p['payload']['statement']['presentation']['income_statement']))['payload']
    output = Path(os.environ['LOCALAPPDATA']) / 'hermes/cache/scratch/financial-v26-edge'
    output.mkdir(parents=True, exist_ok=True)
    errors = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1100}, accept_downloads=True)
        page.on('pageerror', lambda e: errors.append(str(e)))
        def route_request(route):
            url = route.request.url
            if not url.startswith('http://financial.local/'):
                route.abort()
                return
            relative = url.split('http://financial.local/', 1)[1] or 'index.html'
            target = ROOT / relative
            if target.is_file():
                route.fulfill(path=str(target), content_type='text/html' if relative.endswith('.html') else 'application/javascript')
            else:
                route.fulfill(status=404, body='Not found')
        page.route('**/*', route_request)
        page.goto('http://financial.local/')
        page.wait_for_function("document.getElementById('authGate').classList.contains('hidden') === false")
        page.evaluate("""payload=>{
          state.report=normalizeReport(payload);
          $('companySelect').innerHTML=`<option value="${text(payload.company_code)}">${text(payload.company_name)}</option>`;
          $('monthSelect').innerHTML=`<option data-year="${payload.fiscal_year}" data-period="${payload.fiscal_period}">${monthName(payload)}</option>`;
          showApplication({user:{email:'Local verification — no live connection'}});render();
        }""", payload)
        expected_status = payload.get('monthly_recon', {}).get('status', 'unknown')
        assert page.locator('#monthlyRecon').is_checked() == (expected_status == 'completed')
        assert page.locator('#monthlyRecon').is_disabled()
        assert expected_status.capitalize() in page.locator('#monthlyReconNote').inner_text()
        page.evaluate("window.scrollTo(0,document.getElementById('statements').getBoundingClientRect().top+window.scrollY-105)")
        page.screenshot(path=str(output / 'income.png'))
        for type_, body in [('income', '#incomeBody'), ('balance', '#balanceBody')]:
            if type_ == 'balance':
                page.click('#balanceTab')
                page.screenshot(path=str(output / 'balance.png'))
            assert page.locator(body).evaluate("""body=>{
              const rows=[...body.rows];const data=state.report[body.id==='incomeBody'?'income':'balance'];
              return data.filter(r=>r.row_type==='account').every(r=>rows.findIndex(x=>x.dataset.rowId===r.id)<rows.findIndex(x=>x.dataset.rowId===r.parent_id));
            }""")
            section=page.locator(body+' .category-row').first
            section.click()
            assert 'account' in page.locator('#drilldownBody').inner_text()
            section.press('Enter')
            assert 'account' in page.locator('#drilldownBody').inner_text()
        for button, name in [('#downloadPdf','statements.pdf'),('#downloadExcel','statements.xlsx')]:
            with page.expect_download() as download:
                page.click(button)
            download.value.save_as(str(output/name))
            assert (output/name).stat().st_size > 1000
        for status in ['completed','open','mixed','unknown']:
            page.evaluate("""status=>{state.report.monthly_recon={status,completed:status==='completed',basis:'GP fiscal-period module closure'};renderMonthlyRecon()}""",status)
            assert page.locator('#monthlyRecon').is_checked() == (status=='completed')
        page.set_viewport_size({'width':390,'height':844})
        page.locator('#statements').scroll_into_view_if_needed()
        page.screenshot(path=str(output/'mobile.png'))
        assert not errors, errors
        print('Edge headless screenshots and real PDF/XLSX:', output)
        browser.close()
