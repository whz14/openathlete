import hashlib
import multiprocessing
import os
from datetime import date

import pytest

import openathlete.memory as memory
from openathlete.memory import (
    MAX_MEMORY_BYTES,
    MemoryError,
    get_current_plan,
    list_memory,
    parse_next_race,
    read_memory,
    write_memory,
)


FIXED_CONTENT = {
    "athlete": "# 画像\n\n## 能力基线\n",
    "injuries": "# 伤病\n\n## 当前未愈\n\n## 历史伤病\n",
    "races": "# 赛事\n\n## 目标赛事\n无\n\n## 历史赛事\n",
    "coach-user": (
        "# 教练偏好\n\n## 角色定位\n\n## 训练哲学\n\n"
        "## 说话方式\n\n## 建议偏好\n"
    ),
}


def plan_content(title="冬训", status="in_progress", start="2026-10-05",
                 end="2026-11-30", source="coach", body="# 阶段目标\n\n每周框架\n"):
    return (
        "+++\n"
        'title = "{}"\n'
        'status = "{}"\n'
        "start_date = {}\n"
        "end_date = {}\n"
        'source = "{}"\n'
        "+++\n\n{}"
    ).format(title, status, start, end, source, body)


def race_content(title="东京马拉松", status="completed", race_date="2026-12-06"):
    return (
        "+++\n"
        'title = "{}"\n'
        "date = {}\n"
        'status = "{}"\n'
        "+++\n\n# 复盘\n"
    ).format(title, race_date, status)


def injury_content(title="左膝", status="active", start="2026-10-01"):
    return (
        "+++\n"
        'title = "{}"\n'
        'status = "{}"\n'
        "start_date = {}\n"
        "+++\n\n# 病程\n"
    ).format(title, status, start)


def put_raw(home, relative, content):
    path = home / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        content = content.encode("utf-8")
    path.write_bytes(content)
    return path


def assert_code(code, callable_, *args, **kwargs):
    with pytest.raises(MemoryError) as caught:
        callable_(*args, **kwargs)
    assert caught.value.code == code
    return caught.value


def concurrent_writer(home, name, content, expected, start, results):
    try:
        from openathlete.memory import MemoryError, write_memory
        start.wait(10)
        try:
            result = write_memory(home, name, content, expected_sha256=expected)
            results.put(("ok", result["sha256"]))
        except MemoryError as exc:
            results.put(("memory-error", exc.code))
        except Exception as exc:
            results.put(("unexpected", repr(exc)))
    except Exception as exc:
        results.put(("setup-error", repr(exc)))


@pytest.mark.parametrize("name,relative,invalid", [
    ("athlete", "memory/athlete.md", "# 画像\n"),
    ("injuries", "memory/injuries.md", "# 当前未愈\n"),
    ("races", "memory/races.md", "## 目标赛事\n"),
    ("coach-user", "coach-user.md", "## 角色定位\n## 训练哲学\n"),
])
def test_fixed_templates_and_paths(tmp_path, name, relative, invalid):
    assert_code("MEMORY_TEMPLATE_INVALID", write_memory,
                tmp_path, name, invalid, allow_create=True)
    result = write_memory(tmp_path, name, FIXED_CONTENT[name], allow_create=True)
    assert result["created"] is True
    assert result["bytes"] == len(FIXED_CONTENT[name].encode("utf-8"))
    assert (tmp_path / relative).is_file()
    loaded = read_memory(tmp_path, name)
    assert loaded["content"] == FIXED_CONTENT[name]
    assert loaded["sha256"] == result["sha256"]
    assert loaded["modified_ts"] is not None


