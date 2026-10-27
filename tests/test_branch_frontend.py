import json
from scripts import financial_sync as sync
from test_standard_layout import row
from test_statement_layout import run_js


def test_opat_scope_selector_and_exports(tmp_path):
    company=next(c for c in sync.ACTIVE_COMPANIES if c.code=='OPAT')
    accounts=[row('601-00-01','PAYROLL',36,30), row('999-99-99','CHECKLINKS',35,0,2)]
    period=dict(year=2026,period=12,start='2026-12-01',end='2026-12-31')
    payload=sync._period_payload(company,period,accounts,[],[],[accounts],[period])['payload']
    fixture=tmp_path/'property.json'
    fixture.write_text(json.dumps(payload),encoding='utf-8')
    code='const payload=JSON.parse(fs.readFileSync('+json.dumps(str(fixture))+",'utf8'));"
    run_js(code+"""
state.periodPayload=payload;populateBranches();
assert.equal($('branchLabel').textContent,'Property');
assert($('branchSelect').innerHTML.includes('All properties'));
assert(!$('branchSelect').innerHTML.includes('value="99"'));
assert.equal($('branchFilter').hidden,false);
const report=normalizeReport(payload,'01');
assert(report.meta.company_name.includes('Ten Acres'));
assert.equal(report.totals.net_income,-30);
assert.equal(report.monthly_recon,payload.monthly_recon);
assert(report.scope_note.includes('company-wide'));
assert.equal(report.trend[0].net_income,'-30.00000');
const wb=FinancialWorkbook.buildFinancialWorkbook(report,{month:'December'});
assert(wb.getWorksheet('Income Statement').getCell('A1').value.includes('Ten Acres'));
assert(wb.getWorksheet('Balance Sheet').getCell('A4').value.includes('Shared/unassigned'));
assert.deepEqual(pdfRows(report.income,'income').map(r=>r.label.trim()),FinancialWorkbook.traditionalRows(report.income).map(r=>r.label));
state.report=report;$('companySelect').value='OPAT';$('monthSelect').selectedOptions=[{dataset:{year:'2026',period:'12'}}];
assert.equal(fileBase(),'OPAT-property-01-2026-12-financial-statements');
assert.throws(()=>normalizeReport(payload,'missing'),/branch|property/i);
const unrelated={...payload,company_code:'SFAB'};
assert.throws(()=>normalizeReport(unrelated,'01'),/branch|property/i);
state.periodPayload={...payload,company_code:'SMI'};populateBranches();
assert.equal($('branchLabel').textContent,'Branch');
assert($('branchSelect').innerHTML.includes('All branches'));
state.periodPayload=null;populateBranches();
assert.equal($('branchFilter').hidden,true);assert.equal($('branchSelect').value,'');
""")


def test_branch_scope_and_shared_export_contract(tmp_path):
    company=next(c for c in sync.ACTIVE_COMPANIES if c.code=='SMI')
    accounts=[row('601-00-00','PAYROLL',36,30)]
    period=dict(year=2026,period=9,start='2026-09-01',end='2026-09-30')
    payload=sync._period_payload(company,period,accounts,[])['payload']
    # The deliberately unbalanced company fixture must still fail closed.
    fixture=tmp_path/'branch.json'
    fixture.write_text(json.dumps(payload),encoding='utf-8')
    code='const payload=JSON.parse(fs.readFileSync('+json.dumps(str(fixture))+",'utf8'));"
    run_js(code+"""
assert.throws(()=>normalizeReport(payload),/tie-out/);
const report=normalizeReport(payload,'00');
assert.equal(report.meta.branch_code,'00');
assert(report.meta.company_name.includes('Fargo'));
assert.equal(report.totals.net_income,-30);
assert.equal(report.scope_note.includes('no allocation'),true);
assert.throws(()=>normalizeReport(payload,'99'),/branch/i);
const wb=FinancialWorkbook.buildFinancialWorkbook(report,{month:'September'});
assert(wb.getWorksheet('Income Statement').getCell('A1').value.includes('Fargo'));
assert(wb.getWorksheet('Balance Sheet').getCell('A4').value.includes('unallocated'));
assert(wb.getWorksheet('Account Detail'));
assert(wb.worksheets.every(s=>!s.views.some(v=>v.state==='frozen'||v.state==='split')));
assert.equal(report.income.find(r=>r.id==='net_income').values.prior_ytd,null);
const bad=JSON.parse(JSON.stringify(payload));bad.statement.branches['00'].kpis.net_income='999';
assert.throws(()=>normalizeReport(bad,'00'),/tie-out/);
const pageRows=Array.from({length:57},(_,i)=>({id:i,row_type:i===28?'heading':'account'}));
const pages=pdfPageChunks(pageRows,30);
assert.deepEqual(pages.flat(),pageRows);
assert(pages.every(p=>p.length<=30&&p.length>=10&&p.at(-1).row_type!=='heading'));
assert(html.includes('#branchFilter[hidden]{display:none!important}'));
""")
