"""Small operating-system adapters shared by the Harness entry points."""

from __future__ import annotations

import errno
import json
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


WINDOWS = os.name == "nt"
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
CREATE_SUSPENDED = 0x00000004
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_PARAMETER = 87

if WINDOWS:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    _kernel32.GetProcessTimes.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.IsProcessInJob.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.BOOL),
    ]
    _kernel32.IsProcessInJob.restype = wintypes.BOOL

    class _JobBasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JobExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JobBasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    _kernel32.Thread32First.restype = wintypes.BOOL
    _kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    _kernel32.Thread32Next.restype = wintypes.BOOL
    _kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenThread.restype = wintypes.HANDLE
    _kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    _kernel32.ResumeThread.restype = wintypes.DWORD
else:
    import fcntl


def configure_utf8_stdio() -> None:
    """Keep CLI and redirected log output portable across host code pages."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except (OSError, ValueError):
                pass


def acquire_file_lock(stream: Any, *, blocking: bool = True) -> None:
    if WINDOWS:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        while True:
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as error:
                contention = error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or getattr(
                    error, "winerror", None
                ) in {32, 33}
                if not contention:
                    raise
                if not blocking:
                    raise BlockingIOError("File lock is already held") from error
                time.sleep(0.05)
    else:
        operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        fcntl.flock(stream.fileno(), operation)


def release_file_lock(stream: Any) -> None:
    if WINDOWS:
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def file_lock(path: Path, *, blocking: bool = True) -> Iterator[None]:
    """Hold one exclusive cross-process lock until the context exits."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        acquire_file_lock(stream, blocking=blocking)
        try:
            yield
        finally:
            release_file_lock(stream)


def set_private_permissions(path: Path, *, directory: bool = False) -> None:
    """Apply owner-only POSIX modes; Windows keeps the inherited directory ACL."""
    if not WINDOWS:
        path.chmod(0o700 if directory else 0o600)


def is_real_directory(path: Path) -> bool:
    """Reject symlinks and Windows directory reparse points such as junctions."""
    try:
        details = path.lstat()
    except OSError:
        return False
    if not stat.S_ISDIR(details.st_mode) or path.is_symlink():
        return False
    if WINDOWS:
        flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
        if int(getattr(details, "st_file_attributes", 0)) & flag:
            return False
    return True


