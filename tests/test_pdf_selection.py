"""PDF selection is independent of the displayed tab and Excel workbook."""
from test_statement_layout import run_js


def test_pdf_statement_selection_preserves_graphs_excel_and_first_page():
    run_js(r"""
assert(/<label[^>]*for="pdfStatementSelect"/.test(html),'PDF select needs an accessible label');
assert(/<option value="both" selected>Both statements<\/option>/.test(html));
assert(html.includes('Income statement only') && html.includes('Balance sheet only'));
state.report={meta:{company_code:'TEST',company_name:'Test',fiscal_year:2026},comparison:{},income:[{id:'income',row_type:'subtotal',label:'Net Income',values:{current_period:10}}],balance:[{id:'balance',row_type:'subtotal',label:'Total Assets',values:{current_balance:10}}]};
const original=JSON.stringify(state.report);
let pages,titles,graphs;
window.jspdf={jsPDF:function(){pages=[[]];titles=[];return {
 addPage(){pages.push([])},autoTable(options){pages.at(-1).push(...options.body);options.didDrawPage()},
 text(value){if(['Income Statement','Balance Sheet'].includes(value))titles.push(value)},
 splitTextToSize(t){return [t]},setFont(){},setFontSize(){},setTextColor(){},setDrawColor(){},setLineWidth(){},line(){},rect(){},
 getNumberOfPages(){return pages.length},setPage(){},save(){},getTextWidth(){return 100}
}}};
appendPdfGraphs=()=>{graphs++};
for(const scope of ['both','income','balance','invalid'])for(const includeGraphs of [false,true]){
 $('pdfStatementSelect').value=scope;$('includeGraphs').checked=includeGraphs;graphs=0;
 downloadPdfFile();
 assert.deepEqual(titles,scope==='income'?['Income Statement']:scope==='balance'?['Balance Sheet']:['Income Statement','Balance Sheet']);
 assert(pages.every(p=>p.length),'No blank first page for balance only');
 assert.equal(graphs,Number(includeGraphs));
 const wb=FinancialWorkbook.buildFinancialWorkbook(state.report,{month:'October'});
 assert(wb.getWorksheet('Income Statement') && wb.getWorksheet('Balance Sheet'),'Excel remains both');
 assert.equal(JSON.stringify(state.report),original);
}
""")
