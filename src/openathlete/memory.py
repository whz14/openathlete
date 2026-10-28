"""白名单 Markdown 记忆、原子写入与本地训练计划查询。"""
import hashlib
import os
import re
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import date as date_type
from datetime import datetime, timedelta
from pathlib import Path

MAX_MEMORY_BYTES = 1024 * 1024
_LOCK_TIMEOUT_S = 10.0
_LOCK_RETRY_S = 0.05

_FIXED_PATHS = {
    "athlete": ("athlete", Path("memory/athlete.md")),
    "injuries": ("injuries", Path("memory/injuries.md")),
    "races": ("races", Path("memory/races.md")),
    "coach-user": ("coach-user", Path("coach-user.md")),
}
_REQUIRED_SECTIONS = {
    "athlete": ("画像", "能力基线"),
    "injuries": ("当前未愈", "历史伤病"),
    "races": ("目标赛事", "历史赛事"),
    "coach-user": ("角色定位", "训练哲学", "说话方式", "建议偏好"),
}
_LIST_PATHS = {
    "plans": ("plan:", Path("memory/plans")),
    "races": ("race:", Path("memory/races")),
    "injuries": ("injury:", Path("memory/injuries")),
    "archive": ("archive:", Path("memory/archive")),
    "summary": ("summary:", Path("memory/summary")),
}
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *("COM{}".format(i) for i in range(1, 10)),
    *("LPT{}".format(i) for i in range(1, 10)),
    *(prefix + suffix for prefix in ("COM", "LPT") for suffix in "¹²³"),
}
_THREAD_LOCKS = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class MemoryError(RuntimeError):
    def __init__(self, code, message, retryable=False, next_action="", details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.next_action = next_action
        self.details = {} if details is None else details


class _Name:
    def __init__(self, kind, relative, date_text=None):
        self.kind = kind
        self.relative = relative
        self.date_text = date_text


def _fail(code, message, retryable=False, next_action="", details=None):
    raise MemoryError(code, message, retryable, next_action, details)


def _path_invalid(message="记忆名称或路径不在白名单内"):
    _fail("MEMORY_PATH_INVALID", message,
          next_action="使用受支持的 memory 名称，不提供任意路径")


def _home_path(home):
    if home is None:
        _fail("INVALID_ARGUMENT", "home 为必填参数")
    try:
        root = Path(home).expanduser()
    except (TypeError, ValueError):
        _fail("INVALID_ARGUMENT", "home 必须是有效的绝对路径")
    if not root.is_absolute():
        _fail("INVALID_ARGUMENT", "home 必须是绝对路径，不能依赖当前工作目录")
    try:
        return root.resolve(strict=False)
    except OSError:
        _path_invalid("无法解析 HOME 根路径")


def _real_date(text):
    if not isinstance(text, str) or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
        _path_invalid("日期必须使用真实的 YYYY-MM-DD 格式")
    try:
        value = date_type.fromisoformat(text)
    except ValueError:
        _path_invalid("日期必须使用真实的 YYYY-MM-DD 格式")
    if value.isoformat() != text:
        _path_invalid("日期必须使用真实的 YYYY-MM-DD 格式")
    return text


def _real_month(text):
    if not isinstance(text, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}", text):
        _path_invalid("月份必须使用真实的 YYYY-MM 格式")
    try:
        date_type(int(text[:4]), int(text[5:]), 1)
    except ValueError:
        _path_invalid("月份必须使用真实的 YYYY-MM 格式")
    return text


def _slug(text):
    if not text or any(char.isalnum() is False and char not in "_-" for char in text):
        _path_invalid("slug 只能包含 Unicode 字母、数字、下划线和连字符")
    if text.upper() in _WINDOWS_RESERVED:
        _path_invalid("不能使用 Windows 保留设备名")
    return text


def _parse_name(name):
    if not isinstance(name, str) or not name:
        _path_invalid()
    if name in _FIXED_PATHS:
        kind, relative = _FIXED_PATHS[name]
        return _Name(kind, relative)

    prefix, kind, directory, date_length, date_parser = None, None, None, None, None
    if name.startswith("plan:"):
        prefix, kind, directory = "plan:", "plan", "memory/plans"
        date_length, date_parser = 10, _real_date
    elif name.startswith("race:"):
        prefix, kind, directory = "race:", "race", "memory/races"
        date_length, date_parser = 10, _real_date
    elif name.startswith("injury:"):
        prefix, kind, directory = "injury:", "injury", "memory/injuries"
        date_length, date_parser = 7, _real_month
    elif name.startswith("log:"):
        text = name[4:]
        _real_date(text) if len(text) == 10 else _path_invalid()
        return _Name("log", Path("memory/log") / text[:7] / (text + ".md"), text)
    elif name.startswith("summary:"):
        text = name[8:]
        _real_month(text) if len(text) == 7 else _path_invalid()
        return _Name("summary", Path("memory/summary") / (text + ".md"), text)
    elif name.startswith("archive:"):
        _slug(name[8:])
        return _Name("archive", Path("memory/archive") / (name[8:] + ".md"))
    else:
        _path_invalid()

    rest = name[len(prefix):]
    if len(rest) <= date_length or rest[date_length] != "-":
        _path_invalid()
    date_text = date_parser(rest[:date_length])
    slug = _slug(rest[date_length + 1:])
    filename = "{}-{}.md".format(date_text, slug)
    return _Name(kind, Path(directory) / filename, date_text)


def _parse_list(list_name):
    if not isinstance(list_name, str) or not list_name:
        _path_invalid("list 不在白名单内")
    if list_name in _LIST_PATHS:
        prefix, relative = _LIST_PATHS[list_name]
        return prefix, relative
    if list_name.startswith("log:"):
        month = list_name[4:]
        if len(month) == 7:
            _real_month(month)
            return "log:", Path("memory/log") / month
    _path_invalid("list 只支持 plans/races/injuries/archive/log:YYYY-MM/summary")


def _is_link_or_reparse(info):
    if stat.S_ISLNK(info.st_mode):
        return True
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(info, "st_file_attributes", 0) & reparse)


