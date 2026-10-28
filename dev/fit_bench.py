#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.10"
# dependencies = ["fitparse", "garmin-fit-sdk"]
# ///
"""FIT 解析库对比：fitparse vs garmin-fit-sdk（官方）。

从 data/fit/*.zip 抽样解压真实 FIT，比较两者：解析成功率、record 字段覆盖、耗时。
用法：uv run dev/fit_bench.py [样本数，默认 60]
"""
import glob
import sys
import time
import zipfile
from pathlib import Path

if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = int(sys.argv[1]) if len(sys.argv) > 1 else 60
TMP = ROOT / "data" / ".fit_bench"


def extract():
    TMP.mkdir(exist_ok=True)
    files = []
    for z in sorted(glob.glob(str(ROOT / "data" / "fit" / "*.zip")))[:SAMPLE]:
        try:
            with zipfile.ZipFile(z) as f:
                for n in f.namelist():
                    if n.lower().endswith(".fit"):
                        p = TMP / Path(n).name
                        p.write_bytes(f.read(n))
                        files.append(p)
        except Exception as e:
            print(f"解压失败 {Path(z).name}: {e}")
    return files


def bench_fitparse(files):
    import fitparse
    t = time.time()
    ok, fields, recs = 0, set(), 0
    fails = []
    for p in files:
        try:
            for rec in fitparse.FitFile(str(p)).get_messages("record"):
                recs += 1
                for d in rec:
                    fields.add(d.name)
            ok += 1
        except Exception as e:
            fails.append(f"{p.name}: {e}")
    return time.time() - t, ok, fields, recs, fails


def bench_sdk(files):
    from garmin_fit_sdk import Decoder, Stream
    t = time.time()
    ok, fields, recs = 0, set(), 0
    fails = []
    for p in files:
        try:
            msgs, errors = Decoder(Stream.from_file(str(p))).read()
            if errors:
                fails.append(f"{p.name}: {errors[:1]}")
                continue
            for m in msgs.get("record_mesgs", []):
                recs += 1
                fields.update(m.keys())
            ok += 1
        except Exception as e:
            fails.append(f"{p.name}: {e}")
    return time.time() - t, ok, fields, recs, fails


def main():
    files = extract()
    print(f"样本：{len(files)} 个 FIT（来自前 {SAMPLE} 个 zip）\n")
    t1, ok1, f1, r1, fail1 = bench_fitparse(files)
    t2, ok2, f2, r2, fail2 = bench_sdk(files)
    print(f"fitparse        : {ok1}/{len(files)} 成功, {r1} 条 record, {t1:.2f}s")
    print(f"garmin-fit-sdk  : {ok2}/{len(files)} 成功, {r2} 条 record, {t2:.2f}s")
    print(f"\nfitparse 字段数 {len(f1)}，sdk 字段数 {len(f2)}")
    print(f"仅 fitparse 有: {sorted(f1 - f2, key=str)}")
    print(f"仅 sdk 有     : {sorted(f2 - f1, key=str)}")
    key = ["power", "cadence", "stance_time", "vertical_oscillation", "heart_rate",
           "enhanced_speed", "enhanced_altitude", "step_length"]
    print("\n关键字段覆盖（✓=双方都有）:")
    for k in key:
        print(f"  {k:22s} fitparse={'Y' if k in f1 else '-'} sdk={'Y' if k in f2 else '-'}")
    if fail1:
        print(f"\nfitparse 失败 {len(fail1)}:", *fail1[:3], sep="\n  ")
    if fail2:
        print(f"\nsdk 失败 {len(fail2)}:", *fail2[:3], sep="\n  ")


if __name__ == "__main__":
    main()
