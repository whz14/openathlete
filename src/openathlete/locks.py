"""进程内与跨进程非阻塞锁；锁文件只复用，不删除。"""
import asyncio
import os
import re
import threading
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class LockBusy(RuntimeError):
    """锁已由当前进程的另一执行单元或其他进程持有。"""

    def __init__(self, path):
        self.path = Path(path)
        super().__init__("锁被占用：{}".format(self.path))


_PROCESS_LOCKS = {}
_PROCESS_LOCKS_GUARD = threading.Lock()
_ASYNC_LOCKS = {}
_ASYNC_LOCKS_GUARD = threading.Lock()
_LOCK_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def process_lock(key):
    """返回稳定的进程内 threading.Lock，供同步入口非阻塞获取。"""
    key = str(key)
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.Lock())


def async_process_lock(key):
    """返回当前事件循环专属的 asyncio.Lock。"""
    loop = asyncio.get_running_loop()
    key = (id(loop), str(key))
    with _ASYNC_LOCKS_GUARD:
        return _ASYNC_LOCKS.setdefault(key, asyncio.Lock())


class FileLock:
    """小型 Windows/POSIX 文件锁，所有获取均为非阻塞。"""

    def __init__(self, path):
        self.path = Path(path)
        self._handle = None

    def acquire(self):
        if self._handle is not None:
            raise RuntimeError("同一 FileLock 实例不能重复获取")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise LockBusy(self.path) from exc
            else:
                handle.seek(0)
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise LockBusy(self.path) from exc
        except Exception:
            handle.close()
            raise
        self._handle = handle
        return self

    def release(self):
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc, tb):
        self.release()


class HomeLock:
    """同一 HOME 的进程内锁 + OS 文件锁，同时提供同步和异步上下文。"""

    def __init__(self, home, name="sync"):
        if not _LOCK_NAME_RE.match(name):
            raise ValueError("锁名称只能包含字母、数字、下划线和连字符")
        home = Path(home).expanduser().resolve()
        self.path = home / "locks" / "{}.lock".format(name)
        self._process_lock = process_lock(self.path)
        self._file_lock = FileLock(self.path)
        self._async_lock = None
        self._acquired = False

    def acquire(self):
        if self._acquired:
            raise RuntimeError("同一 HomeLock 实例不能重复获取")
        if not self._process_lock.acquire(blocking=False):
            raise LockBusy(self.path)
        try:
            self._file_lock.acquire()
        except Exception:
            self._process_lock.release()
            raise
        self._acquired = True
        return self

    def release(self):
        if not self._acquired:
            return
        try:
            self._file_lock.release()
        finally:
            self._process_lock.release()
            self._acquired = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc, tb):
        self.release()

    async def __aenter__(self):
        if self._acquired:
            raise RuntimeError("同一 HomeLock 实例不能重复获取")
        self._async_lock = async_process_lock(self.path)
        if self._async_lock.locked():
            raise LockBusy(self.path)
        await self._async_lock.acquire()
        try:
            await asyncio.to_thread(self._file_lock.acquire)
        except Exception:
            self._async_lock.release()
            self._async_lock = None
            raise
        self._acquired = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if not self._acquired:
            return
        try:
            await asyncio.to_thread(self._file_lock.release)
        finally:
            if self._async_lock is not None:
                self._async_lock.release()
            self._async_lock = None
            self._acquired = False


def home_lock(home, name="sync"):
    return HomeLock(home, name)
