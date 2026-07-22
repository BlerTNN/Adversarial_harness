import errno
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import harness
import platform_support


class PlatformSupportTests(unittest.TestCase):
    def test_bare_program_resolution_never_probes_the_child_workspace(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            platform_support.shutil,
            "which",
            return_value="/trusted/path/tool",
        ) as which:
            actual = platform_support.resolve_program(
                "tool",
                cwd=Path(temporary) / "candidate",
            )

        self.assertEqual(actual, "/trusted/path/tool")
        which.assert_called_once_with("tool")

    def test_bare_program_resolution_freezes_relative_path_entries(self):
        with patch.object(platform_support.shutil, "which", return_value="relative-bin/tool"):
            actual = platform_support.resolve_program("tool", cwd=Path("candidate"))

        self.assertEqual(actual, str(Path("relative-bin/tool").resolve()))
        self.assertTrue(Path(actual).is_absolute())

    def test_detached_windows_creation_flags_follow_job_breakaway_policy(self):
        base = (
            platform_support.DETACHED_PROCESS
            | platform_support.CREATE_NEW_PROCESS_GROUP
            | platform_support.CREATE_SUSPENDED
        )
        self.assertEqual(
            platform_support._detached_creation_flags_for_job(in_job=False),
            base,
        )
        self.assertEqual(
            platform_support._detached_creation_flags_for_job(
                in_job=True,
                limit_flags=platform_support.JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK,
            ),
            base,
        )
        self.assertEqual(
            platform_support._detached_creation_flags_for_job(
                in_job=True,
                limit_flags=platform_support.JOB_OBJECT_LIMIT_BREAKAWAY_OK,
            ),
            base | platform_support.CREATE_BREAKAWAY_FROM_JOB,
        )
        with self.assertRaisesRegex(RuntimeError, "does not permit"):
            platform_support._detached_creation_flags_for_job(in_job=True)

    def test_windows_detached_spawn_kills_a_child_that_remains_in_a_job(self):
        class SuspendedProcess:
            pid = 42
            _handle = 99
            killed = False
            waited = False

            def kill(self):
                self.killed = True

            def wait(self, timeout=None):
                self.waited = True
                return 1

        process = SuspendedProcess()
        with patch.object(platform_support, "WINDOWS", True), patch.object(
            platform_support,
            "_windows_detached_creation_flags",
            return_value=platform_support.CREATE_SUSPENDED,
            create=True,
        ), patch.object(
            platform_support,
            "_windows_process_in_job",
            return_value=True,
            create=True,
        ), patch.object(
            platform_support,
            "_resume_suspended_process",
            create=True,
        ) as resume, patch.object(
            platform_support,
            "_windows_process_info",
            return_value=(True, "windows-filetime:1"),
            create=True,
        ), patch.object(
            platform_support.subprocess,
            "Popen",
            return_value=process,
        ):
            with self.assertRaisesRegex(RuntimeError, "remained in an enclosing"):
                platform_support.spawn_detached_process(["agent"])

        self.assertTrue(process.killed)
        self.assertTrue(process.waited)
        resume.assert_not_called()

    def test_windows_detached_spawn_resumes_a_verified_child(self):
        class SuspendedProcess:
            pid = 42
            _handle = 99

        process = SuspendedProcess()
        with patch.object(platform_support, "WINDOWS", True), patch.object(
            platform_support,
            "_windows_detached_creation_flags",
            return_value=platform_support.CREATE_SUSPENDED,
            create=True,
        ), patch.object(
            platform_support,
            "_windows_process_in_job",
            return_value=False,
            create=True,
        ), patch.object(
            platform_support,
            "_resume_suspended_process",
            create=True,
        ) as resume, patch.object(
            platform_support,
            "_windows_process_info",
            return_value=(True, "windows-filetime:1"),
            create=True,
        ), patch.object(
            platform_support.subprocess,
            "Popen",
            return_value=process,
        ):
            actual = platform_support.spawn_detached_process(["agent"])

        self.assertIs(actual, process)
        resume.assert_called_once_with(process.pid)

    def test_windows_detached_spawn_refuses_a_host_job_query_failure(self):
        with patch.object(platform_support, "WINDOWS", True), patch.object(
            platform_support,
            "_windows_detached_creation_flags",
            side_effect=PermissionError("simulated query failure"),
            create=True,
        ), patch.object(platform_support.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(RuntimeError, "Could not inspect"):
                platform_support.spawn_detached_process(["agent"])

        popen.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows Job breakaway behavior")
    def test_windows_restricted_host_job_refuses_detached_child_before_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result_path = root / "result.txt"
            sentinel_path = root / "sentinel.txt"
            script = (
                "import sys; from pathlib import Path; import platform_support; "
                "result=Path(sys.argv[1]); sentinel=Path(sys.argv[2]); "
                "\ntry:\n"
                " platform_support.spawn_detached_process([sys.executable,'-c',"
                "'from pathlib import Path; import sys; Path(sys.argv[1]).write_text(\\\"ran\\\")',str(sentinel)])\n"
                "except RuntimeError as error:\n result.write_text(str(error),encoding='utf-8')\n"
                "else:\n raise SystemExit(3)"
            )
            job = platform_support._WindowsJob()
            launcher = subprocess.Popen(
                [sys.executable, "-c", script, str(result_path), str(sentinel_path)],
                cwd=Path(__file__).resolve().parent,
                creationflags=(
                    platform_support.CREATE_NEW_PROCESS_GROUP
                    | platform_support.CREATE_SUSPENDED
                ),
            )
            try:
                job.assign(launcher.pid)
                platform_support._resume_suspended_process(launcher.pid)
                self.assertEqual(launcher.wait(timeout=10), 0)
            finally:
                if launcher.poll() is None:
                    try:
                        job.terminate()
                    except OSError:
                        launcher.kill()
                    launcher.wait(timeout=5)
                job.close()

            self.assertIn("does not permit", result_path.read_text(encoding="utf-8"))
            self.assertFalse(sentinel_path.exists())

    @unittest.skipUnless(os.name == "nt", "Windows Job failure classification")
    def test_windows_managed_job_assignment_failure_is_a_runtime_safety_error(self):
        with patch.object(
            platform_support._WindowsJob,
            "assign",
            side_effect=PermissionError("simulated Job assignment failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Could not contain Windows process"):
                platform_support.spawn_managed_process(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                )

    def test_file_lock_is_mutually_exclusive_across_processes(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "shared.lock"
            ready_path = Path(temporary) / "ready"
            script = (
                "import sys,time; from pathlib import Path; "
                "from platform_support import file_lock; "
                "p=Path(sys.argv[1]); ready=Path(sys.argv[2]); "
                "\nwith file_lock(p):\n ready.write_text('locked'); time.sleep(30)"
            )
            child = subprocess.Popen(
                [sys.executable, "-c", script, str(lock_path), str(ready_path)],
                cwd=Path(__file__).resolve().parent,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.addCleanup(lambda: child.poll() is None and child.kill())
            deadline = time.monotonic() + 5
            while not ready_path.is_file() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(ready_path.is_file(), f"lock holder exited with {child.poll()}")
            with self.assertRaises(BlockingIOError):
                with platform_support.file_lock(lock_path, blocking=False):
                    pass
            child.terminate()
            child.wait(timeout=5)
            with platform_support.file_lock(lock_path, blocking=False):
                pass

    @unittest.skipUnless(os.name == "nt", "Windows lock error mapping")
    def test_windows_file_lock_does_not_retry_non_contention_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "shared.lock"
            with lock_path.open("a+b") as stream, patch.object(
                platform_support.msvcrt,
                "locking",
                side_effect=OSError(errno.EBADF, "bad descriptor"),
            ):
                with self.assertRaises(OSError) as raised:
                    platform_support.acquire_file_lock(stream)
                self.assertEqual(raised.exception.errno, errno.EBADF)

    @unittest.skipUnless(os.name == "nt", "Windows Toolhelp error mapping")
    def test_windows_thread_enumeration_fails_closed_on_midstream_error(self):
        import ctypes

        def first_thread(_snapshot, entry_pointer):
            entry_pointer._obj.th32OwnerProcessID = 42
            entry_pointer._obj.th32ThreadID = 123
            return True

        def failed_next(_snapshot, _entry_pointer):
            ctypes.set_last_error(5)  # ERROR_ACCESS_DENIED, not normal exhaustion
            return False

        with patch.object(
            platform_support._kernel32,
            "CreateToolhelp32Snapshot",
            return_value=99,
        ), patch.object(
            platform_support._kernel32,
            "Thread32First",
            side_effect=first_thread,
        ), patch.object(
            platform_support._kernel32,
            "Thread32Next",
            side_effect=failed_next,
        ), patch.object(
            platform_support._kernel32,
            "CloseHandle",
            return_value=True,
        ):
            with self.assertRaises(OSError) as raised:
                platform_support._windows_thread_ids(42)

        self.assertEqual(raised.exception.winerror, 5)

    @unittest.skipUnless(os.name == "nt", "Windows process identity error mapping")
    def test_windows_process_open_query_errors_are_unknown_not_gone(self):
        for error in (platform_support.ERROR_ACCESS_DENIED, 8):
            with self.subTest(error=error), patch.object(
                platform_support._kernel32,
                "OpenProcess",
                return_value=0,
            ), patch.object(
                platform_support.ctypes,
                "get_last_error",
                return_value=error,
            ):
                self.assertEqual(
                    platform_support.process_identity_status(42, "saved-token"),
                    "unknown",
                )

    @unittest.skipUnless(os.name == "nt", "Windows process identity error mapping")
    def test_windows_missing_pid_is_gone_but_wait_failure_is_unknown(self):
        with patch.object(
            platform_support._kernel32,
            "OpenProcess",
            return_value=0,
        ), patch.object(
            platform_support.ctypes,
            "get_last_error",
            return_value=platform_support.ERROR_INVALID_PARAMETER,
        ):
            self.assertEqual(
                platform_support.process_identity_status(42, "saved-token"),
                "gone",
            )

        with patch.object(
            platform_support._kernel32,
            "OpenProcess",
            return_value=99,
        ), patch.object(
            platform_support._kernel32,
            "WaitForSingleObject",
            return_value=platform_support.WAIT_FAILED,
        ), patch.object(
            platform_support._kernel32,
            "CloseHandle",
            return_value=True,
        ):
            self.assertEqual(
                platform_support.process_identity_status(42, "saved-token"),
                "unknown",
            )

    def test_pid_identity_probe_is_non_destructive_and_rejects_wrong_token(self):
        child = platform_support.spawn_managed_process(
            [sys.executable, "-c", "import time; time.sleep(30)"],
        )
        self.addCleanup(lambda: platform_support.terminate_managed_process(child, grace=0.2))
        started = platform_support.pid_start_time(child.pid)
        self.assertTrue(started)
        self.assertTrue(platform_support.process_matches(child.pid, started))
        self.assertFalse(platform_support.process_matches(child.pid, started + "-wrong"))
        self.assertIsNone(child.poll())
        platform_support.terminate_managed_process(child, grace=1)
        self.assertFalse(platform_support.process_matches(child.pid, started))

    @unittest.skipIf(os.name == "nt", "POSIX identity behavior")
    def test_posix_live_pid_with_an_unreadable_saved_identity_fails_closed(self):
        with patch.object(platform_support.os, "kill", return_value=None), patch.object(
            platform_support,
            "pid_status",
            return_value="S",
        ), patch.object(
            platform_support,
            "pid_start_time",
            return_value="",
        ), patch.object(
            platform_support,
            "_posix_group_has_live_members",
            return_value=True,
        ) as group_scan:
            self.assertFalse(platform_support.process_matches(42, "saved-token"))
            self.assertFalse(platform_support.process_group_matches(42, "saved-token"))

        group_scan.assert_not_called()

    @unittest.skipIf(os.name == "nt", "POSIX identity behavior")
    def test_posix_missing_leader_can_still_match_its_live_descendant_group(self):
        with patch.object(
            platform_support.os,
            "kill",
            side_effect=ProcessLookupError,
        ), patch.object(
            platform_support,
            "_posix_group_has_live_members",
            return_value=True,
        ):
            self.assertTrue(platform_support.process_group_matches(42, "saved-token"))

    @unittest.skipIf(os.name == "nt", "POSIX identity behavior")
    def test_posix_managed_spawn_reaps_a_child_when_identity_cannot_be_read(self):
        with patch.object(platform_support, "pid_start_time", return_value=""):
            with self.assertRaisesRegex(RuntimeError, "safe process identity"):
                platform_support.spawn_managed_process(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                )

    def test_process_tree_termination_includes_grandchildren(self):
        with tempfile.TemporaryDirectory() as temporary:
            pid_path = Path(temporary) / "grandchild.pid"
            script = (
                "import subprocess,sys,time; from pathlib import Path; "
                "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
                "Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(30)"
            )
            parent = platform_support.spawn_managed_process(
                [sys.executable, "-c", script, str(pid_path)],
            )
            parent_started = platform_support.pid_start_time(parent.pid)
            self.addCleanup(
                lambda: platform_support.terminate_managed_process(parent, grace=0.2)
            )
            deadline = time.monotonic() + 5
            while not pid_path.is_file() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(pid_path.is_file())
            grandchild_pid = int(pid_path.read_text())
            self.assertTrue(platform_support.process_matches(grandchild_pid))

            platform_support.terminate_managed_process(parent, grace=1)
            deadline = time.monotonic() + 5
            while platform_support.process_matches(grandchild_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(platform_support.process_matches(grandchild_pid))

    def test_process_tree_cleanup_works_after_root_exits_first(self):
        with tempfile.TemporaryDirectory() as temporary:
            pid_path = Path(temporary) / "grandchild.pid"
            script = (
                "import subprocess,sys; from pathlib import Path; "
                "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
                "Path(sys.argv[1]).write_text(str(p.pid))"
            )
            parent = platform_support.spawn_managed_process(
                [sys.executable, "-c", script, str(pid_path)]
            )
            self.addCleanup(lambda: platform_support.terminate_managed_process(parent, grace=0.2))
            self.assertEqual(parent.wait(timeout=5), 0)
            self.assertTrue(pid_path.is_file())
            grandchild_pid = int(pid_path.read_text())
            self.assertTrue(platform_support.process_matches(grandchild_pid))

            platform_support.terminate_managed_process(parent, grace=1)
            deadline = time.monotonic() + 5
            while platform_support.process_matches(grandchild_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(platform_support.process_matches(grandchild_pid))

    @unittest.skipIf(os.name == "nt", "Windows recovery requires the owning Job handle")
    def test_external_recovery_terminates_a_group_after_its_leader_exits(self):
        with tempfile.TemporaryDirectory() as temporary:
            pid_path = Path(temporary) / "grandchild.pid"
            ready_path = Path(temporary) / "grandchild.ready"
            grandchild = (
                "import signal,sys,time; from pathlib import Path; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "Path(sys.argv[1]).write_text('ready'); time.sleep(30)"
            )
            script = (
                "import subprocess,sys; from pathlib import Path; "
                f"p=subprocess.Popen([sys.executable,'-c',{grandchild!r},sys.argv[2]]); "
                "Path(sys.argv[1]).write_text(str(p.pid))"
            )
            leader = subprocess.Popen(
                [sys.executable, "-c", script, str(pid_path), str(ready_path)],
                **platform_support.process_group_kwargs(),
            )
            started = platform_support.pid_start_time(leader.pid)
            self.addCleanup(
                lambda: platform_support.terminate_process_group(leader.pid, started, grace=0.2)
            )
            deadline = time.monotonic() + 5
            while (
                (not pid_path.is_file() or not ready_path.is_file() or not platform_support.pid_status(leader.pid).startswith("Z"))
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            self.assertTrue(pid_path.is_file() and ready_path.is_file())
            self.assertTrue(platform_support.pid_status(leader.pid).startswith("Z"))
            grandchild_pid = int(pid_path.read_text())
            self.assertTrue(platform_support.process_group_matches(leader.pid, started))
            self.assertTrue(platform_support.process_matches(grandchild_pid))

            platform_support.terminate_process_group(leader.pid, started, grace=0.2)
            self.assertEqual(leader.wait(timeout=5), 0)
            deadline = time.monotonic() + 5
            while platform_support.process_matches(grandchild_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(platform_support.process_group_matches(leader.pid, started))
            self.assertFalse(platform_support.process_matches(grandchild_pid))

    def test_process_marker_uses_pid_and_creation_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "process.json"
            harness.write_process_marker(marker, os.getpid())
            record = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(record["pid"], os.getpid())
            self.assertTrue(record["pid_started"])
            self.assertEqual(harness.read_process_marker(marker), (os.getpid(), record["pid_started"]))
            self.assertTrue(platform_support.process_matches(record["pid"], record["pid_started"]))

    def test_process_launch_and_command_rendering_match_the_host(self):
        kwargs = platform_support.process_group_kwargs()
        if os.name == "nt":
            self.assertIn("creationflags", kwargs)
            self.assertNotIn("start_new_session", kwargs)
            self.assertEqual(platform_support.format_command(["a b", "c"]), '"a b" c')
        else:
            self.assertTrue(kwargs["start_new_session"])
            self.assertNotIn("creationflags", kwargs)
            self.assertEqual(platform_support.format_command(["a b", "c"]), "'a b' c")


if __name__ == "__main__":
    unittest.main()
