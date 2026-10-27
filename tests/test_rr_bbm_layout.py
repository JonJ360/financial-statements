"""Historical June 2026 sent-report contract, exact GP company/account scope."""
import copy
from scripts import financial_sync as sync

ORDERS = {
    'RRCOF': ['604','606','609','610','612','613','614','616','618','660','665','680','620','621','622','623','624','625'],
    'BBMTS': ['618','610','613','650','621','606','622','660','612','623','620','607','626','665','625','624','614','616','609','680','604','701'],
}

def account(number, category):
    return dict(account_number=number+'-00-00',account_description=number,posting_type=1,category_id=category,period_debit='10',period_credit='0',ytd_debit='10',ytd_credit='0')

def test_historical_company_mapping_order_and_sign():
    categories = {'601':36,'602':39,'603':39,'606':40,'623':38,'660':42,'680':42,'701':43}
    for code, order in ORDERS.items():
        company=next(c for c in sync.ACTIVE_COMPANIES if c.code==code)
        rows=[account(n,categories.get(n,35)) for n in dict.fromkeys(['601','602','603',*order,'701'])]
        rows[0]['account_description']='PAYROLL'
        rows[1]['account_description']='FICA TAXES--'
        rows[2]['account_description']='WORKERS COMP & UNEMP--'
        original=copy.deepcopy(rows)
        result=sync._presentation(company,rows,[])['income_statement']
        employee=[r['account_number'] for r in result if r.get('parent_id')=='employee_related_expenses']
        assert employee==['601-00-00','602-00-00','603-00-00']
        other=[r for r in result if r.get('parent_id')=='other_operating_expenses']
        assert [r['account_number'] for r in other][:len(order)]==[n+'-00-00' for n in order]
        assert next(r for r in other if r['account_number']=='701-00-00')['values']['current_period']=='10.00000'
        assert next(r for r in result if r['id']=='net_income')['values']['current_period']==sync._money(-sum((sync._raw_balance(r) for r in rows),sync.ZERO))
        assert any(r['id']=='operating_expenses_heading' for r in result)
        assert rows==original


