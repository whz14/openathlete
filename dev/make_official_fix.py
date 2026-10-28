# 生成给高驰官方的"可直接复制"修复版 tools/list
# 两个变体：minimal（只把 $defs 提到根部）/ recommended（再拆掉 Section 自引用递归）
import copy, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from coros_mcp_proxy import hoist_nested_defs, check_schema

RAW = json.load(open(ROOT / "dev" / "coros_tools_raw.json", encoding="utf-8"))
OUT = ROOT / "dev" / "official-fix"
OUT.mkdir(parents=True, exist_ok=True)

BROKEN = {"createScheduledWorkout", "updateScheduledWorkout", "createTrainingPlan",
          "updateTrainingPlan", "createSingleWorkout", "updateWorkoutDetails"}


def recommended_fix(schema):
    """先提升 $defs，再把 Section 的自引用拆掉：
    Section.sets.items 改引新定义 PlainSection（= 去掉 sets 的 Section）。
    依据：sets 的字段描述本身就写明 "interval groups cannot nest"。"""
    schema, _ = hoist_nested_defs(schema)
    defs = schema.get("$defs", {})
    if "Section" not in defs:
        return schema, False
    section = defs["Section"]
    sets = section.get("properties", {}).get("sets", {})
    if sets.get("items", {}).get("$ref") != "#/$defs/Section":
        return schema, False
    plain = copy.deepcopy(section)
    plain.get("properties", {}).pop("sets", None)
    defs["PlainSection"] = plain
    sets["items"]["$ref"] = "#/$defs/PlainSection"
    return schema, True


def find_cycles(schema):
    """检测 $defs 之间的引用环（walle/xAI/Anthropic strict 都不喜欢循环）。"""
    defs = schema.get("$defs", {})
    graph = {name: [] for name in defs}

    def collect(node, current):
        if isinstance(node, dict):
            ref = node.get("$ref", "")
            if ref.startswith("#/$defs/"):
                graph[current].append(ref[len("#/$defs/"):])
            for v in node.values():
                collect(v, current)
        elif isinstance(node, list):
            for v in node:
                collect(v, current)

    for name, definition in defs.items():
        collect(definition, name)

    cycles, WHITE, GRAY, BLACK = [], 0, 1, 2
    color = {n: WHITE for n in graph}

    def dfs(n, path):
        color[n] = GRAY
        for m in graph.get(n, []):
            if m not in graph:
                continue
            if color[m] == GRAY:
                cycles.append(path + [m])
            elif color[m] == WHITE:
                dfs(m, path + [m])
        color[n] = BLACK

    for n in graph:
        if color[n] == WHITE:
            dfs(n, [n])
    return cycles


results = {}
for variant, fixer in (("minimal", lambda s: hoist_nested_defs(s)[0]),
                       ("recommended", recommended_fix)):
    fixed_tools, touched = [], []
    for t in RAW:
        schema = t["inputSchema"]
        if t["name"] in BROKEN:
            schema = fixer(schema)
            if variant == "recommended":
                schema, changed = schema
                assert changed, f"{t['name']} 未按预期拆掉递归"
        fixed_tools.append({**t, "inputSchema": schema})
        if t["name"] in BROKEN:
            touched.append(t["name"])

    problems = []
    for t in fixed_tools:
        problems += [f"{t['name']}: {p}" for p in check_schema(t["inputSchema"])]
        for c in find_cycles(t["inputSchema"]):
            problems.append(f"{t['name']}: 仍存在引用环 {' -> '.join(c)}")

    out = OUT / f"tools-list.{variant}.json"
    json.dump({"tools": fixed_tools}, open(out, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    results[variant] = (out, touched, problems)
    print(f"[{variant}] 写出 {out.name}；修复 {len(touched)} 个；自检问题 {len(problems)} 个")
    for p in problems:
        print("   !!", p)

# 只含 6 个修复后 inputSchema 的小文件（方便他们做外科手术式替换）
rec = json.load(open(results["recommended"][0], encoding="utf-8"))["tools"]
patched = {t["name"]: t["inputSchema"] for t in rec if t["name"] in BROKEN}
json.dump(patched, open(OUT / "changed-schemas-only.recommended.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
print("[changed-schemas-only] 6 个修复后的 inputSchema 已单独导出")

# ---- 语义等价性验证（jsonschema 规范级）
import jsonschema

def validate(schema, doc):
    v = jsonschema.validators.validator_for(schema)(schema)
    v.validate(doc)

course_doc = {"course": {"courseDescription": "d", "courseName": "n", "sportType": 1,
                         "sections": [{"sectionType": 1},
                                      {"sectionType": 4, "sets": [{"sectionType": 2},
                                                                  {"sectionType": 3}]}]}}

sch_min = next(t for t in json.load(open(results["minimal"][0], encoding="utf-8"))["tools"]
               if t["name"] == "createSingleWorkout")["inputSchema"]
sch_rec = patched["createSingleWorkout"]

validate(sch_min, course_doc); validate(sch_rec, course_doc)
print("[语义] 合法课表（含一组间歇）在 minimal/recommended 下都校验通过 ✓")

# 成员字段校验能力保留：sets 成员里字段类型写错，两个版本都必须拒
bad_member = copy.deepcopy(course_doc)
bad_member["course"]["sections"][1]["sets"][0]["sectionType"] = "oops"
for name, sch in (("minimal", sch_min), ("recommended", sch_rec)):
    try:
        validate(sch, bad_member)
        print(f"[语义] 异常：{name} 接受了类型错误的 sets 成员！")
    except jsonschema.ValidationError:
        print(f"[语义] {name} 正确拒绝类型错误的 sets 成员 ✓")

# 结构差异（刻意为之）：recommended 的 PlainSection 不再向模型/校验器声明 sets，
# 间歇组嵌套从“可表达”变为“不广告”；因 Section 本无 additionalProperties:false，
# 嵌套 sets 在两种版本的服务端校验下都仍会被当作未知字段放行——与原行为一致，未加严。
assert "sets" not in sch_rec["$defs"]["PlainSection"]["properties"]
assert "sets" in sch_rec["$defs"]["Section"]["properties"]
print("[结构] recommended: Section.sets.items -> PlainSection（无 sets 属性），无引用环 ✓")
print("全部完成。文件在", OUT)
