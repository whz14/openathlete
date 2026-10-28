from openathlete import cli


def test_coros_pull_uses_persistent_backfill(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "OPENATHLETE_HOME", tmp_path)
    monkeypatch.setattr(cli, "_coros_client", lambda: object())
    calls = []

    def backfill(home, since, until, dry_run, limit, client):
        calls.append((home, since, until, dry_run, limit, client))
        return {
            "discovered": 2, "processed": 2, "succeeded": 2, "failed": 0,
            "queued_remaining": 0, "coverage_extended": True,
            "activities": [], "next_action": "",
        }

    monkeypatch.setattr("openathlete.sync_state.backfill_activities", backfill)
    args = type("Args", (), {"since": "20261001", "until": "2026-10-03",
                             "dry_run": False, "limit": None})()
    assert cli._coros_pull(args) == 0
    assert calls[0][1:5] == ("2026-10-01", "2026-10-03", False, None)


def test_coros_pull_failure_exit_code(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "OPENATHLETE_HOME", tmp_path)
    monkeypatch.setattr(cli, "_coros_client", lambda: object())
    monkeypatch.setattr("openathlete.sync_state.backfill_activities", lambda *args, **kwargs: {
        "discovered": 1, "processed": 1, "succeeded": 0, "failed": 1,
        "queued_remaining": 1, "coverage_extended": False,
        "activities": [], "next_action": "继续",
    })
    args = type("Args", (), {"since": None, "until": None,
                             "dry_run": False, "limit": 1})()
    assert cli._coros_pull(args) == 1


def test_coros_metrics_supports_dry_run(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "OPENATHLETE_HOME", tmp_path)
    monkeypatch.setattr(cli, "_coros_client", lambda: object())
    calls = []

    def backfill(home, since, until, dry_run, limit, client):
        calls.append((since, until, dry_run, limit))
        return {"discovered": 2, "processed": 0, "coverage_extended": False,
                "next_action": "确认后执行"}

    monkeypatch.setattr("openathlete.sync_state.backfill_metrics", backfill)
    args = type("Args", (), {"since": "20261001", "until": "20261002", "days": 28,
                             "dry_run": True, "limit": 1})()
    assert cli._coros_metrics(args) == 0
    assert calls == [("2026-10-01", "2026-10-02", True, 1)]


def test_coros_default_dates_use_configured_timezone(monkeypatch, tmp_path):
    from datetime import datetime as real_datetime, timezone

    from openathlete.config import set_config_values

    set_config_values(tmp_path, timezone="Asia/Shanghai")
    monkeypatch.setattr(cli, "OPENATHLETE_HOME", tmp_path)
    monkeypatch.setattr(cli, "_coros_client", lambda: object())

    class FakeDateTime:
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                instant = real_datetime(2026, 10, 1, 16, 30, tzinfo=timezone.utc)
                return instant.astimezone(tz)
            return real_datetime(2026, 10, 1, 23, 30)

    monkeypatch.setattr(cli, "datetime", FakeDateTime)
    activity_calls = []

    def backfill_activities(home, since, until, dry_run, limit, client):
        activity_calls.append((since, until))
        return {"discovered": 0, "processed": 0, "succeeded": 0, "failed": 0,
                "queued_remaining": 0, "coverage_extended": False,
                "activities": [], "next_action": ""}

    monkeypatch.setattr("openathlete.sync_state.backfill_activities", backfill_activities)
    args = type("Args", (), {"since": None, "until": None,
                             "dry_run": False, "limit": None})()
    assert cli._coros_pull(args) == 0
    assert activity_calls == [("2026-09-25", "2026-10-02")]

    metric_calls = []

    def backfill_metrics(home, since, until, dry_run, limit, client):
        metric_calls.append((since, until))
        return {"discovered": 0, "processed": 0, "coverage_extended": False,
                "next_action": ""}

    monkeypatch.setattr("openathlete.sync_state.backfill_metrics", backfill_metrics)
    args = type("Args", (), {"since": None, "until": None, "days": 28,
                             "dry_run": False, "limit": None})()
    assert cli._coros_metrics(args) == 0
    assert metric_calls == [("2026-09-04", "2026-10-02")]


def test_no_args_and_serve_use_same_entry(monkeypatch):
    calls = []
    monkeypatch.setattr("openathlete.mcp.server.main", lambda: calls.append("serve"))
    assert cli.main([]) == 0
    assert cli.main(["serve"]) == 0
    assert calls == ["serve", "serve"]
    assert cli.main(["serve", "extra"]) == 2
