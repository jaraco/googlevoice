"""
Per-profile locking and stale-lock cleanup for the browser-driven features.

Chrome allows only **one** instance per ``user-data-dir``: it writes a
``SingletonLock`` and a second launch on the same profile fails with a cryptic
"failed to connect". So :class:`~googlevoice.browser.BrowserSender` and
:class:`~googlevoice.call.Caller` cannot run concurrently on the *same* profile.

:class:`ProfileLock` makes that explicit:

* concurrent use on one profile **fails fast** with :class:`BrowserBusyError`
  (or, with ``wait=True``, queues until the profile frees up);
* a stale ``SingletonLock`` left by a crashed/killed run is cleared
  automatically, so it can't wedge the next launch.

To run truly in parallel, give each worker its own ``profile_dir`` (each signed
in independently); a single Google Voice account is otherwise one "line" and is
naturally used one operation at a time.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import time

try:
    import fcntl  # POSIX advisory locks
except ImportError:  # pragma: no cover - non-POSIX (e.g. Windows)
    fcntl = None

# Chrome's single-instance marker files within a user-data-dir.
_SINGLETONS = ('SingletonLock', 'SingletonSocket', 'SingletonCookie')


class BrowserBusyError(RuntimeError):
    """Another googlevoice browser is already using this Chrome profile."""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, just not ours
        return True
    return True


def _holder_hint(lockpath) -> str:
    """`` (PID 1234)`` for the process holding the lock, or '' if unknown."""
    try:
        pid = pathlib.Path(lockpath).read_text().strip()
    except OSError:
        pid = ''
    return f' (held by PID {pid})' if pid else ''


def clear_stale_singleton(profile_dir) -> None:
    """
    Remove Chrome's ``Singleton*`` files if the owning process is gone.

    ``SingletonLock`` is a symlink whose target encodes ``host-pid``; if that
    pid is dead the lock is stale and would block the next launch.
    """
    profile = pathlib.Path(profile_dir).expanduser()
    try:
        target = os.readlink(profile / 'SingletonLock')
    except (OSError, ValueError):
        return  # not a symlink / missing -> nothing to clear
    pid = None
    with contextlib.suppress(ValueError):
        pid = int(target.rsplit('-', 1)[-1])
    if pid is not None and _pid_alive(pid):
        return  # a live Chrome owns it -- leave it alone
    for name in _SINGLETONS:
        with contextlib.suppress(OSError):
            (profile / name).unlink()


class ProfileLock:
    """Advisory, cross-process lock guarding one Chrome ``user-data-dir``."""

    def __init__(self, profile_dir, *, wait: bool = False, timeout: float = 30):
        self.profile_dir = pathlib.Path(profile_dir).expanduser()
        self.wait = wait
        self.timeout = timeout
        self._fh = None

    def __enter__(self) -> ProfileLock:  # noqa: PYI034
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    def acquire(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        clear_stale_singleton(self.profile_dir)
        if fcntl is None:  # pragma: no cover - rely on Chrome's own lock
            return
        lockpath = self.profile_dir / '.googlevoice.lock'
        lockpath.touch(exist_ok=True)  # 'r+' (don't truncate -- preserves holder pid)
        # Held open for the lock's whole life (released in release()), so a
        # context manager would be wrong here.
        self._fh = open(lockpath, 'r+')  # noqa: SIM115
        start = time.monotonic()
        while True:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Record our pid so a blocked waiter can report who holds it.
                self._fh.seek(0)
                self._fh.truncate()
                self._fh.write(str(os.getpid()))
                self._fh.flush()
                return
            except OSError:
                if not self.wait or time.monotonic() - start > self.timeout:
                    self._fh.close()
                    self._fh = None
                    raise BrowserBusyError(
                        f'Another googlevoice browser is already running on '
                        f'{self.profile_dir}{_holder_hint(lockpath)}. Wait for it '
                        f'to finish, pass wait=True to queue, or use a separate '
                        f'profile_dir to run in parallel.'
                    ) from None
                time.sleep(0.5)

    def release(self) -> None:
        if self._fh is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None
