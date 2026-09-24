"""Executable client/export regression tests; no network or ERP access."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def run_js(body):
    setup = """
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const FinancialWorkbook=require('./scripts/export_xlsx.js');
const html=fs.readFileSync('index.html','utf8');
const code=html.split('<script>')[1].split('async function initialize')[0];
const elements=new Map();
const document={getElementById:id=>{if(!elements.has(id))elements.set(id,{classList:{toggle(){},add(){},remove(){}},setAttribute(){},selectedOptions:[]});return elements.get(id)}};
vm.runInThisContext(code,{filename:'client.js'});
"""
    # Lexical globals needed by the real inline client functions.
    setup = setup.replace("const FinancialWorkbook=", "global.FinancialWorkbook=").replace("const document=", "global.document=")
    result = subprocess.run(["node", "-e", "global.window={};" + setup + body], cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_local_snapshot_all_companies_and_periods():
    import pytest
    if not (ROOT / 'data/financial-statements.json').exists():
        pytest.skip('Local sensitive snapshot is intentionally not committed')
    output = run_js("""
const snapshot=JSON.parse(fs.readFileSync('data/financial-statements.json','utf8'));
let categories=0,accounts=0;const companies=new Set();
for(const period of snapshot.periods){
const before=JSON.stringify(period);const report=normalizeReport(period);
companies.add(report.meta.company_code);
assert.deepEqual(report.monthly_recon,period.payload.monthly_recon??null);
if(!period.payload.monthly_recon)assert.equal(FinancialWorkbook.monthlyReconStatus(report.monthly_recon).status,'unknown');
const wb=FinancialWorkbook.buildFinancialWorkbook(report,{month:'Period '+report.meta.fiscal_period});
for(const type of ['income','balance']){
const source=report[type],ordered=FinancialWorkbook.traditionalRows(source),screen=statementRows(source,type),pdf=pdfRows(source,type);
const sheet=wb.getWorksheet(type==='income'?'Income Statement':'Balance Sheet');
assert.equal(ordered.length,source.length);assert.equal(new Set(ordered.map(r=>r.id)).size,source.length);
assert.deepEqual(ordered.filter(r=>r.row_type==='account').map(r=>r.id),source.filter(r=>r.row_type==='account').map(r=>r.id));
for(const row of source){
const index=ordered.findIndex(r=>r.id===row.id),out=ordered[index];
if(row.row_type!=='heading')assert.deepEqual(out.values,row.values);
assert.equal(pdf[index].label.trim(),out.label);
assert.equal(String(sheet.getCell(index+7,1).value).trim(),out.label);
const keys=type==='income'?['current_period','current_ytd','prior_period','prior_ytd']:['current_balance','prior_balance'];
keys.forEach((key,column)=>assert.equal(sheet.getCell(index+7,column+2).value,out.values?.[key]==null?null:Number(out.values[key])));
if(row.row_type==='account')accounts++;
if(row.row_type==='section'){
categories++;
for(const detail of source.filter(r=>r.parent_id===row.id)){
assert(ordered.findIndex(r=>r.id===detail.id)<index,'category must follow every child');
assert(screen.indexOf(`data-row-id="${text(detail.id)}"`)<screen.indexOf(`data-row-id="${text(row.id)}"`));
}
}
}
}
assert.equal(JSON.stringify(period),before);
}
assert.equal(snapshot.periods.length,snapshot.run.period_count);
assert.equal(companies.size,snapshot.run.company_count);
console.log(JSON.stringify({periods:snapshot.periods.length,companies:companies.size,categories,accounts}));
""")
    print(output)


def test_recon_is_read_only_fail_closed_and_in_both_exports():
    run_js("""
assert(html.includes('id="monthlyRecon"') && /id="monthlyRecon"[^>]*disabled/.test(html));
const basis='GP fiscal-period module closure';
for(const [metadata,expected] of [[undefined,'unknown'],[{status:'completed',completed:true,basis},'completed'],[{status:'completed',completed:false,basis},'unknown'],[{status:'completed',completed:true},'unknown'],[{status:'open',completed:false,basis},'open'],[{status:'mixed',completed:false,basis},'mixed'],[{status:'unknown',completed:false,basis},'unknown']]){
const report={meta:{fiscal_year:2026,fiscal_period:5},monthly_recon:metadata,income:[],balance:[]};
const status=FinancialWorkbook.monthlyReconStatus(metadata);
assert.equal(status.status,expected);assert.equal(status.completed,expected==='completed');
const boxes=[],marks=[];const doc={setFontSize(){},setDrawColor(){},setLineWidth(){},rect(...args){boxes.push(args)},line(...args){marks.push(args)},text(){},splitTextToSize(t){return [t]},getTextWidth(){return 100}};
state.report=report;drawPdfRecon(doc);assert.equal(boxes[0][0],638,'PDF checkbox adjacent to its right-aligned label');assert.equal(marks.length,expected==='completed'?2:0);
state.report=report;renderMonthlyRecon();assert.equal($('monthlyRecon').checked,expected==='completed');
assert($('monthlyReconNote').textContent.includes(status.explanation));
const wb=FinancialWorkbook.buildFinancialWorkbook(report,{month:'May'});
for(const name of ['Income Statement','Balance Sheet']) assert(wb.getWorksheet(name).getCell('A5').value.includes(status.explanation));
}
assert(html.includes('drawPdfRecon(doc'));
clearReportOnly();assert.equal($('monthlyRecon').checked,false);
""")


def test_all_statement_sections_follow_details_in_screen_pdf_and_excel():
    run_js("""
const rows=[
{id:'assets',row_type:'heading',level:0,label:'Assets',values:{current_balance:30}},
{id:'cash',parent_id:'assets',row_type:'section',level:1,label:'Cash',values:{current_balance:30,current_period:30}},
{id:'a',parent_id:'cash',row_type:'account',level:2,label:'Account detail',values:{current_balance:30,current_period:30}},
{id:'total_assets',row_type:'subtotal',level:0,label:'Total Assets',values:{current_balance:30}},
{id:'total_liabilities_and_equity',row_type:'subtotal',level:0,label:'Total Liabilities & Equity',values:{current_balance:30}}];
const original=JSON.stringify(rows);
for(const type of ['income','balance']){
const screen=statementRows(rows,type);
assert(screen.indexOf('Account detail')<screen.indexOf('data-row-id="cash"'),'screen section total must follow detail');
const pdf=pdfRows(rows,type);assert(pdf.findIndex(r=>r.label.includes('Account detail'))<pdf.findIndex(r=>r.label.includes('Total Cash')),'PDF total after detail');
}
const wb=FinancialWorkbook.buildFinancialWorkbook({meta:{fiscal_year:2026,fiscal_period:5},income:rows,balance:rows}, {month:'May'});
for(const name of ['Income Statement','Balance Sheet']){
const labels=wb.getWorksheet(name).getColumn(1).values;
assert(labels.findIndex(x=>String(x).includes('Account detail'))<labels.findIndex(x=>String(x).includes('Total Cash')),'Excel total after detail');
}
assert.equal(JSON.stringify(rows),original,'must not mutate financial payload');
""")
