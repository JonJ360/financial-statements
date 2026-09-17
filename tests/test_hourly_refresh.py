import json
from pathlib import Path

import pytest

from scripts import hourly_refresh


def configure_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr(hourly_refresh, "ROOT", tmp_path)
    monkeypatch.setattr(hourly_refresh, "SNAPSHOT", tmp_path / "data" / "financial-statements.json")
    monkeypatch.setattr(hourly_refresh, "CREDENTIALS", tmp_path / "credentials.json")
    monkeypatch.setattr(hourly_refresh, "STATUS_LOG", tmp_path / "logs" / "financial-statements-refresh.json")
    monkeypatch.setattr(hourly_refresh, "LOCK_FILE", tmp_path / "financial-statements-refresh.lock")


def test_refresh_extracts_then_publishes_and_writes_compact_verified_status(monkeypatch, tmp_path):
    configure_tmp(monkeypatch, tmp_path)
    calls = []

    def fake_run(args):
        calls.append(args)
        if any(str(arg).endswith("financial_sync.py") for arg in args):
            return json.dumps({
                "output": "ignored.json", "as_of": "2026-09-17", "company_count": 25,
                "period_count": 1904, "source_sha256": "a" * 64,
                "manifest": [{"company_code": "SMI"}] * 1904,
            })
        return json.dumps({
            "run_id": "run-1", "source_sha256": "a" * 64,
            "period_count": 1904, "verified": True,
        })

    hourly_refresh.refresh(run_command=fake_run, current_hash_reader=lambda: "b" * 64)

    assert len(calls) == 2
    assert calls[0][1:3] == ["scripts/financial_sync.py", "--output"]
    assert calls[1][1:3] == ["scripts/publish_financials.py", "--snapshot"]
    status = json.loads(hourly_refresh.STATUS_LOG.read_text(encoding="utf-8"))
    assert status["ok"] is True
    assert status["extract"]["company_count"] == 25
    assert status["publish"]["verified"] is True
    assert "manifest" not in status["extract"]
    assert hourly_refresh.LOCK_FILE.exists() is False


def test_unchanged_source_is_verified_without_republishing(monkeypatch, tmp_path):
    configure_tmp(monkeypatch, tmp_path)
    calls = []

    def fake_run(args):
        calls.append(args)
        return json.dumps({
            "as_of": "2026-09-17", "company_count": 25,
            "period_count": 1904, "source_sha256": "a" * 64,
        })

    hourly_refresh.refresh(run_command=fake_run, current_hash_reader=lambda: "a" * 64)

    assert len(calls) == 1
    status = json.loads(hourly_refresh.STATUS_LOG.read_text(encoding="utf-8"))
    assert status["ok"] is True
    assert status["publish"]["verified"] is True
    assert status["publish"]["unchanged"] is True


def test_refresh_failure_is_logged_and_reraised(monkeypatch, tmp_path):
    configure_tmp(monkeypatch, tmp_path)

    def failing_run(args):
        raise RuntimeError("GP unavailable")

    with pytest.raises(RuntimeError, match="GP unavailable"):
        hourly_refresh.refresh(run_command=failing_run)

    status = json.loads(hourly_refresh.STATUS_LOG.read_text(encoding="utf-8"))
    assert status["ok"] is False
    assert status["error"] == "GP unavailable"
    assert hourly_refresh.LOCK_FILE.exists() is False


def test_overlapping_refresh_exits_without_running_commands(monkeypatch, tmp_path):
    configure_tmp(monkeypatch, tmp_path)
    hourly_refresh.LOCK_FILE.write_text("already running", encoding="utf-8")
    calls = []

    result = hourly_refresh.refresh(run_command=lambda args: calls.append(args))

    assert result is False
    assert calls == []
