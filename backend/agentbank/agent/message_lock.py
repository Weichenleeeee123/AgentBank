"""Crash-released conversation locks for the local SQLite runtime.

Lock files are permanent, empty of business data, and must never be unlinked:
replacing a locked inode could admit two workers. All workers sharing a database
must also share its adjacent lock directory on a filesystem supporting OS locks.
"""
from __future__ import annotations

from _thread import LockType
from contextlib import contextmanager
import errno
import hashlib
import json
import os
import sys
from pathlib import Path
from threading import Lock
from typing import Iterator
from weakref import WeakKeyDictionary

from sqlalchemy.engine import Engine

from agentbank.db import Database
from agentbank.errors import DomainError


_guard = Lock()
_memory_locks: WeakKeyDictionary[Engine, dict[str, LockType]] = WeakKeyDictionary()


@contextmanager
def conversation_lock(database: Database, user_id: str, conversation_id: str) -> Iterator[None]:
    identity = hashlib.sha256(json.dumps([user_id, conversation_id]).encode()).hexdigest()
    filename = database.engine.url.database
    if not filename or filename == ':memory:':
        with _guard:
            locks = _memory_locks.setdefault(database.engine, {})
            lock = locks.setdefault(identity, Lock())
        if not lock.acquire(blocking=False):
            raise DomainError('CONVERSATION_BUSY')
        try:
            yield
        finally:
            lock.release()
        return

    directory = Path(str(Path(filename).resolve()) + '.message-locks')
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / (identity + '.lock')).open('a+b') as stream:
        # Windows byte-range locking requires a byte at offset zero.
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b'\0')
            stream.flush()
        stream.seek(0)
        try:
            if sys.platform == 'win32':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise DomainError('CONVERSATION_BUSY') from error
            raise
        try:
            yield
        finally:
            stream.seek(0)
            if sys.platform == 'win32':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