def _check_components(root, relative, expect_dir):
    current = root
    parts = relative.parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return False
        except (OSError, ValueError):
            _path_invalid("无法安全检查记忆路径组件")
        if _is_link_or_reparse(info):
            _path_invalid("HOME 根以下不允许符号链接或 reparse point")
        last = index == len(parts) - 1
        if last and not expect_dir:
            if not stat.S_ISREG(info.st_mode):
                _path_invalid("记忆目标必须是普通文件")
        elif not stat.S_ISDIR(info.st_mode):
            _path_invalid("记忆路径的父组件必须是目录")
    return True


def _verify_resolved(root, candidate):
    try:
        resolved = candidate.resolve(strict=False)
        parent = candidate.parent.resolve(strict=False)
        root_resolved = root.resolve(strict=False)
        if resolved != parent / candidate.name:
            _path_invalid("记忆文件必须是指定目录的直接子文件")
        common = os.path.commonpath((str(root_resolved), str(resolved)))
        if os.path.normcase(common) != os.path.normcase(str(root_resolved)):
            _path_invalid("记忆路径不能逃逸 HOME")
    except MemoryError:
        raise
    except (OSError, RuntimeError, ValueError):
        _path_invalid("无法复核记忆路径边界")


def _secure_path(root, relative, expect_dir=False):
    candidate = root / relative
    if candidate.parent != root / relative.parent:
        _path_invalid()
    exists = _check_components(root, relative, expect_dir)
    _verify_resolved(root, candidate)
    return candidate, exists


def _ensure_directory(root, relative):
    if relative == Path("."):
        return root
    current = root
    for part in relative.parts:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            try:
                current.mkdir()
            except FileExistsError:
                pass
            except OSError as exc:
                raise MemoryError("MEMORY_WRITE_FAILED", "无法创建记忆目录", True,
                                  "检查 HOME 权限和磁盘状态后重试") from exc
            try:
                info = os.lstat(current)
            except OSError as exc:
                raise MemoryError("MEMORY_WRITE_FAILED", "无法检查新建记忆目录", True,
                                  "检查 HOME 权限后重试") from exc
        except OSError as exc:
            raise MemoryError("MEMORY_WRITE_FAILED", "无法检查记忆目录", True,
                              "检查 HOME 权限后重试") from exc
        if _is_link_or_reparse(info):
            _path_invalid("HOME 根以下不允许符号链接或 reparse point")
        if not stat.S_ISDIR(info.st_mode):
            _path_invalid("记忆路径的父组件必须是目录")
    path, _ = _secure_path(root, relative, True)
    return path


