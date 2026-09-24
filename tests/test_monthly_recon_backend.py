"""GP fiscal close metadata; never a substantive audit or actor attribution."""
import copy
import datetime as dt
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import financial_sync as fs


def closed_row():
    return {**{f'PSERIES_{i}': 1 for i in range(1, 7)},
            'origin_row_count': 12, 'origin_invalid_count': 0,
            'origin_mismatch_count': 0, 'CLOSED': 0}


class MonthlyReconTests(unittest.TestCase):
    def test_all_module_flags_closed_with_consistent_origins_is_completed(self):
        self.assertTrue(hasattr(fs, 'monthly_recon_status'), 'missing close classifier')
        result = fs.monthly_recon_status(closed_row())
        self.assertEqual('completed', result['status'])
        self.assertIs(True, result['completed'])
        self.assertEqual('GP fiscal-period module closure', result['basis'])
        self.assertEqual({f'PSERIES_{i}': 1 for i in range(1, 7)}, result['module_flags'])
        self.assertEqual('consistent', result['origin_check']['status'])
        self.assertEqual(12, result['origin_check']['row_count'])
        self.assertIn('not an audit', result['interpretation'])

    def test_incomplete_or_conflicting_evidence_never_certifies_completion(self):
        cases = [
            ({**closed_row(), **{f'PSERIES_{i}': 0 for i in range(1, 7)}}, 'open'),
            ({**closed_row(), 'PSERIES_3': 0}, 'mixed'),
            ({**closed_row(), 'origin_mismatch_count': 1}, 'mixed'),
            ({**closed_row(), 'PSERIES_1': None}, 'unknown'),
            ({**closed_row(), 'PSERIES_2': 2}, 'unknown'),
            ({**closed_row(), 'PSERIES_2': '1'}, 'unknown'),
            ({**closed_row(), 'origin_invalid_count': 1}, 'unknown'),
            ({**closed_row(), 'origin_row_count': 0}, 'unknown'),
            ({**closed_row(), 'origin_mismatch_count': None}, 'unknown'),
            ({}, 'unknown'),
        ]
        for row, expected in cases:
            with self.subTest(row=row):
                before = copy.deepcopy(row)
                result = fs.monthly_recon_status(row)
                self.assertEqual(expected, result['status'])
                self.assertIs(False, result['completed'])
                self.assertEqual(before, row)

    def test_calendar_query_reads_real_flags_and_cross_checks_origin_rows(self):
        sql = fs.CALENDAR_SQL
        for index in range(1, 7):
            self.assertIn(f'p.PSERIES_{index}', sql)
            self.assertIn(f'WHEN {index + 1} THEN p.PSERIES_{index}', sql)
        for token in ('o.SERIES<>0', 'o.YEAR1=p.YEAR1', 'o.PERIODID=p.PERIODID',
                      'o.CLOSED', 'origin_row_count', 'origin_invalid_count', 'origin_mismatch_count'):
            self.assertIn(token, sql)
        self.assertNotIn('p.CLOSED', sql)

    def test_extraction_carries_status_and_hashes_it_without_changing_financials(self):
        # Balanced zero-value source avoids coupling to the other test module.
        account = {'account_index': 1, 'account_number': '100', 'account_description': 'Cash',
                   'posting_type': 0, 'typical_balance': 0, 'category': 1,
                   'category_description': 'Cash', 'fiscal_year': 2026, 'fiscal_period': 8}
        calendar = {**closed_row(), 'fiscal_year': 2026, 'fiscal_period': 8,
                    'period_start': dt.date(2026, 8, 1), 'period_end': dt.date(2026, 8, 31)}
        connection = mock.Mock()
        def extract(row):
            with mock.patch.object(fs, 'verify_read_only'), mock.patch.object(fs, '_fetch', side_effect=[[row], [account]]):
                return fs.extract_company(connection, fs.ACTIVE_COMPANIES[0], dt.date(2026, 9, 24))[0]
        old = extract(calendar)
        frozen = copy.deepcopy(old)
        self.assertEqual('completed', old['payload']['monthly_recon']['status'])
        updated = extract({**calendar, 'PSERIES_1': 0})
        self.assertEqual('mixed', updated['payload']['monthly_recon']['status'])
        self.assertNotEqual(old['payload_sha256'], updated['payload_sha256'])
        self.assertNotEqual(fs.source_hash([old]), fs.source_hash([updated]))
        self.assertEqual(old['payload']['statement'], updated['payload']['statement'])
        self.assertEqual(frozen, old)
        self.assertEqual(fs.canonical_json(updated['payload']), updated['payload_canonical'])
        self.assertEqual(fs.canonical_hash(updated['payload']), updated['payload_sha256'])
        self.assertEqual(old, extract(calendar))

    def test_payload_without_close_evidence_defaults_unknown(self):
        account = fs.map_account_row({'account_index': 1, 'account_number': '100',
                    'posting_type': 0, 'typical_balance': 0, 'category': 1, 'category_description': 'Cash'})
        period = {'year': 2026, 'period': 8, 'start': '2026-08-01', 'end': '2026-08-31'}
        result = fs.build_company_payloads(fs.ACTIVE_COMPANIES[0], [period], {(2026, 8): [account]})
        self.assertEqual('unknown', result[0]['payload']['monthly_recon']['status'])
        self.assertIs(False, result[0]['payload']['monthly_recon']['completed'])


if __name__ == '__main__':
    unittest.main()
