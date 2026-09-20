import pytest
from scripts import publish_financials as publisher


def test_timeout_splits_batch_without_dropping_or_reordering_periods():
    accepted = []
    periods = [{"coordinate": i} for i in range(5)]
    def rpc(*args):
        payload = args[-1]
        assert payload["p_run_id"] == "existing-run"
        if len(payload["p_periods"]) > 1:
            raise RuntimeError('financial_stage_period_batch failed: HTTP 500 {"code":"57014","message":"canceling statement due to statement timeout"}')
        accepted.extend(payload["p_periods"])
    publisher._stage_period_batch(rpc, "base", "key", "token", "existing-run", periods)
    assert accepted == periods


def test_single_period_timeout_and_non_timeout_errors_fail_closed():
    for message, periods in [('HTTP 500 {"code":"57014"}', [{}]), ("integrity validation failed", [{}, {}])]:
        calls = []
        def rpc(*args):
            calls.append(args)
            raise RuntimeError(message)
        with pytest.raises(RuntimeError, match=message.split()[0]):
            publisher._stage_period_batch(rpc, "base", "key", "token", "run", periods)
        assert len(calls) == 1