def process_group_kwargs() -> dict[str, Any]:
    """Return Popen arguments that establish a separately terminable child scope."""
    if WINDOWS:
        return {"creationflags": CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _detached_creation_flags_for_job(*, in_job: bool, limit_flags: int = 0) -> int:
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_SUSPENDED
    if not in_job or limit_flags & JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK:
        return flags
    if limit_flags & JOB_OBJECT_LIMIT_BREAKAWAY_OK:
        return flags | CREATE_BREAKAWAY_FROM_JOB
    raise RuntimeError(
        "The Windows host Job Object does not permit a detached Supervisor"
    )


if WINDOWS:
    class _WindowsJob:
        """One non-inheritable Job Object whose last close kills all members."""

        def __init__(self) -> None:
            self.handle = _kernel32.CreateJobObjectW(None, None)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
            limits = _JobExtendedLimitInformation()
            limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
            if not _kernel32.SetInformationJobObject(
                self.handle,
                9,  # JobObjectExtendedLimitInformation
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                error = ctypes.WinError(ctypes.get_last_error())
                self.close()
                raise error

        def assign(self, pid: int) -> None:
            process_set_quota = 0x0100
            process_terminate = 0x0001
            process = _kernel32.OpenProcess(process_set_quota | process_terminate, False, pid)
            if not process:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not _kernel32.AssignProcessToJobObject(self.handle, process):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                _kernel32.CloseHandle(process)

        def terminate(self) -> None:
            if self.handle and not _kernel32.TerminateJobObject(self.handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())

        def close(self) -> None:
            if self.handle:
                if not _kernel32.CloseHandle(self.handle):
                    raise ctypes.WinError(ctypes.get_last_error())
                self.handle = None

        def __del__(self) -> None:
            try:
                self.close()
            except BaseException:
                pass


    def _windows_thread_ids(pid: int) -> list[int]:
        snapshot = _kernel32.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = _ThreadEntry32()
            entry.dwSize = ctypes.sizeof(entry)
            identifiers: list[int] = []
            ctypes.set_last_error(0)
            if not _kernel32.Thread32First(snapshot, ctypes.byref(entry)):
                error = ctypes.get_last_error()
                if error != 18:  # ERROR_NO_MORE_FILES
                    raise ctypes.WinError(error)
                return identifiers
            while True:
                if int(entry.th32OwnerProcessID) == pid:
                    identifiers.append(int(entry.th32ThreadID))
                entry.dwSize = ctypes.sizeof(entry)
                ctypes.set_last_error(0)
                if _kernel32.Thread32Next(snapshot, ctypes.byref(entry)):
                    continue
                error = ctypes.get_last_error()
                if error != 18:  # ERROR_NO_MORE_FILES
                    raise ctypes.WinError(error)
                break
            return identifiers
        finally:
            _kernel32.CloseHandle(snapshot)


    def _resume_suspended_process(pid: int) -> None:
        identifiers: list[int] = []
        for _ in range(20):
            identifiers = _windows_thread_ids(pid)
            if identifiers:
                break
            time.sleep(0.01)
        if len(identifiers) != 1:
            raise RuntimeError(
                f"Expected one suspended primary thread for PID {pid}, found {len(identifiers)}"
            )
        thread = _kernel32.OpenThread(0x0002, False, identifiers[0])  # THREAD_SUSPEND_RESUME
        if not thread:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            previous = int(_kernel32.ResumeThread(thread))
            if previous != 1:
                if previous == 0xFFFFFFFF:
                    raise ctypes.WinError(ctypes.get_last_error())
                raise RuntimeError(
                    f"Suspended primary thread for PID {pid} had unexpected count {previous}"
                )
        finally:
            _kernel32.CloseHandle(thread)


    def _windows_process_in_job(process_handle: int) -> bool:
        result = wintypes.BOOL()
        if not _kernel32.IsProcessInJob(wintypes.HANDLE(process_handle), None, ctypes.byref(result)):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(result.value)


    def _windows_detached_creation_flags() -> int:
        current = int(_kernel32.GetCurrentProcess())
        if not _windows_process_in_job(current):
            return _detached_creation_flags_for_job(in_job=False)
        limits = _JobExtendedLimitInformation()
        if not _kernel32.QueryInformationJobObject(
            None,
            9,  # JobObjectExtendedLimitInformation for the immediate Job
            ctypes.byref(limits),
            ctypes.sizeof(limits),
            None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        limit_flags = int(limits.BasicLimitInformation.LimitFlags)
        return _detached_creation_flags_for_job(
            in_job=True,
            limit_flags=limit_flags,
        )


def spawn_detached_process(command: list[str], **kwargs: Any) -> subprocess.Popen[str]:
    """Spawn a Supervisor that cannot silently remain in an enclosing Windows Job."""
    if not WINDOWS:
        kwargs.setdefault("start_new_session", True)
        process = subprocess.Popen(command, **kwargs)
        started = pid_start_time(process.pid)
        if not started:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            raise RuntimeError(
                f"Could not establish a safe detached Supervisor identity for PID {process.pid}"
            )
        setattr(process, "_harness_started", started)
        return process

    try:
        flags = int(kwargs.pop("creationflags", 0)) | _windows_detached_creation_flags()
    except OSError as error:
        raise RuntimeError(f"Could not inspect the Windows host Job Object: {error}") from error
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(command, creationflags=flags, **kwargs)
        if _windows_process_in_job(int(process._handle)):
            raise RuntimeError(
                "The detached Supervisor remained in an enclosing Windows Job Object; use --foreground"
            )
        alive, started = _windows_process_info(process.pid)
        if not alive or not started:
            raise RuntimeError(
                f"Could not establish a safe detached Supervisor identity for PID {process.pid}"
            )
        _resume_suspended_process(process.pid)
        setattr(process, "_harness_started", started)
        return process
    except BaseException as error:
        if process is not None:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if isinstance(error, OSError):
            raise RuntimeError(f"Could not establish a detached Windows Supervisor: {error}") from error
        raise


def spawn_managed_process(
    command: list[str],
    *,
    detached: bool = False,
    **kwargs: Any,
) -> subprocess.Popen[str]:
    """Spawn a process in a host-native scope that contains every descendant."""
    if not WINDOWS:
        kwargs.setdefault("start_new_session", True)
        process = subprocess.Popen(command, **kwargs)
        started = pid_start_time(process.pid)
        if not started:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            raise RuntimeError(f"Could not establish a safe process identity for PID {process.pid}")
        setattr(process, "_harness_started", started)
        return process

    try:
        job = _WindowsJob()
    except OSError as error:
        raise RuntimeError(f"Could not create a Windows process Job: {error}") from error
    process: subprocess.Popen[str] | None = None
    try:
        flags = int(kwargs.pop("creationflags", 0)) | CREATE_NEW_PROCESS_GROUP | CREATE_SUSPENDED
        if detached:
            flags |= DETACHED_PROCESS
        process = subprocess.Popen(command, creationflags=flags, **kwargs)
    except BaseException as error:
        try:
            job.close()
        except OSError:
            pass
        raise
    assigned = False
    try:
        job.assign(process.pid)
        assigned = True
        alive, started = _windows_process_info(process.pid)
        if not alive or not started:
            raise RuntimeError(f"Could not establish a safe process identity for PID {process.pid}")
        _resume_suspended_process(process.pid)
        setattr(process, "_harness_job", job)
        setattr(process, "_harness_started", started)
        return process
    except BaseException as error:
        if process is not None:
            try:
                if assigned:
                    job.terminate()
                else:
                    process.kill()
            except OSError:
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        try:
            job.close()
        except OSError:
            pass
        if isinstance(error, OSError):
            raise RuntimeError(f"Could not contain Windows process PID {process.pid}: {error}") from error
        raise


def managed_process_start_time(process: subprocess.Popen[str]) -> str:
    return str(getattr(process, "_harness_started", "")) or pid_start_time(process.pid)


def _posix_process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _posix_group_has_live_members(process_group: int) -> bool:
    executable = shutil.which("ps")
    if executable:
        try:
            output = subprocess.run(
                [executable, "-axo", "pgid=,stat="],
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            ).stdout
            rows = []
            for line in output.splitlines():
                fields = line.split(None, 1)
                if len(fields) == 2 and fields[0].isdigit():
                    rows.append((int(fields[0]), fields[1]))
            if rows:
                return any(group == process_group and not status.startswith("Z") for group, status in rows)
        except (OSError, subprocess.SubprocessError):
            pass
    return _posix_process_group_exists(process_group)


def _posix_pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def process_identity_status(pid: int, started: str = "") -> str:
    """Return match, gone, mismatch, or unknown for one recorded process identity."""
    if pid < 1:
        return "gone"
    if WINDOWS:
        alive, current = _windows_process_info(pid)
        if not alive:
            return "gone"
        if started and not current:
            return "unknown"
        if started and current != started:
            return "mismatch"
        return "match"
    if not _posix_pid_exists(pid):
        return "gone"
    status = pid_status(pid)
    current = pid_start_time(pid)
    if started:
        if not current:
            return "unknown" if _posix_pid_exists(pid) else "gone"
        if current != started:
            return "mismatch"
    return "gone" if status.startswith("Z") else "match"


def process_group_identity_status(pid: int, started: str = "") -> str:
    """Classify a POSIX group while allowing a verified exited leader's descendants."""
    if WINDOWS:
        return process_identity_status(pid, started)
    if pid < 1:
        return "gone"
    if not _posix_pid_exists(pid):
        return "match" if _posix_group_has_live_members(pid) else "gone"
    current = pid_start_time(pid)
    if started:
        if not current:
            if not _posix_pid_exists(pid):
                return "match" if _posix_group_has_live_members(pid) else "gone"
            return "unknown"
        if current != started:
            return "mismatch"
    status = pid_status(pid)
    if status.startswith("Z"):
        return "match" if _posix_group_has_live_members(pid) else "gone"
    return "match"


def process_group_matches(pid: int, started: str = "") -> bool:
    return process_group_identity_status(pid, started) == "match"


def terminate_managed_process(process: subprocess.Popen[str], grace: float = 5.0) -> None:
    """Boundedly terminate a managed root and any descendants, even if the root exited."""
    if WINDOWS:
        job = getattr(process, "_harness_job", None)
        if job is None:
            if process.poll() is not None:
                return
            raise RuntimeError(f"PID {process.pid} was not launched in a Windows Job Object")
        termination_error: BaseException | None = None
        close_error: BaseException | None = None
        try:
            job.terminate()
        except BaseException as error:
            termination_error = error
        try:
            job.close()
        except BaseException as error:
            close_error = error
        if job.handle is None:
            setattr(process, "_harness_job", None)
        try:
            process.wait(timeout=max(1.0, grace))
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f"Managed Windows process PID {process.pid} did not terminate") from error
        if close_error is not None:
            raise RuntimeError(f"Could not close Windows Job for PID {process.pid}") from close_error
        if termination_error is not None and process.returncode is None:
            raise RuntimeError(f"Could not explicitly terminate Windows Job for PID {process.pid}") from termination_error
        return

    process_group = process.pid
    started = managed_process_start_time(process)
    root_alive = process.poll() is None
    identity = process_group_identity_status(process_group, started)
    if not root_alive:
        if identity == "unknown":
            raise RuntimeError(f"Could not verify managed process group {process_group}")
        if identity != "match":
            return
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError as error:
        raise RuntimeError(f"Could not terminate process group {process_group}") from error
    deadline = time.monotonic() + grace
    if root_alive:
        try:
            process.wait(timeout=max(0.1, grace))
        except subprocess.TimeoutExpired:
            pass
    identity = process_group_identity_status(process_group, started)
    while identity == "match" and time.monotonic() < deadline:
        time.sleep(0.05)
        identity = process_group_identity_status(process_group, started)
    if identity == "unknown":
        raise RuntimeError(f"Could not verify managed process group {process_group}")
    if identity == "match":
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError as error:
            raise RuntimeError(f"Could not kill process group {process_group}") from error
        kill_deadline = time.monotonic() + max(1.0, grace)
        identity = process_group_identity_status(process_group, started)
        while identity == "match" and time.monotonic() < kill_deadline:
            time.sleep(0.05)
            identity = process_group_identity_status(process_group, started)
        if identity == "unknown":
            raise RuntimeError(f"Could not verify managed process group {process_group}")
        if identity == "match":
            raise RuntimeError(f"Managed process group {process_group} did not terminate")
    if process.poll() is None:
        try:
            process.wait(timeout=max(1.0, grace))
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f"Managed process PID {process.pid} did not terminate") from error


def format_command(command: list[str]) -> str:
    """Render argv for diagnostics using the host platform's quoting rules."""
    return subprocess.list2cmdline(command) if WINDOWS else shlex.join(command)


def resolve_program(executable: str, *, cwd: Path | None = None) -> str | None:
    """Resolve a program without a shell, honoring an explicit child cwd."""
    candidate = Path(executable).expanduser()
    explicit_path = candidate.is_absolute() or any(
        separator and separator in executable for separator in (os.sep, os.altsep)
    )
    if explicit_path:
        local = candidate if candidate.is_absolute() or cwd is None else cwd / candidate
        found = shutil.which(str(local))
        if found:
            return str(Path(found).resolve())
        if local.is_file() and (WINDOWS or os.access(local, os.X_OK)):
            return str(local.resolve())
        return None
    # A bare name always follows PATH.  In particular, do not probe the child
    # cwd on Windows: a candidate-controlled ``npm.cmd`` must not shadow the
    # configured system ``npm`` verification command.  Candidate-local tools
    # remain available through explicit paths such as ``.\\check.cmd``.
    found = shutil.which(executable)
    return str(Path(found).resolve()) if found else None


def _ps_value(pid: int, field: str) -> str:
    executable = shutil.which("ps")
    if not executable:
        return ""
    try:
        return subprocess.run(
            [executable, "-p", str(pid), "-o", f"{field}="],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _windows_process_info(pid: int) -> tuple[bool, str]:
    if not WINDOWS or pid < 1:
        return False, ""
    process_query_limited_information = 0x1000
    synchronize = 0x00100000
    handle = _kernel32.OpenProcess(
        process_query_limited_information | synchronize, False, pid
    )
    if not handle:
        # A missing PID normally maps to ERROR_INVALID_PARAMETER.  Every other
        # failure is an unreadable identity, not proof that the PID is free;
        # callers with a saved creation token will classify it as unknown.
        error = ctypes.get_last_error()
        return error != ERROR_INVALID_PARAMETER, ""
    try:
        wait_result = int(_kernel32.WaitForSingleObject(handle, 0))
        if wait_result == WAIT_OBJECT_0:
            return False, ""
        if wait_result != WAIT_TIMEOUT:
            return True, ""
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not _kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return True, ""
        value = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
        return True, f"windows-filetime:{value}"
    finally:
        _kernel32.CloseHandle(handle)


def pid_start_time(pid: int) -> str:
    if WINDOWS:
        return _windows_process_info(pid)[1]
    return _ps_value(pid, "lstart")


def pid_status(pid: int) -> str:
    if WINDOWS:
        return "running" if _windows_process_info(pid)[0] else ""
    return _ps_value(pid, "stat")


def process_matches(pid: int, started: str = "") -> bool:
    """Check liveness and fail closed when a required creation token is unavailable."""
    return process_identity_status(pid, started) == "match"


def terminate_process_group(pid: int, started: str = "", grace: float = 5.0) -> None:
    """Terminate an externally recorded POSIX group; Windows requires its owning Job handle."""
    if WINDOWS:
        if not process_matches(pid, started):
            return
        raise RuntimeError(
            "Refusing unsafe PID-only Windows termination; the owning supervisor must close its Job Object"
        )
    identity = process_group_identity_status(pid, started)
    if identity == "unknown":
        raise RuntimeError(f"Could not verify process group {pid}")
    if identity != "match":
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError as error:
        raise RuntimeError(f"Could not terminate process group {pid}") from error
    deadline = time.monotonic() + grace
    identity = process_group_identity_status(pid, started)
    while identity == "match" and time.monotonic() < deadline:
        time.sleep(0.1)
        identity = process_group_identity_status(pid, started)
    if identity == "unknown":
        raise RuntimeError(f"Could not verify process group {pid}")
    if identity == "match":
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError as error:
            raise RuntimeError(f"Could not kill process group {pid}") from error
        kill_deadline = time.monotonic() + max(1.0, grace)
        identity = process_group_identity_status(pid, started)
        while identity == "match" and time.monotonic() < kill_deadline:
            time.sleep(0.05)
            identity = process_group_identity_status(pid, started)
        if identity == "unknown":
            raise RuntimeError(f"Could not verify process group {pid}")
        if identity == "match":
            raise RuntimeError(f"Process group {pid} did not terminate")


def parent_commands(limit: int = 12) -> list[str]:
    """Read ancestor command lines for coordinator-profile detection."""
    if not WINDOWS:
        commands: list[str] = []
        pid = os.getppid()
        for _ in range(limit):
            value = _ps_value(pid, "ppid=,command")
            fields = value.split(None, 1)
            if len(fields) != 2 or not fields[0].isdigit():
                break
            pid = int(fields[0])
            commands.append(fields[1])
            if pid <= 1:
                break
        return commands

    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not powershell:
        return []
    script = (
        "$OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);"
        f"$n=[int]{os.getppid()};$r=@();"
        f"for($i=0;$i -lt {int(limit)};$i++){{"
        "$p=Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $n) -ErrorAction SilentlyContinue;"
        "if($null -eq $p){break};"
        "$r += [pscustomobject]@{parent=[int]$p.ParentProcessId;command=[string]$p.CommandLine};"
        "$n=[int]$p.ParentProcessId;if($n -le 1){break}};"
        "ConvertTo-Json -InputObject @($r) -Compress"
    )
    try:
        output = subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=8,
            creationflags=CREATE_NO_WINDOW,
        ).stdout.strip()
        rows = json.loads(output or "[]")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []
    if isinstance(rows, dict):
        rows = [rows]
    return [str(row.get("command", "")) for row in rows if isinstance(row, dict) and row.get("command")]
