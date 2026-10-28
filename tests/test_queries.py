from datetime import datetime, timezone

from openathlete.config import set_config_values
from openathlete.db.athlete_db import connect, init_db, upsert_daily_metrics
from openathlete.queries import (
    get_overview, get_recovery_status, get_training_load, query_activities,
)


def make_home(tmp_path):
    set_config_values(tmp_path, timezone="UTC", primary_platform="garmin",
                      activity_since="2026-01-01")
    conn = connect(tmp_path / "athlete.db")
    init_db(conn)
    return conn


def add_activity(conn, activity_id, start, offset=0, sport="run", canonical=True,
                 trimp=50.0):
    conn.execute(
        "insert into activity (id,start_ts,tz_offset_s,sport,duration_s,distance_m,"
        "avg_hr,max_hr,trimp,structure_json,zone_boundaries_json) "
        "values (?,?,?,?,?,?,?,?,?,?,?)",
        (activity_id, int(start.timestamp()), offset, sport, 3000.0, 10000.0,
         150.0, 170.0, trimp, '{"type":"continuous","blocks":[]}',
         '[140,150,170,180,190,200]'))
    conn.execute(
        "insert into activity_source (activity_id,platform,platform_activity_id,"
        "fit_path,fit_sha256,is_canonical) values (?,?,?,?,?,?)",
        (activity_id, "garmin", "g{}".format(activity_id), "hidden.fit",
         "a" * 64, 1 if canonical else 0))
    conn.commit()


def test_activity_query_uses_utc_and_activity_local_date(tmp_path):
    conn = make_home(tmp_path)
    start = datetime(2026, 10, 1, 23, 30, tzinfo=timezone.utc)
    add_activity(conn, 1, start, offset=3600)
    add_activity(conn, 2, start, offset=3600, canonical=False)
    conn.close()

    data, coverage = query_activities(tmp_path, "2026-10-02", "2026-10-02", detail=True)

    assert data["returned"] == 1
    item = data["activities"][0]
    assert item["start_ts"] == int(start.timestamp())
    assert item["pace_s_per_km"] == 300.0
    assert item["structure"]["type"] == "continuous"
    assert item["canonical_source"]["fit_sha256"] == "a" * 64
    assert "fit_path" not in item["canonical_source"]
    assert coverage["excluded_conflict_count"] == 1


def test_training_load_returns_real_rows_and_pmc_requirement(tmp_path):
    conn = make_home(tmp_path)
    conn.execute("insert into daily_load values ('2026-10-01',40,10,12,-2)")
    conn.execute("insert into daily_load values ('2026-10-02',50,11,13,-2)")
    conn.commit()
    conn.close()
    data, coverage = get_training_load(tmp_path, "2026-10-01", "2026-10-02")
    assert len(data["daily"]) == 2
    assert data["latest"]["ctl"] == 11
    assert "CTL 11.0" in data["headline"]
    assert coverage["pmc_required_start"] == "2026-08-20"
    assert coverage["pmc_initialized"] is False


def test_recovery_keeps_sources_and_does_not_use_old_value_as_today(tmp_path):
    conn = make_home(tmp_path)
    upsert_daily_metrics(conn, [
        {"date": "2026-10-01", "source": "coros", "resting_hr": 45, "hrv_ms": 70},
        {"date": "2026-10-01", "source": "garmin", "resting_hr": 44, "hrv_ms": 72,
         "calories": 2000},
    ])
    conn.close()
    data, _ = get_recovery_status(tmp_path, "2026-10-01")
    assert data["source"] == "garmin"
    assert data["recovery_pct"] is None
    assert len(data["sources"]) == 2
    garmin = next(item for item in data["sources"] if item["source"] == "garmin")
    assert garmin["resting_hr_bpm"] == 44
    assert garmin["calories_kcal"] == 2000
    assert "resting_hr" not in garmin and "calories" not in garmin

    missing, coverage = get_recovery_status(tmp_path, "2026-10-02")
    assert missing["source"] is None
    assert missing["resting_hr_bpm"] is None
    assert coverage["latest_metric_date"] == "2026-10-01"


def test_overview_distinguishes_uncovered_from_real_zero(tmp_path):
    conn = make_home(tmp_path)
    conn.close()
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "races.md").write_text(
        "# 赛事\n\n## 目标赛事\n\n- 日期：2026-10-10；赛事：测试赛\n\n## 历史赛事\n",
        encoding="utf-8")

    uncovered, _ = get_overview(tmp_path, "2026-10-01", "2026-10-03",
                                {"complete": False})
    complete, _ = get_overview(tmp_path, "2026-10-01", "2026-10-03",
                               {"complete": True})

    assert uncovered["training"]["trimp"] is None
    assert uncovered["daily"][0]["trimp"] is None
    assert complete["training"]["trimp"] == 0
    assert complete["daily"][0]["trimp"] == 0
    assert complete["next_race"]["date"] == "2026-10-10"


def test_overview_marks_partial_trimp_day_and_total_unknown(tmp_path):
    conn = make_home(tmp_path)
    start = datetime(2026, 10, 2, tzinfo=timezone.utc)
    add_activity(conn, 1, start, trimp=50)
    add_activity(conn, 2, start, trimp=None)
    conn.close()
    data, coverage = get_overview(tmp_path, "2026-10-01", "2026-10-03",
                                  {"complete": True})
    assert data["training"]["trimp"] is None
    assert data["training"]["unknown_trimp_count"] == 1
    day = next(item for item in data["daily"] if item["date"] == "2026-10-02")
    assert day["trimp"] is None
    assert day["unknown_trimp_count"] == 1
    assert "trimp" in coverage["missing"]


def test_activity_coverage_marks_estimated_timezone(tmp_path):
    conn = make_home(tmp_path)
    add_activity(conn, 1, datetime(2026, 10, 2, tzinfo=timezone.utc), offset=None)
    conn.close()
    _, coverage = query_activities(tmp_path, "2026-10-01", "2026-10-03")
    assert coverage["timezone_estimated"] is True