def _too_large(actual):
    _fail("MEMORY_TOO_LARGE", "记忆文件超过 1 MiB 上限",
          next_action="缩减内容或拆分档案后再试",
          details={"max_bytes": MAX_MEMORY_BYTES, "actual_bytes": actual})


def _read_snapshot(root, spec):
    path, exists = _secure_path(root, spec.relative)
    if not exists:
        return None
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise MemoryError("MEMORY_READ_FAILED", "无法打开记忆文件", True,
                          "检查文件权限或链接状态后重试") from exc
    try:
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                _path_invalid("记忆目标必须是普通文件")
            if info.st_size > MAX_MEMORY_BYTES:
                _too_large(info.st_size)
            raw = handle.read(MAX_MEMORY_BYTES + 1)
            if len(raw) > MAX_MEMORY_BYTES:
                _too_large(len(raw))
            info = os.fstat(handle.fileno())
    except MemoryError:
        raise
    except OSError as exc:
        raise MemoryError("MEMORY_READ_FAILED", "无法读取记忆文件", True,
                          "检查文件状态后重试") from exc
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MemoryError("MEMORY_READ_FAILED", "记忆文件不是有效 UTF-8", False,
                          "将文件转换为 UTF-8 后再读取",
                          {"reason": "invalid_utf8"}) from exc
    return {
        "content": content,
        "raw": raw,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "modified_ts": int(info.st_mtime),
    }


def _read_result(name, snapshot):
    if snapshot is None:
        return {"name": name, "exists": False, "content": None,
                "sha256": None, "bytes": 0, "modified_ts": None}
    return {
        "name": name,
        "exists": True,
        "content": snapshot["content"],
        "sha256": snapshot["sha256"],
        "bytes": len(snapshot["raw"]),
        "modified_ts": snapshot["modified_ts"],
    }


def read_memory(home, name):
    """按固定 name 读取一个文件；缺失文件成功返回 exists=false。"""
    spec = _parse_name(name)
    root = _home_path(home)
    return _read_result(name, _read_snapshot(root, spec))


def _entry_warning():
    return {"code": "MEMORY_ENTRY_SKIPPED",
            "message": "已跳过不符合名称白名单或链接安全要求的文件"}


def list_memory(home, list_name):
    """枚举白名单目录的直接 Markdown 子文件，不递归。"""
    prefix, relative = _parse_list(list_name)
    root = _home_path(home)
    directory, exists = _secure_path(root, relative, True)
    if not exists:
        return {"list": list_name, "exists": False, "entries": [], "warnings": []}
    try:
        with os.scandir(directory) as iterator:
            filenames = sorted(entry.name for entry in iterator if entry.name.endswith(".md"))
    except OSError as exc:
        raise MemoryError("MEMORY_READ_FAILED", "无法枚举记忆目录", True,
                          "检查目录权限后重试") from exc
    entries = []
    warnings = []
    for filename in filenames:
        name = prefix + filename[:-3]
        try:
            spec = _parse_name(name)
            if spec.relative.parent != relative or spec.relative.name != filename:
                _path_invalid()
            result = read_memory(root, name)
        except MemoryError as exc:
            if exc.code == "MEMORY_PATH_INVALID":
                warnings.append(_entry_warning())
                continue
            raise
        if result["exists"]:
            entries.append({key: result[key]
                            for key in ("name", "sha256", "bytes", "modified_ts")})
    return {"list": list_name, "exists": True, "entries": entries, "warnings": warnings}


def _thread_lock(root):
    key = str(root)
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.Lock())


def _try_file_lock(fd):
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(fd):
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def _memory_lock(root):
    with _thread_lock(root):
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MemoryError("MEMORY_WRITE_FAILED", "无法创建 HOME 根目录", True,
                              "检查 HOME 权限后重试") from exc
        _ensure_directory(root, Path("locks"))
        lock_path, _ = _secure_path(root, Path("locks/memory.lock"))
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise MemoryError("MEMORY_WRITE_FAILED", "无法打开 memory 锁文件", True,
                              "检查 HOME 权限和链接状态后重试") from exc
        locked = False
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                _path_invalid("memory 锁必须是普通文件")
            if info.st_size == 0:
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, b"\0")
                os.fsync(fd)
            deadline = time.monotonic() + _LOCK_TIMEOUT_S
            while True:
                try:
                    _try_file_lock(fd)
                    locked = True
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise MemoryError("MEMORY_BUSY", "另一进程正在写入记忆", True,
                                          "等待另一写入完成后重试") from exc
                    time.sleep(_LOCK_RETRY_S)
            yield
        finally:
            if locked:
                _unlock_file(fd)
            os.close(fd)


