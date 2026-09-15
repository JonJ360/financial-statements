(function (root, factory) {
  'use strict';
  if (typeof module === 'object' && module.exports) {
    module.exports = factory(require('../vendor/exceljs-4.4.0-force-full-calc.min.js'));
  } else {
    root.FinancialWorkbook = factory(root.ExcelJS);
  }
}(typeof globalThis !== 'undefined' ? globalThis : this, function (ExcelJS) {
  'use strict';

  const CURRENCY_FORMAT = '"$"#,##0;("$"#,##0);-';
  const COLORS = {
    navy: '233B61', ink: '172235', muted: '5A6678', pale: 'E8EEF5', white: 'FFFFFF', line: '65758B'
  };

  function safeText(value) {
    const text = String(value == null ? '' : value);
    return /^[=+\-@]/.test(text) ? `'${text}` : text;
  }

  function statementSpec(report, type, month) {
    const year = Number(report.meta.fiscal_year);
    const prior = Number(report.comparison && report.comparison.fiscal_year) || year - 1;
    if (type === 'income') {
      return {
        title: 'Income Statement', rows: report.income,
        subtitle: `For the period ended ${month} ${year}`,
        keys: ['current_period', 'current_ytd', 'prior_period', 'prior_ytd'],
        headers: ['Account', `${month} ${year}`, `YTD ${year}`, `${month} ${prior}`, `YTD ${prior}`]
      };
    }
    return {
      title: 'Balance Sheet', rows: report.balance,
      subtitle: `As of ${month} ${year}`,
      keys: ['current_balance', 'prior_balance'],
      headers: ['Account', `${month} ${year}`, `${month} ${prior}`]
    };
  }

  function styleStatement(workbook, report, type, month) {
    const spec = statementSpec(report, type, month);
    const sheet = workbook.addWorksheet(spec.title, {
      properties: { defaultRowHeight: 18 },
      pageSetup: {
        paperSize: 1, orientation: 'landscape', fitToPage: true,
        fitToWidth: 1, fitToHeight: 0,
        margins: { left: 0.35, right: 0.35, top: 0.55, bottom: 0.55, header: 0.2, footer: 0.2 }
      },
      views: [{ state: 'frozen', xSplit: 1, ySplit: 6, topLeftCell: 'B7', activeCell: 'B7' }]
    });
    sheet.headerFooter.oddFooter = '&LConfidential&C&A&RPage &P of &N';
    sheet.columns = [{ width: 48 }, ...spec.keys.map(() => ({ width: 18 }))];
    sheet.addRow([safeText(report.meta.company_name)]);
    sheet.addRow([spec.title]);
    sheet.addRow([spec.subtitle]);
    sheet.addRow(['Accrual Basis · Unaudited']);
    sheet.addRow([]);
    sheet.addRow(spec.headers);
    sheet.mergeCells(1, 1, 1, spec.headers.length);
    sheet.mergeCells(2, 1, 2, spec.headers.length);
    sheet.mergeCells(3, 1, 3, spec.headers.length);
    sheet.mergeCells(4, 1, 4, spec.headers.length);

    sheet.getRow(1).height = 23;
    sheet.getRow(2).height = 20;
    sheet.getRow(5).height = 8;
    sheet.getCell('A1').font = { name: 'Arial', size: 16, bold: true, color: { argb: COLORS.ink } };
    sheet.getCell('A2').font = { name: 'Arial', size: 13, bold: true, color: { argb: COLORS.navy } };
    for (const address of ['A3', 'A4']) {
      sheet.getCell(address).font = { name: 'Arial', size: 9, color: { argb: COLORS.muted } };
    }
    const header = sheet.getRow(6);
    header.height = 24;
    header.eachCell(cell => {
      cell.font = { name: 'Arial', size: 9, bold: true, color: { argb: COLORS.white } };
      cell.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: COLORS.navy } };
      cell.alignment = { vertical: 'middle', horizontal: cell.col === 1 ? 'left' : 'right' };
    });

    const rowNumbers = {};
    for (const source of spec.rows) {
      const level = Math.max(0, Math.min(2, Number(source.level) || 0));
      const name = `${'  '.repeat(level)}${source.label || ''}`;
      const values = spec.keys.map(key => {
        const value = source.values && source.values[key];
        return value == null ? null : Number(value);
      });
      const row = sheet.addRow([safeText(name), ...values]);
      rowNumbers[source.id] = row.number;
      row.height = source.row_type === 'heading' ? 22 : 18;
      row.eachCell({ includeEmpty: true }, (cell, col) => {
        cell.font = { name: 'Arial', size: 9, bold: source.row_type !== 'account', color: { argb: source.row_type === 'heading' ? COLORS.white : COLORS.ink } };
        cell.alignment = { vertical: 'middle', horizontal: col === 1 ? 'left' : 'right' };
        if (col > 1) cell.numFmt = CURRENCY_FORMAT;
        if (source.row_type === 'heading') cell.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: COLORS.navy } };
        if (source.row_type === 'section') cell.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: COLORS.pale } };
        if (source.row_type === 'subtotal') {
          cell.border = { top: { style: 'thin', color: { argb: COLORS.line } }, bottom: { style: 'double', color: { argb: COLORS.line } } };
        }
      });
    }
    sheet.autoFilter = { from: { row: 6, column: 1 }, to: { row: sheet.rowCount, column: spec.headers.length } };
    sheet.pageSetup.printTitlesRow = '1:6';
    sheet.pageSetup.fitToHeight = Math.max(1, Math.ceil((sheet.rowCount - 6) / 30));
    sheet.pageSetup.printArea = `A1:${sheet.getColumn(spec.headers.length).letter}${sheet.rowCount}`;
    return { sheet, rowNumbers };
  }

  function buildFinancialWorkbook(report, labels) {
    if (!ExcelJS || !ExcelJS.Workbook) throw new Error('ExcelJS is unavailable.');
    const month = labels && labels.month ? safeText(labels.month) : `Period ${report.meta.fiscal_period}`;
    const workbook = new ExcelJS.Workbook();
    workbook.creator = '360 Solutions LLC';
    workbook.company = safeText(report.meta.company_name);
    workbook.title = `${safeText(report.meta.company_name)} Financial Statements`;
    workbook.subject = 'Comparative lender financial statements';
    workbook.created = new Date(0);
    workbook.modified = new Date(0);
    workbook.calcProperties.fullCalcOnLoad = true;
    workbook.calcProperties.forceFullCalc = true;
    styleStatement(workbook, report, 'income', month);
    const balance = styleStatement(workbook, report, 'balance', month);
    const controls = workbook.addWorksheet('Controls', { state: 'hidden' });
    controls.state = 'hidden';
    controls.addRow(['Control', 'Variance']);
    const assets = balance.rowNumbers.total_assets;
    const liabilitiesEquity = balance.rowNumbers.total_liabilities_and_equity;
    controls.addRow(['Current Assets = Liabilities & Equity', { formula: `'Balance Sheet'!B${assets}-'Balance Sheet'!B${liabilitiesEquity}` }]);
    controls.addRow(['Prior Assets = Liabilities & Equity', { formula: `'Balance Sheet'!C${assets}-'Balance Sheet'!C${liabilitiesEquity}` }]);
    controls.getColumn(2).numFmt = CURRENCY_FORMAT;
    return workbook;
  }

  return { buildFinancialWorkbook, CURRENCY_FORMAT };
}));
