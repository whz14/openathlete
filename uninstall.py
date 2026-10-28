#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""OpenAthlete 卸载清理脚本。

三种模式：
    1. 保留数据（默认）：/plugins remove openathlete 后运行本脚本，清托管副本与
       登记残留，~/.openathlete/ 原样保留——随时可装回继续用。
    2. 软删（--purge-data）：数据 home 整体挪为带时间戳的备份，可后悔。
    3. 硬删（--hard-delete）：彻底删除一切本地记录——数据 home、所有软删备份、
       OAuth token、托管副本、登记。这是用户的生物数据，本脚本提供"被遗忘"的
       能力。不可恢复，需输入 DELETE 确认（--yes 跳过）。
       注意：平台侧授权（佳明/高驰账号里对本应用的授权）只能由用户在账号
       设置中撤销，脚本最后会给出指引。

用法：
    uv run uninstall.py                          # 清插件残留，保留数据
    uv run uninstall.py --kill                   # 先杀掉残留的代理进程再清
    uv run uninstall.py --purge-data             # 软删：数据挪为时间戳备份
    uv run uninstall.py --hard-delete            # 硬删：彻底删除，不可恢复
    uv run uninstall.py --hard-delete --include-kimi-credentials
        # 硬删 + 清理 kimi 凭据库中的 coros 直连 token（如曾 /mcp-config login coros）

可选收尾：uv cache clean（清 uv 全局缓存，机器级副作用，自行决定）。
"""
import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

HOME = Path(os.environ["OPENATHLETE_HOME"]).expanduser() \
    if os.environ.get("OPENATHLETE_HOME") else Path.home() / ".openathlete"
MANAGED = Path.home() / ".kimi-code/plugins/managed/openathlete"
INSTALLED_JSON = Path.home() / ".kimi-code/plugins/installed.json"
KIMI_CRED_DIR = Path.home() / ".kimi-code/credentials/mcp"


def find_proxy_pids():
    """跨平台找 coros_mcp_proxy 进程 PID（尽力而为，找不到返回空）。"""
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "
                 "'coros_mcp_proxy' -and $_.Name -match 'python|uv' }"
                 " | Select-Object -ExpandProperty ProcessId"],
                capture_output=True, text=True, timeout=30).stdout
        else:
            out = subprocess.run(["pgrep", "-f", "coros_mcp_proxy"],
                                 capture_output=True, text=True, timeout=10).stdout
        return [int(x) for x in out.split() if x.strip().isdigit()]
    except Exception:
        return []


def kill_proxies():
    pids = find_proxy_pids()
    if not pids:
        print("· 没有残留的 coros 代理进程")
        return
    for pid in pids:
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=10)
            else:
                os.kill(pid, 15)
            print(f"· 已终止代理进程 PID {pid}")
        except Exception as e:
            print(f"! 终止 PID {pid} 失败：{e}（可手动结束后重跑）")


def remove_managed():
    if not MANAGED.exists():
        print("· 托管副本不存在（可能已被 /plugins remove 清理）")
        return
    try:
        shutil.rmtree(MANAGED)
        print(f"· 已删除托管副本 {MANAGED}")
    except OSError as e:
        print(f"! 删除托管副本失败：{e}")
        print("  通常是有代理进程还在跑——加 --kill 重试，或关闭相关会话后重跑。")


def remove_registration():
    if not INSTALLED_JSON.exists():
        return
    try:
        data = json.loads(INSTALLED_JSON.read_text(encoding="utf-8"))
        before = len(data.get("plugins", []))
        data["plugins"] = [p for p in data.get("plugins", []) if p.get("id") != "openathlete"]
        if len(data["plugins"]) < before:
            INSTALLED_JSON.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                      encoding="utf-8")
            print("· 已从 installed.json 摘除 openathlete 登记")
        else:
            print("· installed.json 中没有 openathlete 登记")
    except Exception as e:
        print(f"! 处理 installed.json 失败：{e}（可手动编辑该文件删除 openathlete 条目）")


def find_data_backups():
    return sorted(HOME.parent.glob(HOME.name + ".bak-*"))


def purge_data(assume_yes):
    """软删：数据 home 挪为时间戳备份。"""
    if not HOME.exists():
        print("· 数据 home 不存在，无需清理")
        return
    if not assume_yes:
        ans = input(f"确认把数据 home {HOME} 挪为备份？"
                    "（含训练数据/记忆/OAuth token/同步进度——之后重跑同步会重复上传）[y/N] ")
        if ans.strip().lower() != "y":
            print("· 跳过数据 home 清理")
            return
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = HOME.with_name(f"{HOME.name}.bak-{stamp}")
    try:
        shutil.move(str(HOME), str(backup))
        print(f"· 数据 home 已挪到备份 {backup}（确认无误后可手动删除）")
    except OSError as e:
        print(f"! 移动数据 home 失败：{e}（可能有进程占用，加 --kill 重试）")


def hard_delete(assume_yes, include_kimi_credentials):
    """硬删：彻底删除一切本地记录，不可恢复。"""
    targets = [p for p in [HOME, *find_data_backups()] if p.exists()]
    print("硬删将**永久删除**以下内容（不可恢复）：")
    for t in targets:
        print(f"  - {t}")
    print(f"  - {MANAGED}（托管副本）")
    print("  - installed.json 中的 openathlete 登记")
    kimi_creds = []
    if include_kimi_credentials and KIMI_CRED_DIR.exists():
        kimi_creds = [p for pat in ("coros-*-client.json", "coros-*-tokens.json")
                      for p in KIMI_CRED_DIR.glob(pat)]
        for p in kimi_creds:
            print(f"  - {p}（kimi 凭据库 coros 直连 token）")
    if not targets and not MANAGED.exists():
        print("· 没有找到任何可删内容")
        return
    if not assume_yes:
        ans = input("\n这是你的生物数据与全部记忆，删除后无法恢复"
                    "（含同步进度——之后重跑同步会重复上传已同步活动）。确认请输入 DELETE：")
        if ans.strip() != "DELETE":
            print("· 已取消")
            return
    kill_proxies()
    remove_managed()
    remove_registration()
    for t in targets:
        try:
            shutil.rmtree(t)
            print(f"· 已永久删除 {t}")
        except OSError as e:
            print(f"! 删除 {t} 失败：{e}")
    for p in kimi_creds:
        try:
            p.unlink()
            print(f"· 已删除 {p.name}")
        except OSError as e:
            print(f"! 删除 {p.name} 失败：{e}")
    print("""