def _headings(content):
    headings = []
    for line in content.split("\n"):
        match = re.match(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", line)
        if match:
            title = re.sub(r"[ \t]+#+[ \t]*$", "", match.group(2)).strip()
            headings.append((len(match.group(1)), title))
    return headings


def _validate_sections(content, required):
    titles = {title for _, title in _headings(content)}
    missing = [title for title in required if title not in titles]
    if missing:
        raise ValueError("缺少必需节：{}".format("、".join(missing)))


def _validate_h1_date(content, expected):
    for level, title in _headings(content):
        if level == 1:
            token = title.split(None, 1)[0] if title else ""
            if token != expected:
                raise ValueError("H1 日期必须与 name 中的 {} 一致".format(expected))
            return
    raise ValueError("必须包含与 name 日期一致的 H1")


def _front_matter(content):
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "+++":
        return None
    closing = next((index for index in range(1, len(lines))
                    if lines[index].strip() == "+++"), None)
    if closing is None:
        raise ValueError("TOML front matter 缺少结束 +++")
    try:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        metadata = tomllib.loads("".join(lines[1:closing]))
    except Exception as exc:
        if exc.__class__.__name__ not in {"TOMLDecodeError", "TomliDecodeError"}:
            raise
        raise ValueError("TOML front matter 无法解析") from None
    return metadata, "".join(lines[closing + 1:])


def _native_date(value, label):
    if type(value) is not date_type:
        raise ValueError("{} 必须是 TOML 原生日期".format(label))
    return value


def _required_text(metadata, key):
    value = metadata.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("front matter 的 {} 必须是非空字符串".format(key))
    return value


def _plan_metadata(spec, metadata):
    result = dict(metadata)
    result["title"] = _required_text(metadata, "title")
    status = _required_text(metadata, "status")
    if status not in {"in_progress", "completed", "aborted"}:
        raise ValueError("plan.status 必须是 in_progress/completed/aborted")
    result["status"] = status
    source = _required_text(metadata, "source")
    if source not in {"coach", "ai", "platform"}:
        raise ValueError("plan.source 必须是 coach/ai/platform")
    result["source"] = source
    start = _native_date(metadata.get("start_date"), "plan.start_date")
    end = _native_date(metadata.get("end_date"), "plan.end_date")
    if start > end:
        raise ValueError("plan.start_date 不能晚于 end_date")
    if start.isoformat() != spec.date_text:
        raise ValueError("plan 文件名日期必须等于 start_date")
    result["start_date"] = start.isoformat()
    result["end_date"] = end.isoformat()
    return result


def _race_metadata(spec, metadata):
    result = dict(metadata)
    result["title"] = _required_text(metadata, "title")
    status = _required_text(metadata, "status")
    if status not in {"completed", "dnf", "dns"}:
        raise ValueError("race.status 必须是 completed/dnf/dns")
    race_date = _native_date(metadata.get("date"), "race.date")
    if race_date.isoformat() != spec.date_text:
        raise ValueError("race 文件名日期必须等于 date")
    result["date"] = race_date.isoformat()
    result["status"] = status
    return result


def _injury_metadata(metadata):
    result = dict(metadata)
    result["title"] = _required_text(metadata, "title")
    status = _required_text(metadata, "status")
    if status not in {"active", "recovered"}:
        raise ValueError("injury.status 必须是 active/recovered")
    result["status"] = status
    result["start_date"] = _native_date(
        metadata.get("start_date"), "injury.start_date").isoformat()
    return result


def _validate_template(spec, content, created, previous):
    if spec.kind in _REQUIRED_SECTIONS:
        _validate_sections(content, _REQUIRED_SECTIONS[spec.kind])
        return
    if spec.kind == "log" or spec.kind == "summary":
        _validate_h1_date(content, spec.date_text)
        return
    if spec.kind == "archive":
        if not content.strip() or not any(level == 1 and title.strip()
                                          for level, title in _headings(content)):
            raise ValueError("archive 必须是非空 Markdown 且包含非空 H1")
        return
    if spec.kind not in {"plan", "race", "injury"}:
        raise ValueError("不支持的记忆模板")

    parsed = _front_matter(content)
    previous_structured = bool(previous and previous.splitlines()
                               and previous.splitlines()[0].strip() == "+++")
    if parsed is None:
        if created or previous_structured:
            raise ValueError("新档案或显式迁移必须使用 +++ TOML front matter")
        return
    metadata, _ = parsed
    if spec.kind == "plan":
        _plan_metadata(spec, metadata)
    elif spec.kind == "race":
        _race_metadata(spec, metadata)
    else:
        _injury_metadata(metadata)


def _expected_hash(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        _fail("INVALID_ARGUMENT", "expected_sha256 必须是 64 位十六进制哈希")
    return value.lower()


def _conflict(name, expected, actual):
    _fail("MEMORY_CONFLICT", "记忆文件已被其他写入修改", False,
          "重新读取该文件，合并修改后再使用新哈希写入",
          {"name": name, "expected_sha256": expected, "actual_sha256": actual})


def _fsync_parent(path):
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def write_memory(home, name, content, expected_sha256=None, allow_create=False):
    """在跨进程 memory 锁内校验哈希与模板，并原子替换目标文件。"""
    spec = _parse_name(name)
    if not isinstance(content, str):
        _fail("INVALID_ARGUMENT", "content 必须是 UTF-8 Markdown 字符串")
    if not isinstance(allow_create, bool):
        _fail("INVALID_ARGUMENT", "allow_create 必须是布尔值")
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    try:
        raw = normalized.encode("utf-8")
    except UnicodeEncodeError:
        _fail("INVALID_ARGUMENT", "content 必须能编码为有效 UTF-8")
    if len(raw) > MAX_MEMORY_BYTES:
        _too_large(len(raw))
    expected = _expected_hash(expected_sha256) if expected_sha256 is not None else None
    root = _home_path(home)
    with _memory_lock(root):
        current = _read_snapshot(root, spec)
        if current is None:
            if expected is not None:
                _conflict(name, expected, None)
            if not allow_create:
                _fail("NOT_FOUND", "记忆文件不存在，且未允许新建", False,
                      "确认需要新建后以 allow_create=true 调用",
                      {"name": name})
            created = True
        else:
            created = False
            if expected is None:
                _fail("INVALID_ARGUMENT", "覆盖既有文件必须提供 expected_sha256", False,
                      "先读取文件并把返回的 sha256 作为 expected_sha256")
            if current["sha256"] != expected:
                _conflict(name, expected, current["sha256"])
        try:
            _validate_template(spec, normalized, created,
                               current["content"] if current else None)
        except ValueError as exc:
            raise MemoryError("MEMORY_TEMPLATE_INVALID", str(exc), False,
                              "按该记忆类型的模板修正内容后重试",
                              {"name": name}) from None

        parent = _ensure_directory(root, spec.relative.parent)
        path, _ = _secure_path(root, spec.relative)
        if path.parent != parent:
            _path_invalid()
        digest = hashlib.sha256(raw).hexdigest()
        fd = None
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".memory-", suffix=".tmp", dir=parent)
            with os.fdopen(fd, "wb") as handle:
                fd = None
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            latest = _read_snapshot(root, spec)
            latest_hash = latest["sha256"] if latest else None
            current_hash = current["sha256"] if current else None
            if latest_hash != current_hash:
                _conflict(name, expected, latest_hash)
            os.replace(temporary, path)
            temporary = None
            _fsync_parent(parent)
        except MemoryError:
            raise
        except OSError as exc:
            raise MemoryError("MEMORY_WRITE_FAILED", "记忆文件原子写入失败", True,
                              "检查磁盘空间与 HOME 权限后重试；原文件未被主动备份改写",
                              {"name": name}) from exc
        finally:
            if fd is not None:
                os.close(fd)
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        return {"name": name, "sha256": digest, "bytes": len(raw),
                "written_ts": int(time.time()), "created": created}


def _coerce_date(value, label):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date_type):
        return value
    if isinstance(value, str):
        try:
            if value != date_type.fromisoformat(value).isoformat():
                raise ValueError
            return date_type.fromisoformat(value)
        except ValueError:
            pass
    _fail("INVALID_ARGUMENT", "{} 必须是真实的 YYYY-MM-DD 日期".format(label))


