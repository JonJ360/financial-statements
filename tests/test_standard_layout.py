"""Common template layout and SMI segment scope without accounting plugs."""
import copy
from scripts import financial_sync as sync


def row(number, description, category, amount=10, index=1):
    return sync.map_account_row(dict(account_index=index, account_number=number,
        account_description=description, category=category, category_description=description,
        posting_type=1, typical_balance=0, period_debit=amount, period_credit=0,
        ytd_debit=amount, ytd_credit=0))


def test_account_meaning_not_universal_number():
    for code in [c.code for c in sync.ACTIVE_COMPANIES]:
        for number, description, category, expected in [
            ('623-00-00', 'SHOP PAYROLL', 36, 'employee_related_expenses'),
            ('623-00-00', 'INTEREST EXPENSE', 38, 'other_operating_expenses'),
            ('660-00-00', 'OFFICE STAFF', 36, 'other_operating_expenses'),
            ('680-00-00', 'HUMAN RESOURCES', 42, 'other_operating_expenses'),
            ('603-00-00', 'WORKERS COMP & UNEMP', 39, 'employee_related_expenses'),
            ('602-00-00', 'FICA TAXES', 39, 'employee_related_expenses'),
            ('610-00-00', 'INSURANCE EMPLOYEE', 35, 'other_operating_expenses'),
            ('699-00-00', 'PROFIT SHARING EXPENSE', 37, 'other_operating_expenses'),
            ('701-00-00', 'DISCOUNTS EARNED', 43, 'other_operating_expenses'),
            ('901-00-00', 'INCOME TAX EXPENSE', 42, 'income_tax'),
        ]:
            assert sync._income_section(row(number, description, category), code) == expected


def test_smi_coop_stock_template_scope():
    account={'category_id':12,'account_description':'COOP STOCK'}
    assert sync._balance_section(account,'SMI')=='property_plant_equipment'
    assert sync._balance_section(account,'RRCOF')=='other_assets'


def test_common_headings_totals_and_signs():
    for company in sync.ACTIVE_COMPANIES:
        accounts=[row('701-00-00','DISCOUNTS EARNED',43,-10)]
        original=copy.deepcopy(accounts)
        rows=sync._presentation(company,accounts,[])['income_statement']
        by_id={r['id']:r for r in rows}
        assert by_id['net_income']['label']=='Income(Loss)'
        for section in ['revenue','cost_of_revenue','employee_related_expenses','other_operating_expenses','income_tax']:
            assert section+'_heading' in by_id
        assert by_id['other_operating_expenses']['values']['current_period']=='-10.00000'
        assert by_id['net_income']['values']['current_period']=='10.00000'
        assert accounts==original


def test_smi_branches_prior_only_no_plugs_and_null_comparison():
    company=next(c for c in sync.ACTIVE_COMPANIES if c.code=='SMI')
    prior=[row('699-00-02','PROFIT SHARING EXPENSE - FF',37,20)]
    current=[row('601-00-00','PAYROLL',36,30)]
    branches=sync._branch_statements(company,current,prior,[],[],True)
    assert set(branches)=={'00','01','02','03'}
    ff=branches['02']
    assert ff['branch_name']=='Fergus Falls'
    net=next(r for r in ff['presentation']['income_statement'] if r['id']=='net_income')
    assert net['values']['current_period']=='0.00000'
    assert net['values']['prior_period']=='-20.00000'
    assert ff['comparison']['available'] is True
    assert branches['00']['balance_sheet_difference']=='30.00000'
    assert branches['00']['bridge_controls']['passed']
    assert all(r.get('balance_section')!='allocation' for r in branches['00']['balance_sheet'])
    missing=sync._branch_statements(company,current,[],[],[],False)
    assert next(r for r in missing['02']['presentation']['income_statement'] if r['id']=='net_income')['values']['prior_ytd'] is None
    assert sync._branch_statements(sync.ACTIVE_COMPANIES[0],current,prior,[],[],True)=={}


def test_inactive_unknown_branch_retained_but_not_selectable():
    company=next(c for c in sync.ACTIVE_COMPANIES if c.code=='SMI')
    zero=row('999-00-99','CHECKLINKS',35,0)
    branches=sync._branch_statements(company,[zero],[],[],[],False)
    assert '99' in branches
    assert branches['99']['selectable'] is False
    assert branches['99']['income_statement']==[zero]
    assert all(branches[code]['selectable'] for code in sync.SMI_BRANCHES)
    prior=row('999-00-99','CHECKLINKS',35,1)
    assert sync._branch_statements(company,[zero],[prior],[],[],True)['99']['selectable'] is True
