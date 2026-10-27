"""OPAT property partition: synthetic fixtures only; never GP business data."""
from scripts import financial_sync as sync
from test_standard_layout import row


def test_opat_property_payload_uses_third_segment_and_keeps_unknown():
    company = next(c for c in sync.ACTIVE_COMPANIES if c.code == 'OPAT')
    current = [row('601-77-01', 'PAYROLL', 36, 30),
               row('601-01-00', 'PAYROLL', 36, 10, 2),
               row('999-99-99', 'CHECKLINKS', 35, 0, 3)]
    prior = [row('601-00-02', 'PAYROLL', 36, 20)]
    period = dict(year=2026, period=12, start='2026-12-01', end='2026-12-31')
    payload = sync._period_payload(company, period, current, [], prior, [current], [period])['payload']
    branches = payload['statement'].get('branches', {})
    assert set(branches) == {'00', '01', '02', '03', '04', '05', '99'}
    assert {code: branches[code]['branch_name'] for code in sorted(branches) if code != '99'} == {
        '00': 'Shared/unassigned', '01': 'Ten Acres', '02': 'Pelican Retreat',
        '03': 'Arizona', '04': 'Alaska', '05': 'Montana'}
    assert branches['01']['income_statement'] == [current[0]]
    assert branches['00']['income_statement'] == [current[1]]
    assert branches['99']['income_statement'] == [current[2]]
    assert branches['99']['selectable'] is False
    assert all(branches[c]['selectable'] for c in ['00','01','02','03','04','05'])
    assert branches['01']['balance_sheet_difference'] == '30.00000'
    assert '30.00000' in branches['01']['scope_note']
    assert 'Assets less liabilities and equity' in branches['01']['scope_note']
    assert 'not standalone legal-entity' in branches['01']['scope_note']
    assert 'Shared/unassigned' in branches['01']['scope_note']
    assert 'no allocation or balancing plug' in branches['01']['scope_note']
    assert branches['01']['trend'][0]['net_income'] == '-30.00000'
    prior_net = next(r for r in branches['02']['presentation']['income_statement'] if r['id'] == 'net_income')
    assert prior_net['values']['prior_ytd'] == '-20.00000'
    for key, value in payload['statement']['kpis'].items():
        assert sum(sync.D(b['kpis'][key]) for b in branches.values()) == sync.D(value)
    assert all(b['bridge_controls']['passed'] for b in branches.values())