def _race_warning(line):
    return {"code": "RACE_ENTRY_INVALID", "line": line,
            "message": "目标赛事列表项无法按 日期/赛事 格式解析，已忽略"}


def parse_next_race(home, today):
    """机械解析 races.md 的目标赛事小节，返回最近未过期赛事与警告。"""
    today_value = _coerce_date(today, "today")
    source = read_memory(home, "races")
    if not source["exists"]:
        return None, []
    lines = source["content"].split("\n")
    target = None
    for index, line in enumerate(lines):
        match = re.match(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", line)
        if match and re.sub(r"[ \t]+#+[ \t]*$", "", match.group(2)).strip() == "目标赛事":
            target = (index, len(match.group(1)))
            break
    if target is None:
        return None, []
    start, level = target
    end = len(lines)
    for index in range(start + 1, len(lines)):
        match = re.match(r"^(#{1,6})[ \t]+", lines[index])
        if match and len(match.group(1)) <= level:
            end = index
            break
    candidates = []
    warnings = []
    for index in range(start + 1, end):
        line = lines[index]
        if not line.startswith("- "):
            continue
        match = re.fullmatch(r"- 日期：([^；]+)；赛事：(.+)", line)
        if not match:
            warnings.append(_race_warning(index + 1))
            continue
        date_text, remainder = match.groups()
        race_name = remainder.split("；", 1)[0].strip()
        try:
            race_date = date_type.fromisoformat(date_text)
            if race_date.isoformat() != date_text:
                raise ValueError
        except ValueError:
            warnings.append(_race_warning(index + 1))
            continue
        if not race_name:
            warnings.append(_race_warning(index + 1))
            continue
        if race_date >= today_value:
            candidates.append((race_date, race_name))
    if not candidates:
        return None, warnings
    race_date, race_name = min(enumerate(candidates), key=lambda item: (item[1][0], item[0]))[1]
    return {"date": race_date.isoformat(), "name": race_name,
            "days_until": (race_date - today_value).days}, warnings


def _plan_warning(name):
    return {"code": "PLAN_METADATA_INVALID", "name": name,
            "message": "计划缺少有效元数据，未猜测状态或日期，也未参与当前计划判定"}


def _json_metadata(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date_type):
        return value.isoformat()
    if isinstance(value, list):
        return [_json_metadata(item) for item in value]
    if isinstance(value, tuple):
        return [_json_metadata(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_metadata(item) for key, item in value.items()}
    if hasattr(value, "isoformat") and callable(value.isoformat):
        return value.isoformat()
    return value


def get_current_plan(home, date):
    """返回唯一覆盖日期的进行中计划，以及该自然周周一至周日的日志。"""
    requested = _coerce_date(date, "date")
    listed = list_memory(home, "plans")
    warnings = list(listed["warnings"])
    selected = []
    for entry in listed["entries"]:
        name = entry["name"]
        current = read_memory(home, name)
        if not current["exists"]:
            warnings.append(_plan_warning(name))
            continue
        spec = _parse_name(name)
        try:
            parsed = _front_matter(current["content"])
            if parsed is None:
                raise ValueError("missing metadata")
            raw_metadata, body = parsed
            metadata = _plan_metadata(spec, raw_metadata)
        except (ValueError, TypeError):
            warnings.append(_plan_warning(name))
            continue
        start = date_type.fromisoformat(metadata["start_date"])
        end = date_type.fromisoformat(metadata["end_date"])
        if metadata["status"] == "in_progress" and start <= requested <= end:
            plan = {key: _json_metadata(value) for key, value in metadata.items()}
            plan["name"] = name
            plan["content"] = body
            selected.append(plan)
    if len(selected) > 1:
        _fail("CURRENT_PLAN_CONFLICT", "多份进行中计划同时覆盖请求日期", False,
              "修正计划状态或起止日期，确保最多一份计划覆盖该日",
              {"names": [plan["name"] for plan in selected]})
    monday = requested - timedelta(days=requested.weekday())
    week_logs = {}
    for offset in range(7):
        day = monday + timedelta(days=offset)
        result = read_memory(home, "log:" + day.isoformat())
        week_logs[day.isoformat()] = result["content"]
    return {
        "plan": selected[0] if selected else None,
        "week_logs": week_logs,
        "platform_schedule": {"available": False, "reason": "not_in_v0"},
        "warnings": warnings,
    }


__all__ = [
    "MAX_MEMORY_BYTES", "MemoryError", "read_memory", "list_memory",
    "write_memory", "parse_next_race", "get_current_plan",
]
