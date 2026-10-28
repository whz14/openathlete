# -*- coding: utf-8 -*-
"""非阻塞进程内/跨进程锁测试；锁文件在释放与崩溃后都保留。"""
import asyncio
import multiprocessing
import os

import pytest

from openathlete.locks import LockBusy, home_lock


def _try_lock_worker(home, queue):
    try:
        with home_lock(home, "sync"):
            queue.put("acquired")
    except LockBusy:
        queue.put("busy")


def _crash_with_lock_worker(home, connection):
    lock = home_lock(home, "sync")
    lock.acquire()
    connection.send("acquired")
    os._exit(0)


def test_process_local_busy_and_lock_file_kept(tmp_path):
    first = home_lock(tmp_path, "sync")
    first.acquire()
    with pytest.raises(LockBusy):
        home_lock(tmp_path, "sync").acquire()
    path = tmp_path / "locks" / "sync.lock"
    assert path.exists()
    first.release()
    with home_lock(tmp_path, "sync"):
        assert path.exists()
    assert path.exists()


def test_real_process_lock_and_os_release_after_crash(tmp_path):
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    with home_lock(tmp_path, "sync"):
        process = context.Process(target=_try_lock_worker, args=(str(tmp_path), queue))
        process.start()
        process.join(10)
        assert not process.is_alive()
        assert queue.get(timeout=2) == "busy"
        assert process.exitcode == 0

    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_crash_with_lock_worker, args=(str(tmp_path), sender))
    process.start()
    assert receiver.poll(5)
    assert receiver.recv() == "acquired"
    process.join(10)
    assert not process.is_alive()
    with home_lock(tmp_path, "sync"):
        pass
    assert (tmp_path / "locks" / "sync.lock").exists()


def test_async_process_lock_is_nonblocking(tmp_path):
    async def scenario():
        first = home_lock(tmp_path, "sync")
        async with first:
            with pytest.raises(LockBusy):
                await home_lock(tmp_path, "sync").__aenter__()
        async with home_lock(tmp_path, "sync"):
            pass

    asyncio.run(scenario())
    assert (tmp_path / "locks" / "sync.lock").exists()