@pytest.mark.parametrize("name,relative,content", [
    ("plan:2026-11-02-冬训_1", "memory/plans/2026-11-02-冬训_1.md",
     plan_content(start="2026-11-02", end="2027-01-24")),
    ("log:2026-10-24", "memory/log/2026-10/2026-10-24.md", "# 2026-10-24 训练\n"),
    ("summary:2026-10", "memory/summary/2026-10.md", "# 2026-10 月度概述\n"),
    ("injury:2026-10-左膝_旧伤", "memory/injuries/2026-10-左膝_旧伤.md", injury_content()),
    ("race:2026-12-06-东京_M-1", "memory/races/2026-12-06-东京_M-1.md", race_content()),
    ("archive:多年病史_甲", "memory/archive/多年病史_甲.md", "# 多年病史\n\n原文\n"),
])
def test_every_name_mapping_roundtrip(tmp_path, name, relative, content):
    written = write_memory(tmp_path, name, content, allow_create=True)
    assert written["created"] is True
    assert written["sha256"] == hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert (tmp_path / relative).read_bytes() == content.encode("utf-8")
    loaded = read_memory(tmp_path, name)
    assert loaded["name"] == name
    assert loaded["exists"] is True
    assert loaded["content"] == content
    assert loaded["bytes"] == len(content.encode("utf-8"))


@pytest.mark.parametrize("name", [
    None, "coach-core", "../athlete", "C:\\outside.md", "archive:../x",
    "archive:a/b", "archive:a\x00b", "archive:CON", "archive:点.txt",
    "plan:2026-02-30-x",
])
def test_invalid_names_are_rejected(tmp_path, name):
    assert_code("MEMORY_PATH_INVALID", read_memory, tmp_path, name)


