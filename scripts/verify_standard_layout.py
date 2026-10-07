"""Offline rebuild of a real immutable snapshot; never accesses GP or publishes.

Writes a private rebuilt snapshot, account-meaning audit, and exact invariant receipt.
The source snapshot, scope, period availability, and close metadata remain intact.
"""
from __future__ import annotations
import argparse
import csv
import datetime as dt
import hashlib
import json
from pathlib import Path
try:
    from scripts import financial_sync as sync
    from scripts.publish_financials import validate_document
except ImportError:
    import financial_sync as sync
    from publish_financials import validate_document


def verify(source: Path, output: Path) -> dict:
    original_bytes = source.read_bytes()
    document = json.loads(original_bytes)
    validate_document(document)
    original_codes = {p['company_code'] for p in document['periods']}
    assert original_codes == {c.code for c in sync.ACTIVE_COMPANIES}
    rebuilt = []
    branch_count = 0
    audit = {}
    for company in sync.ACTIVE_COMPANIES:
        periods = sorted([p for p in document['periods'] if p['company_code'] == company.code], key=sync.period_key)
        metadata = [dict(year=p['fiscal_year'], period=p['fiscal_period'], start=p['period_start'], end=p['period_end']) for p in periods]
        raw = {(p['fiscal_year'], p['fiscal_period']): [r for r in p['payload']['statement']['income_statement'] + p['payload']['statement']['balance_sheet'] if not r.get('synthetic')] for p in periods}
        new_periods = sync.build_company_payloads(company, metadata, raw)
        for old, new in zip(periods, new_periods):
            before, after = old['payload']['statement'], new['payload']['statement']
            assert before['income_statement'] == after['income_statement']
            assert before['balance_sheet'] == after['balance_sheet']
            assert before['comparison'] == after['comparison']
            for key, value in before['kpis'].items():
                if key != 'operating_expenses':
                    assert value == after['kpis'][key], (sync.period_key(old), key)
            for statement_type in ['income_statement', 'balance_sheet']:
                old_rows = {r['id']: r for r in before['presentation'][statement_type]}
                new_rows = {r['id']: r for r in after['presentation'][statement_type]}
                for identifier in (['gross_margin', 'net_income'] if statement_type == 'income_statement' else ['total_assets', 'total_liabilities', 'total_equity', 'total_liabilities_and_equity']):
                    assert old_rows[identifier]['values'] == new_rows[identifier]['values']
            assert after['bridge_controls']['passed']
            for row in after['income_statement']:
                audit[(company.code, row['account_number'], row['account_description'])] = sync._income_section(row, company.code)
            if company.code == 'SMI':
                branches = after['branches']
                assert set(sync.SMI_BRANCHES) <= set(branches)
                branch_count += len(branches)
                partition = [r['account_number'] for branch in branches.values() for r in branch['income_statement'] + branch['balance_sheet'] if not r.get('synthetic')]
                assert sorted(partition) == sorted(r['account_number'] for r in raw[(old['fiscal_year'], old['fiscal_period'])])
                for kind in ['income_statement', 'balance_sheet']:
                    summary = {r['id']: r for r in after['presentation'][kind] if r['row_type'] in {'section', 'subtotal'}}
                    for identifier, row in summary.items():
                        for key, value in row['values'].items():
                            values = [next((r['values'].get(key) for r in branch['presentation'][kind] if r['id'] == identifier), None) for branch in branches.values()]
                            if value is None:
                                assert all(v is None for v in values)
                            else:
                                assert sum((sync.D(v) for v in values if v is not None), sync.ZERO) == sync.D(value), (sync.period_key(old), identifier, key)
                for branch in branches.values():
                    assert branch['bridge_controls']['passed']
                    assert branch['comparison']['available'] == after['comparison']['available']
                    for trend in branch['trend']:
                        accounts = [r for r in raw[(trend['fiscal_year'], trend['fiscal_period'])] if sync._branch_code(r) == branch['branch_code']]
                        metrics = sync._statement_metrics(accounts, company.code)
                        for key in ['revenue', 'gross_profit', 'operating_expenses', 'net_income']:
                            assert trend[key] == sync._money(metrics[key])
            # Rebuild from frozen raw rows cannot reread GP close flags; preserve their verified evidence.
            new['payload']['monthly_recon'] = old['payload'].get('monthly_recon')
            new['payload_canonical'] = sync.canonical_json(new['payload'])
            new['payload_sha256'] = sync.canonical_hash(new['payload'])
            rebuilt.append(new)
    assert len(rebuilt) == document['run']['period_count']
    assert {sync.period_key(p) for p in rebuilt} == {sync.period_key(p) for p in document['periods']}
    run = {**document['run'], **sync._model_metadata(), 'source_sha256': sync.source_hash(rebuilt)}
    result = {'run': run, 'periods': rebuilt}
    validate_document(result)
    output.mkdir(parents=True, exist_ok=True)
    sync.write_atomic(result, output / 'rebuilt-snapshot.json')
    with (output / 'account-meaning-audit.csv').open('w', newline='', encoding='utf-8-sig') as file:
        writer = csv.writer(file)
        writer.writerow(['company', 'account_number', 'description', 'section'])
        writer.writerows([*key, value] for key, value in sorted(audit.items()))
    receipt = dict(verified_at=dt.datetime.now(dt.timezone.utc).isoformat(), source=str(source.resolve()),
        source_file_sha256=hashlib.sha256(original_bytes).hexdigest(), mapping_version=sync.MAPPING_VERSION,
        company_count=len(original_codes), period_count=len(rebuilt), branch_period_count=branch_count,
        account_meanings=len(audit), rebuilt_source_sha256=run['source_sha256'],
        raw_accounts_unchanged=True, accounting_totals_unchanged=True, close_metadata_preserved=True,
        period_coordinates_preserved=True, branch_partition_and_trends_verified=True,
        source_unchanged=source.read_bytes() == original_bytes,
        network_access=False, published=False)
    assert receipt['source_unchanged']
    sync.write_atomic(receipt, output / 'invariants.json')
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.source, args.output), indent=2))
