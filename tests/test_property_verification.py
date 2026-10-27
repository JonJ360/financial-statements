"""Independent offline verifier must detect scoped and consolidated regressions."""
import copy
import pytest
from scripts import financial_sync as sync
from scripts import verify_standard_layout as verifier
from test_standard_layout import row


def fixture():
    company = next(c for c in sync.ACTIVE_COMPANIES if c.code == 'OPAT')
    periods = [dict(year=y, period=12, start=f'{y}-12-01', end=f'{y}-12-31') for y in (2025, 2026)]
    raw = {(y, 12): [row('601-00-01', 'PAYROLL', 36, y-2000), row('999-99-99','CHECKLINKS',35,0,2)] for y in (2025,2026)}
    current = sync._period_payload(company, periods[1], raw[(2026,12)], [], raw[(2025,12)], list(raw.values()), periods)['payload']['statement']
    return company, current, raw


def test_verifier_checks_every_consolidated_kpi():
    _, statement, _ = fixture()
    changed = copy.deepcopy(statement)
    changed['kpis']['operating_expenses'] = '999.00000'
    with pytest.raises(AssertionError):
        verifier.verify_consolidated_kpis(statement, changed)


@pytest.mark.parametrize('mutation', ['prior', 'current', 'trend', 'unknown', 'residual'])
def test_verifier_rejects_scoped_corruption(mutation):
    company, statement, raw = fixture()
    verifier.verify_scope_partition(company, statement, raw, (2026,12))
    bad = copy.deepcopy(statement)
    branch = bad['branches']['01']
    if mutation in ('prior', 'current'):
        key = 'prior_ytd' if mutation == 'prior' else 'current_period'
        next(r for r in branch['presentation']['income_statement'] if r['id']=='net_income')['values'][key] = '999.00000'
    elif mutation == 'trend':
        branch['trend'][0]['cost_of_goods_sold'] = '999.00000'
    elif mutation == 'unknown':
        del bad['branches']['99']
    else:
        branch['balance_sheet_difference'] = '999.00000'
    with pytest.raises(AssertionError):
        verifier.verify_scope_partition(company, bad, raw, (2026,12))