def test_home_is_explicit_and_never_uses_cwd(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert_code("INVALID_ARGUMENT", read_memory, "relative-home", "athlete")
    missing = read_memory(tmp_path / "actual-home", "race:2026-12-06-东京")
    assert missing == {
        "name": "race:2026-12-06-东京", "exists": False, "content": None,
        "sha256": None, "bytes": 0, "modified_ts": None,
    }
    assert not elsewhere.joinpath("memory").exists()


def test_create_update_hash_checks_and_lf(tmp_path):
    name = "log:2026-10-24"
    assert_code("NOT_FOUND", write_memory, tmp_path, name, "# 2026-10-24\n")
    assert_code("MEMORY_CONFLICT", write_memory, tmp_path, name, "# 2026-10-24\n",
                expected_sha256="0" * 64, allow_create=True)
    created = write_memory(tmp_path, name, "# 2026-10-24\r\n\r\n甲\r乙\n",
                           allow_create=True)
    raw = (tmp_path / "memory/log/2026-10/2026-10-24.md").read_bytes()
    assert b"\r" not in raw
    assert created["sha256"] == hashlib.sha256(raw).hexdigest()
    assert_code("INVALID_ARGUMENT", write_memory, tmp_path, name, "# 2026-10-24\n")
    assert_code("INVALID_ARGUMENT", write_memory, tmp_path, name, "# 2026-10-24\n",
                expected_sha256="bad")
    assert_code("MEMORY_CONFLICT", write_memory, tmp_path, name, "# 2026-10-24\n",
                expected_sha256="0" * 64)
    updated = write_memory(tmp_path, name, "# 2026-10-24\n新内容\n",
                           expected_sha256=created["sha256"])
    assert updated["created"] is False
    assert read_memory(tmp_path, name)["sha256"] == updated["sha256"]


def test_size_boundaries_utf8_and_lf(tmp_path):
    prefix = "# 原文\n"
    exact = prefix + "a" * (MAX_MEMORY_BYTES - len(prefix.encode("utf-8")))
    result = write_memory(tmp_path, "archive:上限", exact, allow_create=True)
    assert result["bytes"] == MAX_MEMORY_BYTES
    assert read_memory(tmp_path, "archive:上限")["bytes"] == MAX_MEMORY_BYTES
    assert_code("MEMORY_TOO_LARGE", write_memory, tmp_path, "archive:过大",
                exact + "b", allow_create=True)
    put_raw(tmp_path, "memory/archive/超大.md", b"x" * (MAX_MEMORY_BYTES + 1))
    assert_code("MEMORY_TOO_LARGE", read_memory, tmp_path, "archive:超大")
    put_raw(tmp_path, "memory/archive/坏编码.md", b"# title\n\xff")
    error = assert_code("MEMORY_READ_FAILED", read_memory, tmp_path, "archive:坏编码")
    assert error.details["reason"] == "invalid_utf8"
    assert_code("INVALID_ARGUMENT", write_memory, tmp_path, "archive:代理",
                "# title\n\ud800", allow_create=True)


def test_atomic_replace_failure_preserves_original_and_cleans_temp(tmp_path, monkeypatch):
    name = "log:2026-10-24"
    original = "# 2026-10-24\n旧内容\n"
    created = write_memory(tmp_path, name, original, allow_create=True)
    target = tmp_path / "memory/log/2026-10/2026-10-24.md"

    def fail_replace(*_):
        raise OSError("injected")

    monkeypatch.setattr(memory.os, "replace", fail_replace)
    assert_code("MEMORY_WRITE_FAILED", write_memory, tmp_path, name,
                "# 2026-10-24\n新内容\n", expected_sha256=created["sha256"])
    assert target.read_bytes() == original.encode("utf-8")
    assert list(target.parent.glob(".memory-*.tmp")) == []
    assert (tmp_path / "locks/memory.lock").is_file()


def make_list_fixtures(home):
    write_memory(home, "plan:2026-10-05-冬训", plan_content(), allow_create=True)
    write_memory(home, "race:2026-12-06-东京", race_content(), allow_create=True)
    write_memory(home, "injury:2026-10-左膝", injury_content(), allow_create=True)
    write_memory(home, "archive:原文", "# 原文\n", allow_create=True)
    write_memory(home, "log:2026-10-01", "# 2026-10-01\n", allow_create=True)
    write_memory(home, "log:2026-10-31", "# 2026-10-31\n", allow_create=True)
    write_memory(home, "log:2026-11-01", "# 2026-11-01\n", allow_create=True)
    write_memory(home, "summary:2026-10", "# 2026-10\n", allow_create=True)


def test_every_list_mode(tmp_path):
    make_list_fixtures(tmp_path)
    modes = [
        ("plans", {"plan:2026-10-05-冬训"}),
        ("races", {"race:2026-12-06-东京"}),
        ("injuries", {"injury:2026-10-左膝"}),
        ("archive", {"archive:原文"}),
        ("log:2026-10", {"log:2026-10-01", "log:2026-10-31"}),
        ("summary", {"summary:2026-10"}),
    ]
    for list_name, expected in modes:
        result = list_memory(tmp_path, list_name)
        assert result["exists"] is True
        assert {entry["name"] for entry in result["entries"]} == expected
        for entry in result["entries"]:
            loaded = read_memory(tmp_path, entry["name"])
            assert entry["sha256"] == loaded["sha256"]
            assert entry["bytes"] == loaded["bytes"]
            assert entry["modified_ts"] == loaded["modified_ts"]


def test_list_empty_invalid_entries_and_no_recursion(tmp_path):
    empty = list_memory(tmp_path, "plans")
    assert empty["exists"] is False
    assert empty["entries"] == []
    make_list_fixtures(tmp_path)
    put_raw(tmp_path, "memory/plans/notes.md", "# not a plan\n")
    put_raw(tmp_path, "memory/plans/2026-02-30-坏日期.md", "# invalid\n")
    put_raw(tmp_path, "memory/plans/ignore.txt", "text")
    put_raw(tmp_path, "memory/plans/nested/2026-10-01-嵌套.md", "# nested\n")
    result = list_memory(tmp_path, "plans")
    assert [entry["name"] for entry in result["entries"]] == ["plan:2026-10-05-冬训"]
    assert len(result["warnings"]) == 2
    assert all("content" not in entry for entry in result["entries"])


@pytest.mark.parametrize("list_name", [None, "athlete", "plans/..", "log:2026-13"])
def test_invalid_lists_are_rejected(tmp_path, list_name):
    assert_code("MEMORY_PATH_INVALID", list_memory, tmp_path, list_name)


@pytest.mark.parametrize("kind,name,valid,required", [
    ("plan", "plan:2026-10-05-冬训", plan_content(),
     ("title", "status", "start_date", "end_date", "source")),
    ("race", "race:2026-12-06-东京", race_content(), ("title", "date", "status")),
    ("injury", "injury:2026-10-左膝", injury_content(), ("title", "status", "start_date")),
])
def test_new_front_matter_required_fields(tmp_path, kind, name, valid, required):
    for key in required:
        lines = [line for line in valid.splitlines()
                 if not line.startswith(key + " =")]
        invalid = "\n".join(lines) + "\n"
        assert_code("MEMORY_TEMPLATE_INVALID", write_memory,
                    tmp_path, name, invalid, allow_create=True)
    assert not read_memory(tmp_path, name)["exists"]


@pytest.mark.parametrize("name,content", [
    ("plan:2026-10-05-冬训", plan_content(status="paused")),
    ("plan:2026-10-05-冬训", plan_content(start="2026-10-06")),
    ("plan:2026-10-05-冬训", plan_content(start="2026-10-05", end="2026-10-04")),
    ("plan:2026-10-05-冬训", plan_content().replace("start_date = 2026-10-05",
                                                   'start_date = "2026-10-05"')),
    ("plan:2026-10-05-冬训", "# 纯 Markdown 旧计划\n"),
    ("race:2026-12-06-东京", race_content(race_date="2026-12-07")),
    ("injury:2026-10-左膝", injury_content(status="healed")),
])
def test_new_front_matter_enums_dates_and_presence(tmp_path, name, content):
    assert_code("MEMORY_TEMPLATE_INVALID", write_memory,
                tmp_path, name, content, allow_create=True)


@pytest.mark.parametrize("name,content", [
    ("log:2026-10-24", "# 2026-10-23\n"),
    ("summary:2026-10", "## 2026-10\n"),
    ("archive:原文", "只有正文\n"),
])
def test_log_summary_archive_templates(tmp_path, name, content):
    assert_code("MEMORY_TEMPLATE_INVALID", write_memory,
                tmp_path, name, content, allow_create=True)


@pytest.mark.parametrize("name,relative,legacy,updated", [
    ("race:2026-12-06-东京", "memory/races/2026-12-06-东京.md",
     "# 东京复盘\n\n旧格式。\n", "# 东京复盘\n\n直接续写，不迁移。\n"),
    ("injury:2026-10-左膝", "memory/injuries/2026-10-左膝.md",
     "# 左膝病程\n\n旧格式。\n", "# 左膝病程\n\n直接续写，不迁移。\n"),
    ("plan:2020-01-01-旧计划", "memory/plans/2020-01-01-旧计划.md",
     "# 旧计划\n\n没有元数据。\n", "# 旧计划\n\n仍按原文续写。\n"),
])
def test_legacy_files_read_and_overwrite_without_migration(tmp_path, name, relative,
                                                            legacy, updated):
    put_raw(tmp_path, relative, legacy)
    before = read_memory(tmp_path, name)
    assert before["content"] == legacy
    result = write_memory(tmp_path, name, updated, expected_sha256=before["sha256"])
    assert result["created"] is False
    assert read_memory(tmp_path, name)["content"] == updated


def test_explicit_legacy_migration_is_validated(tmp_path):
    name = "injury:2026-10-左膝"
    put_raw(tmp_path, "memory/injuries/2026-10-左膝.md", "# 旧病程\n")
    before = read_memory(tmp_path, name)
    assert_code("MEMORY_TEMPLATE_INVALID", write_memory, tmp_path, name,
                "+++\ntitle = \"左膝\"\n+++\n", expected_sha256=before["sha256"])
    write_memory(tmp_path, name, injury_content(status="recovered"),
                 expected_sha256=before["sha256"])
    assert_code("MEMORY_TEMPLATE_INVALID", write_memory, tmp_path, name,
                "# 再删掉元数据\n", expected_sha256=read_memory(tmp_path, name)["sha256"])


def test_two_processes_with_same_expected_hash_only_one_wins(tmp_path):
    name = "log:2026-10-24"
    created = write_memory(tmp_path, name, "# 2026-10-24\n初始\n", allow_create=True)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    writers = [
        context.Process(target=concurrent_writer,
                        args=(str(tmp_path), name, "# 2026-10-24\n进程甲\n",
                              created["sha256"], start, results)),
        context.Process(target=concurrent_writer,
                        args=(str(tmp_path), name, "# 2026-10-24\n进程乙\n",
                              created["sha256"], start, results)),
    ]
    for process in writers:
        process.start()
    start.set()
    for process in writers:
        process.join(20)
        assert not process.is_alive()
        assert process.exitcode == 0
    outcomes = [results.get(timeout=2), results.get(timeout=2)]
    assert sum(item[0] == "ok" for item in outcomes) == 1
    assert ("memory-error", "MEMORY_CONFLICT") in outcomes, outcomes
    assert read_memory(tmp_path, name)["content"] in {
        "# 2026-10-24\n进程甲\n", "# 2026-10-24\n进程乙\n",
    }
    assert (tmp_path / "locks/memory.lock").exists()


def make_link(link, target, directory=True):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        if os.name != "nt" or not directory:
            pytest.skip("当前平台不允许创建链接用于测试：{}".format(exc))
        import subprocess
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
        )
        if result.returncode:
            pytest.skip("当前平台不允许创建 junction 用于测试")


