# -*- coding: utf-8 -*-
"""课型结构解析核心单测（全部合成数据，不依赖真实 FIT）。"""
from openathlete.db.athlete_db import parse_structure
from openathlete.structure import (
    Lap, classify_laps, summarize, summarize_ride, validate_structure_dict,
)


def run_lap(dist, pace_s, hr=160, trigger="distance"):
    return Lap(dist_m=dist, time_s=pace_s * dist / 1000, pace=float(pace_s),
               avg_hr=hr, trigger=trigger)


def ride_lap(dist, time_s, power=None, hr=None, trigger="distance"):
    return Lap(dist_m=float(dist), time_s=float(time_s),
               pace=(time_s / (dist / 1000)) if dist > 1 else None,
               avg_hr=hr, avg_power=power, trigger=trigger)


def hr_interval_laps():
    laps = []
    for i in range(4):
        laps.append(Lap(dist_m=400.0, time_s=48.0, avg_hr=160, trigger="manual"))
        if i < 3:
            laps.append(Lap(dist_m=80.0, time_s=12.0, avg_hr=135, trigger="manual"))
    return laps


def intervals_10x1200_laps():
    # 热身与首组之间要有恢复圈才会成为独立块（连续 fast 圈会合并）；
    # 热身配速须 <1.35×快组配速（否则它自己就被判成恢复圈）、>1.2×（才判热身）
    laps = [run_lap(2000, 270, hr=130), run_lap(140, 450, hr=145)]
    for i in range(10):
        laps.append(run_lap(1200, 208 + (i % 4) * 2, hr=170))
        laps.append(run_lap(140, 450, hr=150))
    return laps


def test_regular_intervals_10x1200():
    session = {"distance_m": 16900.0, "time_s": 4600.0}
    laps = intervals_10x1200_laps()
    laps.insert(5, Lap(dist_m=0.0, time_s=1.0))
    nl, st = summarize(session, laps)
    d = st.to_dict()
    assert d["type"] == "intervals"
    groups = [b for b in d["blocks"] if (b["kind"] if isinstance(b, dict) else b.kind) == "group"]
    assert len(groups) == 1
    g = groups[0]
    g = g if isinstance(g, dict) else g.to_dict()
    assert g["cycles"] == 10
    assert len(g["steps"]) == 1
    assert g["steps"][0]["dist_m"] == 1200
    assert g["recovery"]["standing"] is False
    assert len(d["reps"]) == 10
    assert "10×1.2km" in nl
    assert "热身" in nl
    assert validate_structure_dict(d) == []


def test_ladder_repeat_fold():
    laps = []
    for _ in range(2):
        for dist, pace in ((4000, 220), (3200, 215), (2400, 210)):
            laps.append(run_lap(dist, pace, hr=168))
            laps.append(run_lap(200, 500, hr=145))
    laps.pop()  # 末尾无恢复
    nl, st = summarize({"distance_m": 20200.0, "time_s": 6000.0}, laps)
    d = st.to_dict()
    assert d["type"] == "intervals"
    g = [b for b in st.blocks if hasattr(b, "steps")][0]
    assert g.cycles == 2 and len(g.steps) == 3
    assert "(4.0km@3:40+3.2km@3:35+2.4km@3:30)×2" in nl
    assert validate_structure_dict(d) == []


def test_no_repeat_pattern():
    laps = []
    for dist, pace in ((5000, 230), (4000, 225), (3000, 220), (820, 215)):
        laps.append(run_lap(dist, pace, hr=165))
        laps.append(run_lap(210, 500, hr=145))
    laps.pop()
    nl, st = summarize({"distance_m": 13650.0, "time_s": 4000.0}, laps)
    d = st.to_dict()
    assert d["type"] == "intervals"
    g = [b for b in st.blocks if hasattr(b, "steps")][0]
    assert g.cycles == 1 and len(g.steps) == 4
    assert nl.startswith("4组(5.0km+4.0km+3.0km+820m)")
    assert validate_structure_dict(d) == []


def test_continuous_run():
    laps = [run_lap(1000, 228 + (i % 3) * 2, hr=150) for i in range(10)]
    nl, st = summarize({"distance_m": 10000.0, "time_s": 2300.0}, laps)
    d = st.to_dict()
    assert d["type"] == "continuous"
    assert d["wave_hint"] is False
    assert nl.startswith("连续 10.0km @3:50")
    assert validate_structure_dict(d) == []


