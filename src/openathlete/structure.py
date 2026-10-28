#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""课型结构解析共享核心：圈序列 → 块/组/折叠/nl（纯函数，无 IO）。

输入是已从 FIT 提取的 Lap 与 session 汇总（IO 在各入口脚本），输出即
DESIGN.md §3 课型结构契约 JSON。跑步用配速比判恢复，骑行换功率比
（无功率计退心率比）并支持时间型组（按时长折叠、时长型 nl）。
"""
from dataclasses import dataclass, field

PARSER_VERSION = "1.0.0"

# --- 跑步阈值（原始 FIT 实测调定） ---
# 恢复圈判定：圈配速慢于快组中位配速的该倍数即视为恢复（站休/慢跑恢复对比通常 >1.8）
RECOVERY_RATIO = 1.35
# 组间配速差异小于该比例则视为连续跑而非间歇；也用于热身/放松块识别与波动提示
CONTINUOUS_RATIO = 1.20
# 佳明手动短圈旁证：偏慢但未达恢复比的短圈视为手动结束组后的慢跑恢复
MANUAL_RECOVERY_RATIO = 1.12
MANUAL_MAX_TIME_S = 180
# 无配速空圈：时长达到该值视为站休恢复圈，否则视为误触丢弃
EMPTY_RECOVERY_MIN_S = 3
# 折叠聚类容差：类内偏差 ≤12% 或 ≤100m（短距离绝对噪声大，给绝对下限）
FOLD_REL_TOL = 0.12
FOLD_DIST_ABS_TOL_M = 100
# 时长折叠的绝对容差（类比距离规则：短时长组给绝对下限）
FOLD_TIME_ABS_TOL_S = 20
# 站休判定（DESIGN.md §3 文法约束：standing ⇔ time<45 且 dist<60）
STANDING_MAX_TIME_S = 45
STANDING_MAX_DIST_M = 60

# --- 骑行阈值（通勤样本调定：真实间歇样本缺，正例由合成单测覆盖） ---
# 功率比：圈均功率低于快组中位功率/该值判恢复（间歇/恢复功率比通常 ≥1.6）
RIDE_POWER_RECOVERY_RATIO = 1.30
RIDE_POWER_CONTINUOUS_RATIO = 1.15
RIDE_POWER_MANUAL_RATIO = 1.10
# 心率比：心率有滞后，阈值放宽（快组 160 vs 恢复 135 ≈ 1.19）
RIDE_HR_RECOVERY_RATIO = 1.12
RIDE_HR_CONTINUOUS_RATIO = 1.06
RIDE_HR_MANUAL_RATIO = 1.05
# 恢复圈时长须 ≤ 快组中位时长的该比例——通勤停表圈（时长更长）不误判恢复
RIDE_RECOVERY_MAX_TIME_SHARE = 0.7
# 恢复圈速度须 ≤ 快组中位速度的该比例——下坡低心率/低功率高速圈不是恢复
# （代价：爬坡重复组的下坡滑行恢复检不出，退化为连续+波动提示，留待 record 流增强）
RIDE_RECOVERY_MAX_SPEED_SHARE = 0.9
# nl 渲染：功率差 <5W / 心率差 <3bpm 视为定值（类比跑步配速差 <3s/km）
POWER_NL_TIGHT_W = 5
HR_NL_TIGHT_BPM = 3
PACE_NL_TIGHT_S = 3


@dataclass
class Lap:
    dist_m: float
    time_s: float
    pace: float | None = None      # s/km
    avg_hr: float | None = None
    max_hr: float | None = None
    avg_power: float | None = None
    trigger: str | None = None


@dataclass
class Block:
    """合并后的连续同标签圈段：rep（连续 fast 圈）或 recovery。"""
    kind: str
    dist_m: float
    time_s: float
    laps: int
    pace: float | None = None
    avg_hr: int | None = None
    avg_power: int | None = None


@dataclass
class Group:
    cycles: int
    steps: list
    recovery: dict | None = None
    kind: str = "group"

    def to_dict(self):
        return {"kind": self.kind, "cycles": self.cycles,
                "steps": self.steps, "recovery": self.recovery}


@dataclass
class Structure:
    """DESIGN.md §3 课型结构契约。to_dict 键序即契约 JSON 键序。"""
    type: str                      # intervals / continuous / unknown
    nl: str | None = None
    blocks: list = field(default_factory=list)
    reps: list | None = None
    recoveries: list | None = None
    wave_hint: bool | None = None
    structure_version: int = 1

    def to_dict(self):
        d = {"structure_version": self.structure_version, "type": self.type}
        if self.nl is not None:
            d["nl"] = self.nl
        if self.blocks:
            d["blocks"] = [b.to_dict() if isinstance(b, Group) else b for b in self.blocks]
        if self.reps is not None:
            d["reps"] = self.reps
        if self.recoveries is not None:
            d["recoveries"] = self.recoveries
        if self.wave_hint is not None:
            d["wave_hint"] = self.wave_hint
        return d


@dataclass(frozen=True)
class MetricProfile:
    """按运动类型替换的快慢判定指标与阈值。"""
    name: str                      # pace / power / hr
    higher_is_slower: bool         # pace 越大越慢；power/hr 越小越慢
    recovery_ratio: float
    continuous_ratio: float
    manual_ratio: float
    allow_duration_fold: bool
    recovery_max_time_share: float | None
    recovery_max_speed_share: float | None
    nl_unit: str | None            # 骑行 nl 强度单位（W / bpm）；跑步 None 用配速


RUN_PROFILE = MetricProfile("pace", True, RECOVERY_RATIO, CONTINUOUS_RATIO,
                            MANUAL_RECOVERY_RATIO, False, None, None, None)
RIDE_POWER_PROFILE = MetricProfile("power", False, RIDE_POWER_RECOVERY_RATIO,
                                   RIDE_POWER_CONTINUOUS_RATIO, RIDE_POWER_MANUAL_RATIO,
                                   True, RIDE_RECOVERY_MAX_TIME_SHARE,
                                   RIDE_RECOVERY_MAX_SPEED_SHARE, "W")
RIDE_HR_PROFILE = MetricProfile("hr", False, RIDE_HR_RECOVERY_RATIO,
                                RIDE_HR_CONTINUOUS_RATIO, RIDE_HR_MANUAL_RATIO,
                                True, RIDE_RECOVERY_MAX_TIME_SHARE,
                                RIDE_RECOVERY_MAX_SPEED_SHARE, "bpm")


def fmt_pace(sec_per_km):
    if sec_per_km is None:
        return "-"
    sec_per_km = max(0, sec_per_km)
    return "{}:{:02d}".format(int(sec_per_km // 60), int(round(sec_per_km % 60)))


def fmt_dur(seconds):
    seconds = int(round(seconds))
    return "{}:{:02d}".format(seconds // 60, seconds % 60)


def fmt_dist(meters):
    km = meters / 1000
    if km < 1:
        return "{}m".format(int(round(meters / 10) * 10))
    for nd in (1, 2):
        if abs(km - round(km, nd)) < 0.02:
            return ("{:." + str(nd) + "f}km").format(round(km, nd))
    return "{:.2f}km".format(km)


def median(values):
    values = sorted(values)
    n = len(values)
    if n == 0:
        return None
    mid = n // 2
    return values[mid] if n % 2 else (values[mid - 1] + values[mid]) / 2


def profile_value(lap, profile):
    if profile.name == "pace":
        return lap.pace
    if profile.name == "power":
        return lap.avg_power
    return lap.avg_hr


def _slower(value, fast_med, ratio, higher_is_slower):
    if higher_is_slower:
        return value > fast_med * ratio
    return value < fast_med / ratio


def classify_laps(laps, profile=RUN_PROFILE):
    """给每圈打标签：fast / recovery / empty。返回 (labels, fast_median)。"""
    vals = [profile_value(l, profile) for l in laps]
    present = [v for v in vals if v]
    if not present:
        return ["fast"] * len(laps), None
    overall = median(present)
    if profile.higher_is_slower:
        fast_half = [v for v in present if v <= overall]
    else:
        fast_half = [v for v in present if v >= overall]
    fast_med = median(fast_half) or overall
    fast_med_time = fast_med_speed = None
    if profile.recovery_max_time_share is not None:
        fast_laps = [l for l, v in zip(laps, vals) if v and not _slower(
            v, fast_med, profile.recovery_ratio, profile.higher_is_slower)]
        fast_med_time = median([l.time_s for l in fast_laps])
        fast_med_speed = median([l.dist_m / l.time_s for l in fast_laps
                                 if l.dist_m > 1 and l.time_s > 0])
    labels = []
    for lap, v in zip(laps, vals):
        if not v:  # 无强度数据（误触空圈、站休）
            labels.append("recovery" if lap.time_s >= EMPTY_RECOVERY_MIN_S else "empty")
            continue
        slow = _slower(v, fast_med, profile.recovery_ratio, profile.higher_is_slower)
        if not slow and lap.trigger == "manual" and lap.time_s <= MANUAL_MAX_TIME_S:
            # 手动短圈且偏慢：典型手动结束组、慢跑恢复
            slow = _slower(v, fast_med, profile.manual_ratio, profile.higher_is_slower)
        if slow and fast_med_time and \
                lap.time_s > fast_med_time * profile.recovery_max_time_share:
            slow = False  # 时长不比快组短的慢圈不是恢复（通勤停表）
        if slow and fast_med_speed and profile.recovery_max_speed_share is not None \
                and lap.dist_m > 1 and lap.time_s > 0 and \
                lap.dist_m / lap.time_s > fast_med_speed * profile.recovery_max_speed_share:
            slow = False  # 速度不比快组慢的弱强度圈不是恢复（下坡）
        labels.append("recovery" if slow else "fast")
    return labels, fast_med


def build_structure(laps, labels):
    """把圈序列合并成 Block：rep（连续 fast）/ recovery。"""
    blocks = []
    cur = None
    for lap, label in zip(laps, labels):
        if label == "empty":
            continue
        kind = "rep" if label == "fast" else "recovery"
        if cur and cur["kind"] == kind:
            cur["dist_m"] += lap.dist_m
            cur["time_s"] += lap.time_s
            cur["laps"] += 1
            if lap.avg_hr:
                cur["hrs"].append(lap.avg_hr)
            if lap.avg_power:
                cur["powers"].append(lap.avg_power)
        else:
            if cur:
                blocks.append(cur)
            cur = {"kind": kind, "dist_m": lap.dist_m, "time_s": lap.time_s,
                   "laps": 1, "hrs": [lap.avg_hr] if lap.avg_hr else [],
                   "powers": [lap.avg_power] if lap.avg_power else []}
    if cur:
        blocks.append(cur)
    out = []
    for b in blocks:
        pace = (b["time_s"] / (b["dist_m"] / 1000)) if b["dist_m"] > 1 else None
        avg_hr = int(round(sum(b["hrs"]) / len(b["hrs"]))) if b["hrs"] else None
        avg_power = int(round(sum(b["powers"]) / len(b["powers"]))) if b["powers"] else None
        out.append(Block(kind=b["kind"], dist_m=b["dist_m"], time_s=b["time_s"],
                         laps=b["laps"], pace=pace, avg_hr=avg_hr, avg_power=avg_power))
    return out


def fold_reps(reps, key="dist_m", abs_tol=FOLD_DIST_ABS_TOL_M):
    """找组距/组时长序列的最小重复周期 p 及类内最大相对偏差。

    [1200]*10 → (1, dev)（10×1.2km）；[4000,3200,2400]*2 → (3, dev)（(4+3.2+2.4km)×2）；
    无重复模式返回 (None, None)（整个序列视为一个大组）。
    """
    n = len(reps)
    for p in range(1, n // 2 + 1):
        if n % p:
            continue
        ok, dev = True, 0.0
        for i in range(p):
            vs = [getattr(reps[j], key) for j in range(i, n, p)]
            m = median(vs)
            if any(abs(v - m) > max(FOLD_REL_TOL * m, abs_tol) for v in vs):
                ok = False
                break
            dev = max(dev, max(abs(v - m) for v in vs) / m)
        if ok:
            return p, dev
    return None, None


def _step_stats(members, profile):
    """step 统计袋按类型扩展：跑步配速/HR，骑行功率/HR（无功率计纯 HR）。"""
    hrs = [b.avg_hr for b in members if b.avg_hr]
    if profile.name == "power":
        ps = [b.avg_power for b in members if b.avg_power]
        return {"power_min_w": min(ps) if ps else None,
                "power_max_w": max(ps) if ps else None,
                "avg_hr_max": max(hrs) if hrs else None, "count": len(members)}
    if profile.name == "hr":
        return {"hr_min": min(hrs) if hrs else None,
                "hr_max": max(hrs) if hrs else None, "count": len(members)}
    ps = [b.pace for b in members if b.pace]
    return {"pace_min_s": round(min(ps), 1) if ps else None,
            "pace_max_s": round(max(ps), 1) if ps else None,
            "avg_hr_max": max(hrs) if hrs else None, "count": len(members)}


def build_group(reps, recs, profile=RUN_PROFILE):
    """把平铺的组/恢复折叠成契约 group 块：{cycles, steps, recovery}。

    跑步只按距离折叠；骑行距离/时长都试，取拟合偏差更小的维度
    （时间型课 10×10min 距离随风速飘，时长稳；距离型课反之）。
    """
    n = len(reps)
    p, dimension = None, "dist_m"
    if n >= 2:
        p_d, dev_d = fold_reps(reps)
        if profile.allow_duration_fold:
            p_t, dev_t = fold_reps(reps, key="time_s", abs_tol=FOLD_TIME_ABS_TOL_S)
            if p_t and (p_d is None or dev_t < dev_d):
                p, dimension = p_t, "time_s"
            else:
                p = p_d
        else:
            p = p_d
    cycles, step_count = (n // p, p) if p else (1, n)
    steps = []
    for i in range(step_count):
        members = reps[i::step_count] if p else [reps[i]]
        vs = [getattr(b, dimension) for b in members]
        step = {dimension: round(median(vs), 0)}
        step.update(_step_stats(members, profile))
        steps.append(step)
    recovery = None
    if recs:
        t_med = median([b.time_s for b in recs])
        d_med = median([b.dist_m for b in recs])
        recovery = {"time_s_med": round(t_med, 0), "dist_m_med": round(d_med, 0),
                    "standing": bool(t_med < STANDING_MAX_TIME_S
                                     and d_med < STANDING_MAX_DIST_M),
                    "count": len(recs)}
    return Group(cycles=cycles, steps=steps, recovery=recovery)


def _rec_desc(recovery, with_dist=True):
    if not recovery:
        return ""
    if recovery["standing"]:
        return "，间休 {}（站休）".format(fmt_dur(recovery["time_s_med"]))
    if not with_dist:
        return "，间休 {}".format(fmt_dur(recovery["time_s_med"]))
    return "，间休 {}（{}）".format(fmt_dur(recovery["time_s_med"]),
                                   fmt_dist(recovery["dist_m_med"]))


def group_nl(group, mean_pace):
    """按 DESIGN.md §3 课型自然语言规范渲染跑步 group 块。"""
    steps, cycles, rec = group.steps, group.cycles, group.recovery
    rec_desc = _rec_desc(rec)

    def step_text(s, with_pace):
        t = fmt_dist(s["dist_m"])
        if with_pace and s["pace_min_s"]:
            if abs(s["pace_max_s"] - s["pace_min_s"]) < PACE_NL_TIGHT_S:
                t += "@{}".format(fmt_pace(s["pace_min_s"]))
            else:
                t += "@{}-{}".format(fmt_pace(s["pace_min_s"]), fmt_pace(s["pace_max_s"]))
        return t

    if cycles > 1 and len(steps) == 1:
        step = steps[0]
        body = "{}×{}".format(cycles, fmt_dist(step["dist_m"]))
        lo, hi = step["pace_min_s"], step["pace_max_s"]
        if lo:
            body += " @{}-{}(均{})".format(fmt_pace(lo), fmt_pace(hi), fmt_pace(mean_pace))
    elif cycles > 1:
        body = "({})×{}".format("+".join(step_text(s, True) for s in steps), cycles)
    else:
        paces = [p for s in steps for p in (s["pace_min_s"], s["pace_max_s"]) if p]
        body = "{}组({})".format(len(steps), "+".join(fmt_dist(s["dist_m"]) for s in steps))
        if paces:
            body += " @{}-{}(均{})".format(fmt_pace(min(paces)), fmt_pace(max(paces)),
                                          fmt_pace(mean_pace))
    return body + rec_desc


def group_nl_ride(group, mean_stat, profile):
    """骑行 group 块的 nl：强度用功率/心率；时间型组按时长渲染，恢复不记距离。"""
    steps, cycles, rec = group.steps, group.cycles, group.recovery
    unit = profile.nl_unit
    lo_key, hi_key = ("power_min_w", "power_max_w") if profile.name == "power" \
        else ("hr_min", "hr_max")
    tight = POWER_NL_TIGHT_W if profile.name == "power" else HR_NL_TIGHT_BPM
    duration_group = steps and "time_s" in steps[0]
    rec_desc = _rec_desc(rec, with_dist=not duration_group)
    mean_txt = str(int(round(mean_stat))) if mean_stat else None

    def seg_text(s):
        return fmt_dist(s["dist_m"]) if "dist_m" in s else fmt_dur(s["time_s"])

    def stat_text(s, with_range=True):
        lo, hi = s.get(lo_key), s.get(hi_key)
        if lo is None:
            return ""
        if with_range and hi - lo >= tight:
            return "@{}-{}{}".format(lo, hi, unit)
        return "@{}{}".format(lo, unit)

    def step_text(s):
        return seg_text(s) + stat_text(s)

    if cycles > 1 and len(steps) == 1:
        s = steps[0]
        body = "{}×{}".format(cycles, seg_text(s))
        if s.get(lo_key) is not None:
            body += " @{}-{}{}".format(s[lo_key], s[hi_key], unit)
            if mean_txt:
                body += "(均{}{})".format(mean_txt, unit)
    elif cycles > 1:
        body = "({})×{}".format("+".join(step_text(s) for s in steps), cycles)
    else:
        vals = [v for s in steps for v in (s.get(lo_key), s.get(hi_key)) if v is not None]
        body = "{}组({})".format(len(steps), "+".join(seg_text(s) for s in steps))
        if vals:
            body += " @{}-{}{}".format(min(vals), max(vals), unit)
            if mean_txt:
                body += "(均{}{})".format(mean_txt, unit)
    return body + rec_desc


def block_nl(b):
    pace = fmt_pace(b.pace) if b.pace else "-"
    return "{} @{}".format(fmt_dist(b.dist_m), pace)


def block_stat_nl(b, profile):
    stat = b.avg_power if profile.name == "power" else b.avg_hr
    if stat is None:
        return fmt_dist(b.dist_m)
    return "{} @{}{}".format(fmt_dist(b.dist_m), stat, profile.nl_unit)


def _steady_block(role, block, profile):
    d = {"kind": "steady", "role": role, "dist_m": block.dist_m, "time_s": block.time_s}
    if profile.name == "pace":
        d["pace_s"] = block.pace
    elif profile.name == "power":
        d["avg_power"] = block.avg_power
    else:
        d["avg_hr"] = block.avg_hr
    return d


def summarize(session, laps):
    """解析跑步课型，返回 (summary_text, Structure)。"""
    if not laps:
        return "无计圈数据", Structure(type="unknown")

    labels, fast_med = classify_laps(laps)
    blocks = build_structure(laps, labels)
    reps = [b for b in blocks if b.kind == "rep"]
    recs = [b for b in blocks if b.kind == "recovery"]

    # 首尾慢跑块判为热身/放松：明显慢于主体 rep
    rep_med = median([b.pace for b in reps if b.pace]) if reps else None
    warmup = cooldown = None
    if reps and rep_med:
        if blocks[0].kind == "rep" and blocks[0] is not reps[-1] and blocks[0].pace and \
                blocks[0].pace > rep_med * CONTINUOUS_RATIO and len(blocks) > 2:
            warmup = blocks[0]
            reps = reps[1:]
        if blocks[-1].kind == "rep" and blocks[-1] is not reps[-1] and blocks[-1].pace and \
                blocks[-1].pace > rep_med * CONTINUOUS_RATIO and len(blocks) > 2:
            cooldown = blocks[-1]
            reps = reps[:-1]

    rep_list = [{
        "dist_m": round(b.dist_m, 1), "time_s": round(b.time_s, 1),
        "pace_s": round(b.pace, 1) if b.pace else None,
        "pace": fmt_pace(b.pace), "avg_hr": b.avg_hr, "laps": b.laps,
    } for b in reps]
    rec_list = [{"dist_m": round(b.dist_m, 1), "time_s": round(b.time_s, 1)} for b in recs]

    if len(reps) >= 2 and recs:
        paces = [b.pace for b in reps if b.pace]
        mean_pace = sum(paces) / len(paces)
        group = build_group(reps, recs)
        nl_parts = []
        blocks_out = []
        if warmup:
            blocks_out.append(_steady_block("warmup", warmup, RUN_PROFILE))
            nl_parts.append("热身 " + block_nl(warmup))
        blocks_out.append(group)
        nl_parts.append(group_nl(group, mean_pace))
        if cooldown:
            blocks_out.append(_steady_block("cooldown", cooldown, RUN_PROFILE))
            nl_parts.append("放松 " + block_nl(cooldown))
        nl = " → ".join(nl_parts)
        return nl, Structure(
            type="intervals", nl=nl,
            blocks=blocks_out, reps=rep_list, recoveries=rec_list)

    # 无恢复结构：连续跑（含配速均匀的场地课）
    total_d = session.get("distance_m") or sum(l.dist_m for l in laps)
    total_t = session.get("time_s") or sum(l.time_s for l in laps)
    pace = total_t / (total_d / 1000) if total_d else None
    lap_paces = [l.pace for l in laps if l.pace]
    wave = ""
    if lap_paces and fast_med and max(lap_paces) > fast_med * CONTINUOUS_RATIO:
        wave = "；圈速有波动但无明确恢复圈（可能为变速跑/法特莱克或恢复对比过小），建议 --laps 看逐圈"
    nl = "连续 {} @{}（无间歇结构）{}".format(fmt_dist(total_d), fmt_pace(pace), wave)
    return nl, Structure(
        type="continuous", nl=nl,
        blocks=[{"kind": "steady", "role": "main", "dist_m": total_d,
                 "time_s": total_t, "pace_s": pace}],
        wave_hint=bool(wave))


def select_ride_profile(laps):
    """骑行判定指标选择：过半圈有功率用功率比，否则过半有心率用心率比。"""
    n_power = sum(1 for l in laps if l.avg_power)
    n_hr = sum(1 for l in laps if l.avg_hr)
    if n_power and n_power * 2 >= len(laps):
        return RIDE_POWER_PROFILE
    if n_hr and n_hr * 2 >= len(laps):
        return RIDE_HR_PROFILE
    return None


def has_ride_power(session, laps):
    if session.get("avg_power"):
        return True
    return select_ride_profile(laps) is RIDE_POWER_PROFILE


def ride_is_training(session, laps, threshold_km):
    distance = session.get("distance_m") or sum(l.dist_m for l in laps)
    return has_ride_power(session, laps) or distance >= threshold_km * 1000


def summarize_commute_ride(session, laps):
    """阈值候选的连续结构；只分派解析，不保存通勤/训练标签。"""
    total_d = session.get("distance_m") or sum(l.dist_m for l in laps)
    total_t = session.get("time_s") or sum(l.time_s for l in laps)
    hrs = [l.avg_hr for l in laps if l.avg_hr]
    avg_hr = int(round(sum(hrs) / len(hrs))) if hrs else None
    block = {"kind": "steady", "role": "main", "dist_m": total_d, "time_s": total_t}
    stat = " @{}bpm".format(avg_hr) if avg_hr else ""
    if avg_hr:
        block["avg_hr"] = avg_hr
    nl = "连续 {}{}（无间歇结构）".format(fmt_dist(total_d), stat)
    return nl, Structure(type="continuous", nl=nl, blocks=[block], wave_hint=False)


def summarize_ride(session, laps):
    """骑行课型解析：骨架同跑步，快慢判定换功率比/心率比，支持时间型组。"""
    if not laps:
        return "无计圈数据", Structure(type="unknown")

    total_d = session.get("distance_m") or sum(l.dist_m for l in laps)
    total_t = session.get("time_s") or sum(l.time_s for l in laps)
    profile = select_ride_profile(laps)
    if profile is None:
        nl = "连续 {}（无间歇结构）".format(fmt_dist(total_d))
        return nl, Structure(
            type="continuous", nl=nl,
            blocks=[{"kind": "steady", "role": "main", "dist_m": total_d, "time_s": total_t}],
            wave_hint=False)

    labels, fast_med = classify_laps(laps, profile)
    blocks = build_structure(laps, labels)
    reps = [b for b in blocks if b.kind == "rep"]
    recs = [b for b in blocks if b.kind == "recovery"]

    def stat_of(b):
        return b.avg_power if profile.name == "power" else b.avg_hr

    rep_med = median([stat_of(b) for b in reps if stat_of(b)]) if reps else None
    warmup = cooldown = None
    if reps and rep_med:
        if blocks[0].kind == "rep" and blocks[0] is not reps[-1] and stat_of(blocks[0]) and \
                stat_of(blocks[0]) < rep_med / profile.continuous_ratio and len(blocks) > 2:
            warmup = blocks[0]
            reps = reps[1:]
        if blocks[-1].kind == "rep" and blocks[-1] is not reps[-1] and stat_of(blocks[-1]) and \
                stat_of(blocks[-1]) < rep_med / profile.continuous_ratio and len(blocks) > 2:
            cooldown = blocks[-1]
            reps = reps[:-1]

    rep_list = [{
        "dist_m": round(b.dist_m, 1), "time_s": round(b.time_s, 1),
        "avg_power": b.avg_power, "avg_hr": b.avg_hr, "laps": b.laps,
    } for b in reps]
    rec_list = [{"dist_m": round(b.dist_m, 1), "time_s": round(b.time_s, 1)} for b in recs]

    if len(reps) >= 2 and recs:
        vals = [stat_of(b) for b in reps if stat_of(b)]
        mean_stat = sum(vals) / len(vals) if vals else None
        group = build_group(reps, recs, profile)
        nl_parts = []
        blocks_out = []
        if warmup:
            blocks_out.append(_steady_block("warmup", warmup, profile))
            nl_parts.append("热身 " + block_stat_nl(warmup, profile))
        blocks_out.append(group)
        nl_parts.append(group_nl_ride(group, mean_stat, profile))
        if cooldown:
            blocks_out.append(_steady_block("cooldown", cooldown, profile))
            nl_parts.append("放松 " + block_stat_nl(cooldown, profile))
        nl = " → ".join(nl_parts)
        return nl, Structure(
            type="intervals", nl=nl,
            blocks=blocks_out, reps=rep_list, recoveries=rec_list)

    wave = ""
    lap_vals = [profile_value(l, profile) for l in laps if profile_value(l, profile)]
    if lap_vals and fast_med and _slower(min(lap_vals) if not profile.higher_is_slower
                                         else max(lap_vals), fast_med,
                                         profile.continuous_ratio, profile.higher_is_slower):
        wave = "；圈强度有波动但无明确恢复圈（可能为变速骑行或恢复对比过小）"
    field = "avg_power" if profile.name == "power" else "avg_hr"
    values = [getattr(lap, field) for lap in laps if getattr(lap, field)]
    average = int(round(sum(values) / len(values))) if values else None
    unit = "W" if profile.name == "power" else "bpm"
    stat_txt = " @{}{}".format(average, unit) if average is not None else ""
    nl = "连续 {}{}（无间歇结构）{}".format(fmt_dist(total_d), stat_txt, wave)
    block = {"kind": "steady", "role": "main", "dist_m": total_d, "time_s": total_t}
    if average is not None:
        block[field] = average
    return nl, Structure(type="continuous", nl=nl, blocks=[block], wave_hint=bool(wave))


def validate_structure_dict(d):
    """构建端校验器：文法之外的跨字段约束（DESIGN.md §3）。返回错误列表。"""
    errs = []
    blocks = d.get("blocks") or []
    reps = d.get("reps")
    groups = [b for b in blocks if b.get("kind") == "group"]
    if groups and reps is not None:
        expect = sum(g["cycles"] * len(g["steps"]) for g in groups)
        if expect != len(reps):
            errs.append("group cycles×len(steps)={} 与 reps 计数 {} 不一致".format(
                expect, len(reps)))
    for g in groups:
        for s in g.get("steps", []):
            if ("dist_m" in s) == ("time_s" in s):
                errs.append("step 必须有且仅有一个维度（dist_m/time_s）：{}".format(s))
            for lo, hi in (("pace_min_s", "pace_max_s"), ("power_min_w", "power_max_w"),
                           ("hr_min", "hr_max")):
                if s.get(lo) is not None and s.get(hi) is not None and s[lo] > s[hi]:
                    errs.append("{} > {}：{}".format(lo, hi, s))
        rec = g.get("recovery")
        if rec is not None:
            standing = rec["time_s_med"] < STANDING_MAX_TIME_S \
                and rec["dist_m_med"] < STANDING_MAX_DIST_M
            if bool(rec.get("standing")) != bool(standing):
                errs.append("recovery standing 与 time/dist 约束不一致：{}".format(rec))
    if d.get("type") == "continuous" and groups:
        errs.append("continuous 结构不应包含 group 块")
    if d.get("type") == "intervals" and not groups:
        errs.append("intervals 结构缺少 group 块")
    return errs