def test_trusted_home_root_may_be_a_link(tmp_path):
    actual = tmp_path / "actual-home"
    created = write_memory(actual, "athlete", FIXED_CONTENT["athlete"], allow_create=True)
    link = tmp_path / "configured-home-link"
    make_link(link, actual)
    assert read_memory(link, "athlete")["sha256"] == created["sha256"]
    write_memory(link, "athlete", FIXED_CONTENT["athlete"] + "\n更新\n",
                 expected_sha256=created["sha256"])
    assert "更新" in read_memory(actual, "athlete")["content"]


def test_links_below_home_are_rejected_for_read_list_and_write(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    put_raw(outside, "memory/athlete.md", FIXED_CONTENT["athlete"])
    make_link(home / "memory", outside / "memory")
    assert_code("MEMORY_PATH_INVALID", read_memory, home, "athlete")
    assert_code("MEMORY_PATH_INVALID", list_memory, home, "plans")
    assert_code("MEMORY_PATH_INVALID", write_memory, home, "athlete",
                FIXED_CONTENT["athlete"], allow_create=True)


def test_list_skips_a_linked_markdown_entry(tmp_path):
    home = tmp_path / "home"
    target = put_raw(home, "memory/plans/2026-10-05-正常.md", plan_content())
    linked = home / "memory/plans/2026-10-06-链接.md"
    if os.name == "nt":
        outside = tmp_path / "outside-target"
        outside.mkdir()
        make_link(linked, outside, directory=True)
    else:
        outside = put_raw(tmp_path, "outside.md", plan_content(title="外部"))
        make_link(linked, outside, directory=False)
    direct = "plan:2026-10-06-链接"
    assert_code("MEMORY_PATH_INVALID", read_memory, home, direct)
    listed = list_memory(home, "plans")
    assert [entry["name"] for entry in listed["entries"]] == ["plan:2026-10-05-正常"]
    assert len(listed["warnings"]) == 1
    assert target.is_file()


def test_parse_next_race_order_placeholders_and_warnings(tmp_path):
    assert parse_next_race(tmp_path, "2026-10-24") == (None, [])
    write_memory(tmp_path, "races", FIXED_CONTENT["races"], allow_create=True)
    assert parse_next_race(tmp_path, "2026-10-24") == (None, [])
    past = FIXED_CONTENT["races"].replace(
        "无", "- 日期：2026-10-23；赛事：昨天")
    before = read_memory(tmp_path, "races")
    write_memory(tmp_path, "races", past, expected_sha256=before["sha256"])
    assert parse_next_race(tmp_path, "2026-10-24") == (None, [])
    assert_code("INVALID_ARGUMENT", parse_next_race, tmp_path, "2026-02-30")

    content = """# 赛事

## 目标赛事
无
- 日期：2026-10-20；赛事：过去赛事
- 日期：2026-11-01；赛事：较晚赛事
- 日期：2026-10-30；赛事：同日第一场；备注
- 日期：2026-10-30；赛事：同日第二场
- 日期：2026-02-30；赛事：坏日期
- 日期 2026-12-01；赛事：坏格式
- 日期：2026-12-01；赛事：；空名称
普通文字 2026-10-25 不猜日期
  - 日期：2026-10-25；赛事：缩进项不解析

## 历史赛事
- 日期：2026-10-25；赛事：历史小节不参与
"""
    ordered_home = tmp_path / "ordered"
    write_memory(ordered_home, "races", content, allow_create=True)
    value, warnings = parse_next_race(ordered_home, date(2026, 10, 24))
    assert value == {"date": "2026-10-30", "name": "同日第一场", "days_until": 6}
    assert len(warnings) == 3
    assert all(item["code"] == "RACE_ENTRY_INVALID" for item in warnings)


def test_current_plan_selection_metadata_and_natural_week(tmp_path):
    active_name = "plan:2026-10-05-冬训"
    front_matter = (
        "+++\n"
        'title = "冬训"\n'
        'status = "in_progress"\n'
        "start_date = 2026-10-05\n"
        "end_date = 2026-11-30\n"
        'source = "ai"\n'
        "+++\n"
    )
    body = "\n\n# 阶段目标\n\n第一周  \n- 保留原有空白\n\n"
    put_raw(tmp_path, "memory/plans/2026-10-05-冬训.md", front_matter + body)
    write_memory(tmp_path, "plan:2026-09-01-已完成",
                 plan_content(title="已完成", status="completed", start="2026-09-01",
                              end="2026-12-01"), allow_create=True)
    write_memory(tmp_path, "plan:2026-11-01-未来",
                 plan_content(title="未来", start="2026-11-01", end="2026-11-30"),
                 allow_create=True)
    put_raw(tmp_path, "memory/plans/2020-01-01-旧计划.md", "# 旧计划\n不猜状态\n")
    for day, text in [
        ("2026-10-18", "上周日"), ("2026-10-19", "周一"),
        ("2026-10-25", "周日"), ("2026-10-26", "下周一"),
    ]:
        write_memory(tmp_path, "log:" + day, "# {}\n{}\n".format(day, text),
                     allow_create=True)
    put_raw(tmp_path, "config.toml", 'timezone = "Pacific/Kiritimati"\n')
    result = get_current_plan(tmp_path, "2026-10-21")
    assert result["plan"]["name"] == active_name
    assert result["plan"]["title"] == "冬训"
    assert result["plan"]["status"] == "in_progress"
    assert result["plan"]["source"] == "ai"
    assert result["plan"]["start_date"] == "2026-10-05"
    assert result["plan"]["end_date"] == "2026-11-30"
    assert result["plan"]["content"] == body
    assert list(result["week_logs"]) == [
        "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22",
        "2026-10-23", "2026-10-24", "2026-10-25",
    ]
    assert result["week_logs"]["2026-10-19"] == "# 2026-10-19\n周一\n"
    assert result["week_logs"]["2026-10-25"] == "# 2026-10-25\n周日\n"
    assert all(result["week_logs"][day] is None for day in (
        "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-24",
    ))
    assert result["platform_schedule"] == {"available": False, "reason": "not_in_v0"}
    assert [item["code"] for item in result["warnings"]] == ["PLAN_METADATA_INVALID"]


def test_current_plan_no_match_and_bad_metadata_warn_without_guessing(tmp_path):
    put_raw(tmp_path, "memory/plans/2020-01-01-旧计划.md", "# 无元数据\n")
    put_raw(tmp_path, "memory/plans/2026-10-01-坏计划.md",
            plan_content(start="2026-10-01", end="2020-01-01"))
    result = get_current_plan(tmp_path, date(2026, 10, 24))
    assert result["plan"] is None
    assert len(result["week_logs"]) == 7
    assert all(content is None for content in result["week_logs"].values())
    assert len(result["warnings"]) == 2
    assert all(item["code"] == "PLAN_METADATA_INVALID" for item in result["warnings"])


def test_current_plan_conflict(tmp_path):
    write_memory(tmp_path, "plan:2026-10-01-甲",
                 plan_content(title="甲", start="2026-10-01"), allow_create=True)
    write_memory(tmp_path, "plan:2026-10-02-乙",
                 plan_content(title="乙", start="2026-10-02"), allow_create=True)
    error = assert_code("CURRENT_PLAN_CONFLICT", get_current_plan,
                        tmp_path, "2026-10-24")
    assert set(error.details["names"]) == {"plan:2026-10-01-甲", "plan:2026-10-02-乙"}
    assert_code("INVALID_ARGUMENT", get_current_plan, tmp_path, "not-a-date")
