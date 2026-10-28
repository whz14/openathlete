#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""OpenAthlete 数据移植：把 ~/.openathlete/ 整体打包，换机恢复。

    uv run migrate.py export [输出.zip]     # 打包数据 home（默认 ./openathlete-export-<时间戳>.zip）
    uv run migrate.py export --no-logs      # 打包时排除 logs/
    uv run migrate.py import <包.zip>       # 在新机器恢复到 ~/.openathlete
    uv run migrate.py import <包.zip> --yes # 目标已存在时覆盖（先把现有挪成备份）

新机器上的完整流程：装 uv → /plugins install <仓库> → uv run migrate.py import <包>
→ 开会话即可。OAuth token 随包恢复，理论上直接可用；若失效，代理会引导重新授权。
"""
import argparse
import datetime
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

HOME = Path(os.environ["OPENATHLETE_HOME"]).expanduser() \
    if os.environ.get("OPENATHLETE_HOME") else Path.home() / ".openathlete"
META = "openathlete-export.json"


def do_export(out_arg, no_logs):
    if not HOME.exists():
        sys.exit(f"数据 home 不存在：{HOME}（没什么可导出的）")
    if out_arg:
        out = Path(out_arg).expanduser().resolve()
    else:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
        out = (Path.cwd() / f"openathlete-export-{stamp}.zip").resolve()
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        meta = {"tool": "openathlete-migrate", "version": 1,
                "exportedAt": datetime.datetime.now(datetime.timezone.utc)
                .isoformat(timespec="seconds")}
        z.writestr(META, json.dumps(meta, ensure_ascii=False, indent=1))
        for p in sorted(HOME.rglob("*")):
            if p.is_dir():
                continue
            rel = p.relative_to(HOME)
            if no_logs and rel.parts[0] == "logs":
                continue
            z.write(p, str(rel))
            n += 1
    size_mb = out.stat().st_size / 1024 / 1024
    print(f"已导出 {n} 个文件 → {out}（{size_mb:.1f} MB）")
    print("把这个 zip 拷到新机器，按文件头部注释的流程 import 即可。")


def do_import(zip_path, assume_yes):
    src = Path(zip_path).expanduser().resolve()
    if not src.exists():
        sys.exit(f"找不到导出包：{src}")
    with zipfile.ZipFile(src) as z:
        names = z.namelist()
        if META not in names:
            if not assume_yes:
                ans = input("包内没有导出标记，不像 migrate.py 的产物。仍要解压吗？[y/N] ")
                if ans.strip().lower() != "y":
                    sys.exit("已取消")
        if HOME.exists() and any(HOME.iterdir()):
            if not assume_yes:
                ans = input(f"目标 {HOME} 已存在且非空，先把现有内容挪成备份再恢复？[y/N] ")
                if ans.strip().lower() != "y":
                    sys.exit("已取消")
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = HOME.with_name(f"{HOME.name}.bak-{stamp}")
            shutil.move(str(HOME), str(backup))
            print(f"现有数据 home 已挪到备份 {backup}")
        HOME.mkdir(parents=True, exist_ok=True)
        n = 0
        for name in names:
            if name == META or name.endswith("/"):
                continue
            # 防路径穿越
            target = (HOME / name).resolve()
            if HOME.resolve() not in target.parents and target != HOME.resolve():
                print(f"! 跳过可疑路径 {name}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(name))
            n += 1
    print(f"已恢复 {n} 个文件 → {HOME}")
    print("后续：/plugins install 装好插件后开会话即可；OAuth token 随包恢复，")
    print("失效时按提示 uv run ./coros_mcp_proxy.py --login 重新授权。")


def main():
    ap = argparse.ArgumentParser(description="OpenAthlete 数据移植（打包/恢复 ~/.openathlete）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pe = sub.add_parser("export", help="打包数据 home")
    pe.add_argument("out", nargs="?", help="输出 zip 路径（默认 ./openathlete-export-<时间戳>.zip）")
    pe.add_argument("--no-logs", action="store_true", help="排除 logs/ 目录")
    pi = sub.add_parser("import", help="在新机器恢复数据 home")
    pi.add_argument("zip_path", help="export 生成的 zip")
    pi.add_argument("--yes", action="store_true", help="目标已存在时直接覆盖（先备份现有）")
    args = ap.parse_args()

    if args.cmd == "export":
        do_export(args.out, args.no_logs)
    else:
        do_import(args.zip_path, args.yes)


if __name__ == "__main__":
    main()