def test_real_snapshot_all_periods_and_shared_exports(tmp_path):
    import json
    from pathlib import Path
    import pytest
    from test_statement_layout import run_js
    import os
    path=Path(os.environ.get('FINANCIAL_REBUILT_SNAPSHOT', Path(__file__).resolve().parents[1]/'data/financial-statements.json'))
    if not path.exists():
        pytest.skip('Private real snapshot not committed')
    snapshot=json.loads(path.read_text(encoding='utf-8'))
    original=copy.deepcopy(snapshot)
    lookup={(p['company_code'],p['fiscal_year'],p['fiscal_period']):p for p in snapshot['periods']}
    companies={c.code:c for c in sync.ACTIVE_COMPANIES}
    changed=0
    for period in snapshot['periods']:
        code=period['company_code']
        statement=period['payload']['statement']
        prior=lookup.get((code,period['fiscal_year']-1,period['fiscal_period']))
        accounts=statement['income_statement']+statement['balance_sheet']
        prior_accounts=[] if prior is None else prior['payload']['statement']['income_statement']+prior['payload']['statement']['balance_sheet']
        presentation=sync._presentation(companies[code],accounts,prior_accounts)
        if not statement['comparison']['available']:
            sync._mark_missing_comparison(presentation)
        old=statement['presentation']
        old_balance={r['id']:r['values'] for r in old['balance_sheet'] if r['row_type']!='heading'}
        new_balance={r['id']:r['values'] for r in presentation['balance_sheet'] if r['row_type']!='heading'}
        for identifier, values in old_balance.items():
            if code=='SMI' and identifier in {'property_plant_equipment','other_assets'}:
                continue
            old_row=next(r for r in old['balance_sheet'] if r['id']==identifier)
            if old_row['row_type']=='account' and identifier not in new_balance:
                # Only the SMI COOP STOCK section-derived ID may change.
                assert code=='SMI' and 'COOP STOCK' in old_row['label']
                new_row=next(r for r in presentation['balance_sheet'] if r.get('account_number')==old_row['account_number'] and r.get('parent_id')=='property_plant_equipment')
                assert new_row['values']==values
            else:
                assert new_balance[identifier]==values
        old_net=next(r for r in old['income_statement'] if r['id']=='net_income')
        new_net=next(r for r in presentation['income_statement'] if r['id']=='net_income')
        assert new_net['values']==old_net['values']
        changed+=1
        before={r['account_number']:r for r in old['income_statement'] if r['row_type']=='account'}
        after={r['account_number']:r for r in presentation['income_statement'] if r['row_type']=='account'}
        assert before.keys()==after.keys()
        for number,old_row in before.items():
            values=old_row['values']
            flip=old_row['parent_id']=='other_income' and after[number]['parent_id']=='other_operating_expenses'
            expected={k:None if v is None else sync._money(-sync.D(v)) for k,v in values.items()} if flip else values
            assert after[number]['values']==expected
        raw=[a for a in accounts if not a.get('synthetic')]
        metrics=sync._statement_metrics(raw,code)
        updated=copy.deepcopy(statement)
        updated['presentation']=presentation
        updated['kpis']['operating_expenses']=sync._money(metrics['operating_expenses'])
        assert sync.validate_statement_bridge(updated)['passed']
        # Raw snapshot stays untouched; exercise the generated rows, not stale stored layout.
        period['payload']['statement']['presentation']=presentation
        # Publication canonical strings duplicate payloads and can exceed Node's
        # single-string ceiling; they are not part of the browser/export contract.
        period.pop('payload_canonical', None)
    generated=tmp_path/'layout.json'
    generated.write_text(json.dumps(snapshot,separators=(',',':')),encoding='utf-8')
    # The only in-memory changes are presentation; raw GP rows, balances and KPI source remain intact.
    for old,new in zip(original['periods'],snapshot['periods']):
        for key in ('income_statement','balance_sheet','kpis','trend'):
            assert old['payload']['statement'][key]==new['payload']['statement'][key]
    output=run_js("""
const snapshot=JSON.parse(fs.readFileSync(PATH,'utf8'));
let accounts=0,categories=0; const companies=new Set();
for(const period of snapshot.periods){
 const report=normalizeReport(period);companies.add(report.meta.company_code);
 const wb=FinancialWorkbook.buildFinancialWorkbook(report,{month:'Period '+report.meta.fiscal_period});
 for(const type of ['income','balance']){
  const source=report[type],rows=FinancialWorkbook.traditionalRows(source),pdf=pdfRows(source,type),screen=statementRows(source,type);
  const sheet=wb.getWorksheet(type==='income'?'Income Statement':'Balance Sheet');
  assert.equal(rows.length,source.length);
  const keys=type==='income'?['current_period','current_ytd','prior_period','prior_ytd']:['current_balance','prior_balance'];
  rows.forEach((row,index)=>{
   assert.equal(pdf[index].label.trim(),row.label);
   assert.equal(String(sheet.getCell(index+7,1).value).trim(),row.label);
   keys.forEach((key,c)=>assert.equal(sheet.getCell(index+7,c+2).value,row.values?.[key]==null?null:Number(row.values[key])));
   if(row.row_type==='account') accounts++;
   if(row.row_type==='section'){
    categories++;
    for(const child of source.filter(r=>r.parent_id===row.id)){
     assert(rows.findIndex(r=>r.id===child.id)<index);
     assert(screen.indexOf(`data-row-id="${text(child.id)}"`)<screen.indexOf(`data-row-id="${text(row.id)}"`));
    }
   }
  });
 }
}
console.log(JSON.stringify({periods:snapshot.periods.length,companies:companies.size,accounts,categories}));
""".replace('PATH',json.dumps(str(generated))))
    print('Real rebuilt layout / shared screen-PDF-XLSX:',output,'target periods:',changed)


def test_real_snapshot_full_builder_controls_and_trends():
    import json
    from pathlib import Path
    import pytest
    import os
    path=Path(os.environ.get('FINANCIAL_REBUILT_SNAPSHOT', Path(__file__).resolve().parents[1]/'data/financial-statements.json'))
    if not path.exists():
        pytest.skip('Private real snapshot not committed')
    snapshot=json.loads(path.read_text(encoding='utf-8'))
    count=0
    for company in sync.ACTIVE_COMPANIES:
        source=[p for p in snapshot['periods'] if p['company_code']==company.code]
        periods=[dict(year=p['fiscal_year'],period=p['fiscal_period'],start=p['period_start'],end=p['period_end']) for p in source]
        rows={(p['fiscal_year'],p['fiscal_period']):[a for a in p['payload']['statement']['income_statement']+p['payload']['statement']['balance_sheet'] if not a.get('synthetic')] for p in source}
        rebuilt=sync.build_company_payloads(company,periods,rows)
        for before,after in zip(source,rebuilt):
            old,new=before['payload']['statement'],after['payload']['statement']
            assert new['bridge_controls']['passed']
            assert new['income_statement']==old['income_statement']
            assert new['balance_sheet']==old['balance_sheet']
            for key,value in old['kpis'].items():
                if key!='operating_expenses':
                    assert new['kpis'][key]==value
            for trend in new['trend']:
                raw=rows[(trend['fiscal_year'],trend['fiscal_period'])]
                metrics=sync._statement_metrics(raw,company.code)
                assert trend['operating_expenses']==sync._money(metrics['operating_expenses'])
                assert trend['net_income']==sync._money(metrics['net_income'])
            count+=1
    assert count==snapshot['run']['period_count']
    print('Full builder GP controls, KPI bridges, net income and trends:',count,'periods')