本地记录已清除。最后一步需要你手动完成（真正的"被遗忘"）：
· 高驰：COROS App → 账号设置/授权管理，撤销对本应用的授权
· 佳明：Garmin Connect 账号设置 → 已连接应用，撤销授权
· 若配置过飞书 webhook，到飞书群机器人设置中删除该机器人""")


def main():
    ap = argparse.ArgumentParser(description="OpenAthlete 卸载清理")
    ap.add_argument("--kill", action="store_true", help="先终止残留的 coros 代理进程")
    ap.add_argument("--purge-data", action="store_true",
                    help="软删：数据 home 挪为时间戳备份，可后悔")
    ap.add_argument("--hard-delete", action="store_true",
                    help="硬删：彻底删除数据/备份/token/插件残留，不可恢复")
    ap.add_argument("--include-kimi-credentials", action="store_true",
                    help="硬删时同时清理 kimi 凭据库中的 coros 直连 token")
    ap.add_argument("--yes", action="store_true", help="跳过确认提示")
    args = ap.parse_args()

    if args.hard_delete:
        hard_delete(args.yes, args.include_kimi_credentials)
        print("\n· Kimi Code 里 /reload 让卸载生效")
        return

    pids = find_proxy_pids()
    if pids and not args.kill:
        print(f"发现 {len(pids)} 个 coros 代理进程还在运行：{pids}")
        print("继续可能在 Windows 上因文件锁失败；建议加 --kill 重跑，或先关闭相关会话。\n")
    if args.kill:
        kill_proxies()
    remove_managed()
    remove_registration()
    if args.purge_data:
        purge_data(args.yes)
    else:
        print(f"· 数据 home 已保留：{HOME}（软删加 --purge-data，硬删加 --hard-delete）")

    print("\n完成。收尾事项：")
    print("· Kimi Code 里 /reload 让卸载生效")
    print("· 可选：uv cache clean（清 uv 全局缓存，影响本机所有 uv 项目，自行决定）")


if __name__ == "__main__":
    main()
