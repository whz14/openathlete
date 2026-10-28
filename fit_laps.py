#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.10"
# dependencies = ["fitparse"]
# ///
"""从原始 FIT 还原训练课真实结构（圈/组/恢复），修正平台接口只给自动距离圈的问题。

背景：高驰 lap 接口对导入活动只返回重切的 1/5/10km 自动圈，间歇课被平均成
"变速跑"；而原始 FIT 里的 lap 消息完整保留手表计圈（佳明含 lap_trigger，
高驰原生无 trigger 但圈列表本身完整）。本脚本直接解析原始 FIT：

  uv run ./fit_laps.py YYYY-MM-DD                 # 某天全部佳明活动
  uv run ./fit_laps.py YYYY-MM-DD YYYY-MM-DD      # 日期范围
  uv run ./fit_laps.py --id GARMIN_ACTIVITY_ID    # 指定佳明 activity_id
  uv run ./fit_laps.py --file xxx.fit             # 解析任意裸 FIT
  加 --laps 输出逐圈明细；加 --json 输出机器可读结构。

分组规则：先按"配速明显慢于快组中位数"识别恢复圈（佳明手动圈作旁证），
恢复圈之间的连续快跑圈合并为一组；无恢复圈且配速均匀则判为连续跑。
解析核心在 openathlete.structure，根目录 workout_structure.py 保留兼容导出。
"""
import argparse
import io
import json
import os
import sqlite3
import sys
from pathlib import Path

if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import fitparse

from workout_structure import (
    Lap, classify_laps, summarize, fmt_pace, fmt_dur, fmt_dist,
)

OPENATHLETE_HOME = Path(
    os.environ.get("OPENATHLETE_HOME", Path.home() / ".openathlete")
)
DB_PATH = OPENATHLETE_HOME / "sync.db"
FIT_DIR = OPENATHLETE_HOME / "fit"


def fit_paths_for_id(activity_id):
    exact = sorted(FIT_DIR.rglob("*-garmin-{}.fit".format(activity_id)))
    segments = sorted(FIT_DIR.rglob("*-garmin-{}#*.fit".format(activity_id)))
    return exact + segments


def parse_fit(raw):
    fit = fitparse.FitFile(io.BytesIO(raw))
    session = {}
    laps = []
    for m in fit.get_messages():
        if m.name == "session":
            d = {x.name: x.value for x in m}
            session = {
                "sport": d.get("sport"),
                "sub_sport": d.get("sub_sport"),
                "distance_m": d.get("total_distance"),
                "time_s": d.get("total_elapsed_time"),
                "start_time": str(d.get("start_time") or ""),
            }
        elif m.name == "lap":
            d = {x.name: x.value for x in m}
            dist = d.get("total_distance") or 0.0
            t = d.get("total_elapsed_time") or d.get("total_timer_time") or 0.0
            laps.append(Lap(
                dist_m=dist,
                time_s=t,
                pace=(t / (dist / 1000)) if dist > 1 and t > 0 else None,
                avg_hr=d.get("avg_heart_rate"),
                max_hr=d.get("max_heart_rate"),
                avg_power=d.get("avg_power"),
                trigger=d.get("lap_trigger"),
            ))
    return session, laps


def activities_for_dates(start, end):
    if not DB_PATH.exists():
        raise SystemExit("找不到 sync.db：{}（尚未同步过佳明数据？）".format(DB_PATH))
    db = sqlite3.connect(DB_PATH)
    rows = db.execute(
        "select activity_id, start_time_local from garmin_activity "
        "where date(start_time_local) between date(?) and date(?) order by start_time_local",
        (start, end)).fetchall()
    return rows


def render(activity_label, fit_path, show_laps):
    raw = Path(fit_path).read_bytes()
    session, laps = parse_fit(raw)
    summary, struct = summarize(session, laps)
    out = {
        "activity": activity_label,
        "sport": session.get("sport"), "sub_sport": session.get("sub_sport"),
        "summary": summary, "structure": struct.to_dict(),
    }
    lines = ["{}  {}/{}  {}km  {}".format(
        activity_label, session.get("sport"), session.get("sub_sport"),
        round((session.get("distance_m") or 0) / 1000, 2), summary)]
    if show_laps and laps:
        labels, _ = classify_laps(laps)
        lines.append("  圈# | 距离 | 时间 | 配速 | 均HR | 触发 | 判定")
        for i, (lap, label) in enumerate(zip(laps, labels), 1):
            lines.append("  {:3d} | {:>7} | {:>5} | {:>5} | {:>4} | {} | {}".format(
                i, fmt_dist(lap.dist_m), fmt_dur(lap.time_s), fmt_pace(lap.pace),
                lap.avg_hr or "-", lap.trigger, label))
    return out, "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="从原始 FIT 还原训练课真实结构")
    ap.add_argument("start", nargs="?", help="开始日期 YYYY-MM-DD")
    ap.add_argument("end", nargs="?", help="结束日期 YYYY-MM-DD（默认同开始）")
    ap.add_argument("--id", help="佳明 activity_id（直接用本地归档）")
    ap.add_argument("--file", help="直接解析裸 .fit 文件（如高驰下载的原生 FIT）")
    ap.add_argument("--laps", action="store_true", help="输出逐圈明细")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    results = []
    if args.file:
        out, text = render(Path(args.file).name, args.file, args.laps)
        results.append((out, text))
    elif args.id:
        paths = fit_paths_for_id(args.id)
        if not paths:
            raise SystemExit("本地归档没有该活动：{}".format(args.id))
        for path in paths:
            label = args.id if len(paths) == 1 else "{} ({})".format(args.id, path.name)
            out, text = render(label, path, args.laps)
            results.append((out, text))
    elif args.start:
        rows = activities_for_dates(args.start, args.end or args.start)
        if not rows:
            print("该日期范围内没有佳明活动记录。")
            return
        for activity_id, start_local in rows:
            paths = fit_paths_for_id(activity_id)
            if not paths:
                results.append((None, "{}  [跳过] 本地无 FIT 归档".format(start_local)))
                continue
            for path in paths:
                label = "{} ({})".format(start_local, activity_id)
                if len(paths) > 1:
                    label += " {}".format(path.name)
                out, text = render(label, path, args.laps)
                results.append((out, text))
    else:
        ap.print_help()
        return

    if args.json:
        print(json.dumps([o for o, _ in results if o], ensure_ascii=False, indent=2))
    else:
        print("\n\n".join(t for _, t in results))


if __name__ == "__main__":
    main()
