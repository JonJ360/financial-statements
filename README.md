# GP Financial Statements

Read-only Dynamics GP extraction for 23 active companies and an atomic Supabase publishing contract. The extractor creates one JSON payload for each company/fiscal period; generated payloads are local artifacts and are not committed.

## Scope

`BBMTS DMERC FFCAP FTSOL LPPAR MILT OLH OPAT OZAK1 OZDEV RDSC REB RRCOF RSS SEAMX SFAB SOL SPFAR UNIFO UUFGO VGA VPENG SMI`

The main companies use the reviewed GP endpoint; SMI uses its separate reviewed endpoint. `SOL1` is intentionally excluded. `TROP` (Tropic Paws) and `TPC` (The Perry Center) are excluded from extraction and the current serving snapshot; GP records and historical immutable snapshots are retained. Labels come from `gp360_company_mapping_2026.csv`, with `REB` displayed as **360 Sales LLC** and `SEAMX` as **Sea Max LLC**.

## Extraction contract

- SQL is `SELECT`-only and the login is rejected unless it has the reviewed read-only permission profile.
- Fiscal periods come from each database's `SY40100` period rows joined to its `SY40101` fiscal-year bounds. Every configured period from fiscal 2020 forward is emitted by default, including future months and open/unreconciled months; callers may request a smaller trailing count explicitly. Neither date nor close status limits selection. Period 0 is not emitted, but its opening debit/credit is included in balance-sheet ending balances. `--as-of` records the extraction date, not a period or transaction-date cutoff: reports show actual posted GP period balances at extraction time, not unposted batches or projections.
- Ledger 1 only. `GL10110` current summary rows take precedence over matching `GL10111` history rows.
- Every chart account is emitted for every selected period. `GL00100.PSTNGTYP` controls IS/BS classification, `GL00100.TPCLBLNC` controls normal account display signs, `GL00102` supplies the category, and `GL00105.ACTNUMST` supplies the formatted account number. Raw debit/credit and opening/YTD values remain in the payload for controls.
- GP category math is fixed: net sales = categories 31+32, COGS = raw debit less credit for 33, and operating expenses/other generally use raw debit less credit for categories 34–47. Category-37 descriptions containing `DRAW` or `DISTRIBUTION` are excluded from net income and reclassified as equity distributions. Net income is the inverse of the remaining raw P&L, and gross profit is net sales less COGS.
- Balance-sheet KPIs use raw category section signs (assets 1–10, liabilities 11–19, equity 20–30) and add a synthetic Current Earnings equity row. Every period fails closed unless summary equals detail, debits equal credits, raw BS YTD plus raw P&L YTD equals zero, Assets = Liabilities + Equity within $0.01, and every account/category is present.
- Each staged period is an envelope containing `payload`, its exact sorted/minified UTF-8 JSON as `payload_canonical`, and `payload_sha256`; the persisted row stores only `payload`. The run hash is SHA-256 of the lowercase period SHA strings concatenated in `(company_code, fiscal_year, fiscal_period)` order. The run also carries an explicit manifest of every expected period key.
- The browser calls `financial_statement_catalog()` for metadata only, then `financial_statement_period(p_company_code, p_fiscal_year, p_fiscal_period)` for exactly the selected payload. Both RPCs independently bind `auth.uid()` to Jon's reviewed UUID. The browser does not persist financial payloads or preload the run.

## V2.6 statement presentation and monthly reconciliation

All income-statement and balance-sheet category totals follow their account detail consistently in the browser, PDF and Excel. Financial calculations are unchanged.

Each selected fiscal month has a read-only **Monthly recon completed** checkbox. `payload.monthly_recon` uses GP `SY40100` SERIES=0 flags `PSERIES_1` through `PSERIES_6`, cross-checked against nonzero-series transaction-origin closure flags for that fiscal year/period. All six closed with consistent origin evidence means completed; open, mixed and unknown remain unchecked. This is an operational close indicator, not an audit certification or evidence of who completed the work. Missing legacy metadata is unknown. Closure changes participate in snapshot hashes and the existing hourly refresh; GP is never modified.

Full tests: `python -m pytest tests -q`. Opt-in local Edge visual/export checks: `FINANCIAL_EDGE_VERIFY=1 python -m pytest tests -q -s`.

## Run

```bash
python -m unittest tests.test_financial_sync -v
python scripts/financial_sync.py --company SFAB --company REB --as-of 2026-09-14 --output data/sample-sfab-reb.json
```

Omit `--company` to require all 23 companies to succeed in one extraction. If any company fails, no output is replaced.

## Hourly refresh

`scripts/hourly_refresh.py` extracts all 23 companies, validates every configured fiscal period (including future and open months), compares the resulting source hash with the verified production pointer, and publishes atomically only when the financial data changed. A local exclusive lock prevents overlapping runs. Success is silent and recorded in `C:\Users\jonj\AppData\Local\hermes\logs\financial-statements-refresh.json`; failures are re-raised so the scheduler can alert.

Hermes schedules this script every hour. The Financial Statements app therefore reads a production snapshot that is normally no more than one successful hourly run behind GP. The app still reads only the currently verified, atomically promoted snapshot; an extraction or validation failure leaves the prior statement set live.

## Supabase publisher

`supabase/migrations/202609140001_financial_statements.sql` has been reviewed and applied to the production project, together with its validation-COALESCE correction. It creates immutable run, period, and run-event rows; atomic current/previous pointers; retry-idempotent stage/batch/validate/promote operations; an explicit preconditioned rollback RPC; SQL/Python-equivalent hash and manifest validation; FORCE RLS; and hardened `SECURITY DEFINER` functions with empty search paths and fully qualified object references. Direct table access is revoked from API and operational roles, including `service_role`; only the minimum RPC execute grants remain. The two frontend read RPCs are Jon-only and bound to UUID `b605d98f-498e-4a94-94cf-e055ed2b5fcc`.

The publisher refuses credential JSON containing `service_role_key`. It reuses only `supabase_url`, `publishable_key`, `current_ar_ingestion_key`, `current_ar_promotion_key`, and `operator_verification_key`. Before its first RPC it validates the three JWT role claims, rejects duplicate input/manifest keys, recomputes every canonical payload hash and the ordered run hash, and verifies all declared counts. Tokens are never printed.

Publishing a reviewed snapshot is:

```bash
python scripts/publish_financials.py --snapshot data/financial-statements.json --credentials PATH/TO/local-credentials.json
```

The publisher stages a run, sends period batches, validates, promotes atomically, then verifies the current run ID, source hash, and period count. A retry with the same source hash is accepted only when all metadata and period conflicts match exactly. This repository does not apply migrations or publish as part of extraction or tests.

## Vendored ExcelJS patch

`vendor/exceljs-4.4.0-force-full-calc.min.js` is intentionally not the unmodified ExcelJS 4.4.0 distribution. It adds serialization of the workbook `forceFullCalc` calculation property so exported lender workbooks recalculate formulas when opened. The official 4.4.0 artifact SHA-256 is `7e49da68588e250dbb8bba190d2caa8ab3787cc0284bda1d8b2f805c4df742c9`; the reviewed patched artifact SHA-256 is `ab59774d3d6607eefc33eec3f8b184b0635b70942b656bd98014abdaa143eefe`. Removing the single `forceFullCalc` serializer expression restores the official artifact byte-for-byte.