def test_premature_manual_lap_is_recovery():
    laps = [
        run_lap(1000, 210, hr=170), run_lap(140, 450, hr=150),
        run_lap(1000, 210, hr=170),
        run_lap(390, 245, hr=158, trigger="manual"),  # 提前按圈结束组、慢跑恢复
        run_lap(1000, 210, hr=170), run_lap(140, 450, hr=150),
    ]
    labels, _ = classify_laps(laps)
    assert labels[3] == "recovery"
    nl, st = summarize({"distance_m": 4670.0, "time_s": 1500.0}, laps)
    d = st.to_dict()
    assert d["type"] == "intervals"
    assert len(d["reps"]) == 3


def test_mixed_lap_degrades_to_continuous_with_wave():
    # 组距与恢复距离不对齐产生混合圈（140m 恢复 + 400m 圈 ≈ 4:27/km），
    # 够不到恢复判定阈值 → 退化为"圈速有波动的连续跑"（DESIGN.md §3 盲区说明）
    laps = []
    for _ in range(8):
        laps.append(run_lap(400, 210, hr=168))
        laps.append(run_lap(540, 267, hr=160))
    nl, st = summarize({"distance_m": 7520.0, "time_s": 3000.0}, laps)
    d = st.to_dict()
    assert d["type"] == "continuous"
    assert d["wave_hint"] is True


def test_standing_rest():
    laps = []
    for i in range(5):
        laps.append(run_lap(1000, 210, hr=172))
        if i < 4:
            laps.append(Lap(dist_m=0.0, time_s=30.0, avg_hr=150))  # 站休 30s
    nl, st = summarize({"distance_m": 5000.0, "time_s": 1200.0}, laps)
    d = st.to_dict()
    assert d["type"] == "intervals"
    g = [b for b in st.blocks if hasattr(b, "steps")][0]
    assert g.recovery["standing"] is True
    assert "（站休）" in nl
    assert validate_structure_dict(d) == []


# --- 骑行 ---

def test_ride_power_time_type_10x10min():
    dists = [4800, 5200, 4900, 5100, 5000, 4850, 5150, 4950, 5050, 5000]
    powers = [230, 245, 238, 240, 236, 242, 234, 244, 232, 241]
    laps = []
    for i in range(10):
        laps.append(ride_lap(dists[i], 600, power=powers[i], hr=150 + i % 3))
        if i < 9:
            laps.append(ride_lap(1000, 180, power=120, hr=130))
    nl, st = summarize_ride({"distance_m": 59000.0, "time_s": 7620.0}, laps)
    d = st.to_dict()
    assert d["type"] == "intervals"
    g = [b for b in st.blocks if hasattr(b, "steps")][0]
    assert g.cycles == 10 and len(g.steps) == 1
    assert "time_s" in g.steps[0] and "dist_m" not in g.steps[0]
    assert g.steps[0]["time_s"] == 600
    assert g.steps[0]["power_min_w"] == 230 and g.steps[0]["power_max_w"] == 245
    assert nl == "10×10:00 @230-245W(均238W)，间休 3:00"
    assert validate_structure_dict(d) == []


def test_ride_power_distance_type():
    laps = []
    for i, (dist, t, p) in enumerate(((4950, 590, 275), (5025, 615, 285),
                                      (4980, 600, 280), (5010, 620, 282),
                                      (4990, 585, 278))):
        laps.append(ride_lap(dist, t, power=p, hr=150))
        if i < 4:
            laps.append(ride_lap(1000, 200, power=150, hr=135))
    nl, st = summarize_ride({"distance_m": 28955.0, "time_s": 3810.0}, laps)
    d = st.to_dict()
    assert d["type"] == "intervals"
    g = [b for b in st.blocks if hasattr(b, "steps")][0]
    assert g.cycles == 5
    assert "dist_m" in g.steps[0]
    assert "5×5.0km @275-285W" in nl
    assert "间休 3:20（1.0km）" in nl
    assert validate_structure_dict(d) == []


