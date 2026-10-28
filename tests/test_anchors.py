from openathlete.db.athlete_db import connect, find_anchor, init_db, write_anchor


def mem_db():
    conn = connect(":memory:")
    init_db(conn)
    return conn


def fields(date, **values):
    result = {"valid_from": date}
    result.update(values)
    return result


def test_anchor_sparse_updates_keep_complete_snapshot_and_empty_is_noop():
    conn = mem_db()
    write_anchor(conn, "run", fields(
        "2026-01-01", lt1_hr=140, lt2_hr=170, max_hr=190,
        resting_hr=50, threshold_pace_s=220), "manual")
    write_anchor(conn, "run", fields("2026-02-01", lt2_hr=172), "coros")
    feb = find_anchor(conn, "run", "2026-02-02")
    assert feb["lt1_hr"] == 140 and feb["lt2_hr"] == 172
    assert feb["max_hr"] == 190 and feb["resting_hr"] == 50
    assert feb["threshold_pace_s"] == 220
    jan = conn.execute("select * from threshold_anchor where valid_from='2026-01-01'").fetchone()
    assert jan["valid_to"] == "2026-01-31"
    before = conn.execute("select * from threshold_anchor").fetchall()
    assert write_anchor(conn, "run", fields("2026-03-01"), "coros") == []
    after = conn.execute("select * from threshold_anchor").fetchall()
    assert [(r["sport"], r["valid_from"], r["valid_to"]) for r in after] == \
        [(r["sport"], r["valid_from"], r["valid_to"]) for r in before]


def test_anchor_same_day_and_backdated_insert_maintain_versions():
    conn = mem_db()
    write_anchor(conn, "run", fields("2026-01-01", lt1_hr=140, max_hr=190), "manual")
    write_anchor(conn, "run", fields("2026-02-01", lt2_hr=172), "manual")
    write_anchor(conn, "run", fields("2026-02-01", lt1_hr=141), "manual")
    assert conn.execute("select count(*) c from threshold_anchor").fetchone()["c"] == 2
    feb = find_anchor(conn, "run", "2026-02-02")
    assert feb["lt1_hr"] == 141 and feb["lt2_hr"] == 172 and feb["max_hr"] == 190
    write_anchor(conn, "run", fields("2026-01-15", max_hr=195), "manual")
    rows = conn.execute(
        "select valid_from, valid_to from threshold_anchor order by valid_from").fetchall()
    assert [(r["valid_from"], r["valid_to"]) for r in rows] == [
        ("2026-01-01", "2026-01-14"),
        ("2026-01-15", "2026-01-31"),
        ("2026-02-01", None),
    ]
    mid = find_anchor(conn, "run", "2026-01-20")
    assert mid["lt1_hr"] == 140 and mid["max_hr"] == 195


def test_resting_hr_uses_latest_global_value_across_sports():
    conn = mem_db()
    write_anchor(conn, "run", fields(
        "2026-01-01", lt1_hr=140, lt2_hr=170, max_hr=190, resting_hr=50), "manual")
    write_anchor(conn, "ride", fields("2026-03-01", resting_hr=48), "manual")
    anchor = find_anchor(conn, "run", "2026-03-15")
    assert anchor["max_hr"] == 190 and anchor["resting_hr"] == 48