def test_ride_hr_intervals_no_power():
    laps = []
    for i in range(4):
        laps.append(ride_lap(4000, 480, hr=160, trigger="manual"))
        if i < 3:
            laps.append(ride_lap(800, 120, hr=135, trigger="manual"))
    nl, st = summarize_ride({"distance_m": 18400.0, "time_s": 2280.0}, laps)
    d = st.to_dict()
    assert d["type"] == "intervals"
    g = [b for b in st.blocks if hasattr(b, "steps")][0]
    assert g.cycles == 4
    assert g.steps[0]["hr_min"] == 160 and g.steps[0]["hr_max"] == 160
    assert "bpm" in nl
    assert len(d["reps"]) == 4
    assert validate_structure_dict(d) == []


def test_ride_commute_is_continuous():
    # 通勤：自动 5km 圈心率波动 + 一个长时间停表圈 + 一个下坡高速低心率圈，
    # 三条防线（心率比/时长比/速度比）都不能误判恢复
    laps = [
        ride_lap(5000, 820, hr=150), ride_lap(5000, 810, hr=148),
        ride_lap(5000, 830, hr=152), ride_lap(5000, 900, hr=135),
        ride_lap(5000, 815, hr=149), ride_lap(5000, 825, hr=151),
        ride_lap(5000, 1500, hr=120),   # 买咖啡停表
        ride_lap(5000, 480, hr=100),    # 长下坡
    ]
    nl, st = summarize_ride({"distance_m": 40000.0, "time_s": 6980.0}, laps)
    d = st.to_dict()
    assert d["type"] == "continuous"


def test_ride_no_power_no_hr():
    laps = [ride_lap(5000, 800), ride_lap(5000, 810)]
    nl, st = summarize_ride({"distance_m": 10000.0, "time_s": 1610.0}, laps)
    d = st.to_dict()
    assert d["type"] == "continuous"
    assert "连续 10.0km（无间歇结构）" == nl


def test_short_no_power_ride_is_continuous_despite_hr_interval_shape():
    result = parse_structure(
        "ride", None, {"distance_m": 1840.0, "time_s": 228.0}, hr_interval_laps(), 15)
    assert result["type"] == "continuous"
    assert len(result["blocks"]) == 1 and result["blocks"][0]["kind"] == "steady"
    assert "reps" not in result and "commute" not in result and "training" not in result


def test_power_or_distance_threshold_enables_training_parse():
    with_power = parse_structure(
        "ride", None, {"distance_m": 1840.0, "time_s": 228.0, "avg_power": 220},
        hr_interval_laps(), 15)
    with_lap_power = parse_structure(
        "ride", None, {"distance_m": 1840.0, "time_s": 228.0},
        [ride_lap(400, 48, power=220)], 15)
    below_threshold = parse_structure(
        "ride", None, {"distance_m": 14999.0, "time_s": 228.0}, hr_interval_laps(), 15)
    at_threshold = parse_structure(
        "ride", None, {"distance_m": 15000.0, "time_s": 228.0}, hr_interval_laps(), 15)
    assert with_power["type"] == "intervals"
    assert with_lap_power["blocks"][0]["avg_power"] == 220
    assert below_threshold["type"] == "continuous"
    assert at_threshold["type"] == "intervals"


# --- 校验器 ---

def test_validator_rejects_inconsistent_structure():
    cases = [
        ({"type": "intervals",
          "blocks": [{"kind": "group", "cycles": 10,
                      "steps": [{"dist_m": 1200.0}], "recovery": None}],
          "reps": [{"dist_m": 1200.0}] * 5}, {"cycles"}),
        ({"type": "intervals",
          "blocks": [{"kind": "group", "cycles": 2,
                      "steps": [{"dist_m": 1000.0}],
                      "recovery": {"time_s_med": 60, "dist_m_med": 100,
                                   "standing": True, "count": 1}}],
          "reps": [{"dist_m": 1000.0}] * 2}, {"standing"}),
        ({"type": "intervals",
          "blocks": [{"kind": "group", "cycles": 2,
                      "steps": [{"dist_m": 1000.0, "time_s": 300.0,
                                 "pace_min_s": 220, "pace_max_s": 200}],
                      "recovery": None}],
          "reps": [{"dist_m": 1000.0}] * 2}, {"维度", "pace_min_s"}),
    ]
    for bad, expected in cases:
        errors = validate_structure_dict(bad)
        for term in expected:
            assert any(term in error for error in errors)
