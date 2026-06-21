import tempfile
import unittest
import os
import errno
import io
from argparse import Namespace
from pathlib import Path
from unittest import mock
from contextlib import redirect_stdout

import macbox_cli
import macbox_fuse


class MacBoxTests(unittest.TestCase):
    def rewrite_with_temp_project(self, line):
        td = tempfile.TemporaryDirectory()
        old = macbox_cli.project_root
        macbox_cli.project_root = lambda: Path(td.name)
        self.addCleanup(td.cleanup)
        self.addCleanup(lambda: setattr(macbox_cli, "project_root", old))
        return macbox_cli.rewrite_shell_line("alpha", line), Path(td.name) / ".macbox/sessions/alpha/overlay"

    def test_virtual_path_maps_absolute_path_under_overlay(self):
        with tempfile.TemporaryDirectory() as td:
            old = macbox_cli.project_root
            macbox_cli.project_root = lambda: Path(td)
            try:
                mapped = macbox_cli.virtual_path("t", "/Users/example/file.txt")
                self.assertEqual(mapped, Path(td) / ".macbox/sessions/t/overlay/Users/example/file.txt")
            finally:
                macbox_cli.project_root = old

    def test_write_root_restriction(self):
        self.assertTrue(macbox_cli.path_allowed(Path("/tmp/a"), ["/tmp"]))
        self.assertTrue(macbox_cli.path_allowed(Path("/private/tmp/a"), ["/tmp"]))
        self.assertTrue(macbox_cli.path_allowed(Path("/Users/me/a"), ["/"]))

    def test_collect_sessions_reports_pending_changes(self):
        with tempfile.TemporaryDirectory() as td:
            old = macbox_cli.project_root
            macbox_cli.project_root = lambda: Path(td)
            try:
                macbox_cli.ensure_sandbox("alpha", writes=["/tmp"])
                pending = macbox_cli.virtual_path("alpha", "/tmp/a.txt")
                pending.parent.mkdir(parents=True)
                pending.write_text("hello")
                sessions = macbox_cli.collect_sessions()
                self.assertEqual(sessions[0]["name"], "alpha")
                self.assertEqual(sessions[0]["pendingChanges"], 1)
            finally:
                macbox_cli.project_root = old

    def test_rewrite_shell_line_maps_absolute_redirects(self):
        rewritten, overlay = self.rewrite_with_temp_project("echo hi > /tmp/a.txt && cat < /tmp/a.txt")
        self.assertIn(str(overlay), rewritten)
        self.assertIn("> ", rewritten)
        self.assertIn("< ", rewritten)
        self.assertIn("command mkdir -p", rewritten)

    def test_rewrite_shell_line_maps_append_redirects(self):
        rewritten, overlay = self.rewrite_with_temp_project("echo hi >> /tmp/a.txt")
        self.assertIn(">> ", rewritten)
        self.assertIn(str(overlay), rewritten)

    def test_rewrite_shell_line_maps_quoted_absolute_path_with_spaces(self):
        rewritten, overlay = self.rewrite_with_temp_project('echo hi > "/tmp/macbox space path/a.txt"')
        self.assertIn(str(overlay), rewritten)
        self.assertIn("macbox space path/a.txt", rewritten)

    def test_rewrite_shell_line_maps_relative_redirects(self):
        rewritten, overlay = self.rewrite_with_temp_project("echo hi > local.txt && cat < ./local.txt")
        self.assertIn(str(overlay), rewritten)
        self.assertIn("local.txt", rewritten)

    def test_rewrite_shell_line_ignores_redirect_text_inside_quotes(self):
        rewritten, overlay = self.rewrite_with_temp_project('python -c "print(1 > 0)" > /tmp/out.txt')
        self.assertIn('python -c "print(1 > 0)"', rewritten)
        self.assertIn(str(overlay), rewritten)

    def test_rewrite_shell_line_ignores_quoted_comparison_without_redirect(self):
        rewritten, overlay = self.rewrite_with_temp_project('test "10 > 2" = "10 > 2"')
        self.assertEqual(rewritten, 'test "10 > 2" = "10 > 2"')
        self.assertFalse(overlay.exists())

    def test_rewrite_shell_line_does_not_initialize_sandbox_profile(self):
        with tempfile.TemporaryDirectory() as td:
            old = macbox_cli.project_root
            macbox_cli.project_root = lambda: Path(td)
            try:
                rewritten = macbox_cli.rewrite_shell_line("alpha", "echo hi > /tmp/out.txt")
                self.assertIn(".macbox/sessions/alpha/overlay", rewritten)
                self.assertFalse((Path(td) / ".macbox/sessions/alpha/profile.sb").exists())
            finally:
                macbox_cli.project_root = old

    def test_rewrite_shell_line_leaves_plain_commands_unchanged(self):
        rewritten, overlay = self.rewrite_with_temp_project("mkdir 111 && echo done")
        self.assertEqual(rewritten, "mkdir 111 && echo done")
        self.assertFalse(overlay.exists())

    def test_shell_rc_aliases_use_current_python_without_env_lookup(self):
        with tempfile.TemporaryDirectory() as td:
            old = macbox_cli.project_root
            macbox_cli.project_root = lambda: Path(td)
            try:
                rc = macbox_cli.shell_rc("alpha")
                self.assertIn("alias mb-changes=", rc)
                self.assertIn("alias mb-apply=", rc)
                self.assertIn("vpath()", rc)
                self.assertIn("mkdir() {", rc)
                self.assertIn("touch() {", rc)
                self.assertIn("cat() {", rc)
                self.assertIn(macbox_cli.sys.executable, rc)
                self.assertNotIn("/usr/bin/env python3", rc)
            finally:
                macbox_cli.project_root = old

    def test_prototype_backend_contract_create_changes_apply_discard(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as allowed:
            old = macbox_cli.project_root
            macbox_cli.project_root = lambda: Path(project)
            try:
                backend = macbox_cli.sandbox_backend()
                self.assertEqual(backend.name, "prototype")
                backend.create("contract", writes=[allowed])

                real = Path(allowed) / "contract.txt"
                staged = backend.real_to_virtual("contract", str(real))
                staged.parent.mkdir(parents=True)
                staged.write_text("contract")

                changes = backend.list_changes("contract")
                self.assertEqual(len(changes), 1)
                self.assertEqual(changes[0]["realPath"], str(macbox_cli.normalize_abs(str(real))))

                applied, backup = backend.apply("contract", clear=False)
                self.assertEqual(applied, 1)
                self.assertIsNotNone(backup)
                self.assertEqual(real.read_text(), "contract")

                staged.write_text("discard")
                self.assertTrue(backend.list_changes("contract"))
                backend.discard("contract")
                self.assertEqual(backend.list_changes("contract"), [])
            finally:
                macbox_cli.project_root = old

    def test_prototype_backend_contract_delete_marker(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as allowed:
            old = macbox_cli.project_root
            macbox_cli.project_root = lambda: Path(project)
            try:
                backend = macbox_cli.sandbox_backend()
                real = Path(allowed) / "delete-contract.txt"
                real.write_text("delete")
                backend.create("delete-contract", writes=[allowed])

                marked = backend.mark_delete("delete-contract", str(real))
                self.assertEqual(marked, macbox_cli.normalize_abs(str(real)))
                changes = backend.list_changes("delete-contract")
                self.assertEqual(changes[0]["change"], "delete")

                applied, _ = backend.apply("delete-contract", clear=True)
                self.assertEqual(applied, 1)
                self.assertFalse(real.exists())
                self.assertEqual(backend.list_changes("delete-contract"), [])
            finally:
                macbox_cli.project_root = old

    def test_prototype_backend_contract_launch_specs(self):
        with tempfile.TemporaryDirectory() as project:
            old = macbox_cli.project_root
            macbox_cli.project_root = lambda: Path(project)
            try:
                backend = macbox_cli.sandbox_backend()
                backend.create("launch")

                shell = backend.prepare_shell("launch", ["/bin/zsh", "-lc", "echo hi > relative.txt"])
                self.assertEqual(shell.argv[:3], ["sandbox-exec", "-f", str(macbox_cli.profile_path("launch"))])
                self.assertEqual(shell.cwd, Path(project))
                self.assertIn("relative.txt", shell.display_command)
                self.assertIn(".macbox/sessions/launch/overlay", shell.argv[-1])

                app = backend.prepare_app("launch", Path("/bin/echo"), ["hello"])
                self.assertEqual(app.argv[:3], ["sandbox-exec", "-f", str(macbox_cli.profile_path("launch"))])
                self.assertEqual(app.argv[-2:], ["/bin/echo", "hello"])

                terminal_command = backend.open_terminal_command("launch")
                self.assertIn("macbox", terminal_command)
                self.assertIn("session --name", terminal_command)
            finally:
                macbox_cli.project_root = old

    def test_macfuse_status_shape(self):
        status = macbox_cli.macfuse_status()
        self.assertIn("available", status)
        self.assertIn("filesystem", status)
        self.assertIn("framework", status)
        self.assertIn("mountCommand", status)
        self.assertIn("pythonBinding", status)

    def test_backend_status_declares_arbitrary_paths_not_ready(self):
        old_status = macbox_cli.macfuse_status
        macbox_cli.macfuse_status = lambda: {
            "available": False,
            "filesystem": None,
            "framework": None,
            "mountCommand": None,
            "pythonBinding": False,
        }
        try:
            status = macbox_cli.backend_status()
            self.assertEqual(status["productionBackend"], "fuse")
            self.assertTrue(status["arbitraryVirtualPaths"]["required"])
            self.assertFalse(status["arbitraryVirtualPaths"]["ready"])
            self.assertTrue(status["arbitraryVirtualPaths"]["readOnlyMountImplemented"])
            self.assertTrue(status["arbitraryVirtualPaths"]["writeOverlayImplemented"])
            self.assertFalse(status["arbitraryVirtualPaths"]["sessionExecutionImplemented"])
            self.assertIn("workspace-only", status["arbitraryVirtualPaths"]["note"])
        finally:
            macbox_cli.macfuse_status = old_status

    def test_backend_doctor_reports_missing_macfuse_as_blocking(self):
        old_status = macbox_cli.macfuse_status
        macbox_cli.macfuse_status = lambda: {
            "available": False,
            "filesystem": None,
            "framework": None,
            "mountCommand": None,
            "pythonBinding": False,
        }
        try:
            report = macbox_cli.backend_doctor_report()
            self.assertFalse(report["ready"])
            blocking_ids = {check["id"] for check in report["checks"] if not check["ok"] and check["severity"] == "blocking"}
            self.assertIn("arbitrary-virtual-paths", blocking_ids)
            self.assertIn("macfuse-installed", blocking_ids)
            self.assertIn("python-fuse-binding", blocking_ids)
            self.assertTrue(any("Install macFUSE" in action for action in report["nextActions"]))
            self.assertTrue(any("session execution" in action for action in report["nextActions"]))
        finally:
            macbox_cli.macfuse_status = old_status

    def test_backend_doctor_reports_implementation_gap_after_dependencies(self):
        old_status = macbox_cli.macfuse_status
        macbox_cli.macfuse_status = lambda: {
            "available": True,
            "filesystem": "/Library/Filesystems/macfuse.fs",
            "framework": None,
            "mountCommand": "/usr/local/bin/mount_macfuse",
            "pythonBinding": True,
        }
        try:
            report = macbox_cli.backend_doctor_report()
            self.assertFalse(report["ready"])
            blocking_ids = {check["id"] for check in report["checks"] if not check["ok"] and check["severity"] == "blocking"}
            self.assertEqual(blocking_ids, {"arbitrary-virtual-paths"})
            self.assertTrue(any("session execution" in action for action in report["nextActions"]))
            self.assertFalse(report["installPlan"]["backendReady"])
            self.assertTrue(report["installPlan"]["macfuseInstalled"])
        finally:
            macbox_cli.macfuse_status = old_status

    def test_backend_install_plan_prefers_brew_when_requested(self):
        old_status = macbox_cli.macfuse_status
        macbox_cli.macfuse_status = lambda: {
            "available": False,
            "filesystem": None,
            "framework": None,
            "mountCommand": None,
            "pythonBinding": False,
        }
        try:
            with mock.patch("macbox_cli.shutil.which", return_value="/opt/homebrew/bin/brew"):
                plan = macbox_cli.backend_install_plan(use_brew=True)
            self.assertEqual(plan["backend"], "macfuse")
            self.assertEqual(plan["steps"][0]["type"], "command")
            self.assertIn("install --cask macfuse", plan["steps"][0]["command"])
            self.assertTrue(plan["requiresAdminApproval"])
            self.assertFalse(plan["backendReady"])
            self.assertFalse(plan["macfuseInstalled"])
        finally:
            macbox_cli.macfuse_status = old_status

    def test_backend_install_execute_requires_explicit_brew(self):
        args = Namespace(backend="macfuse", use_brew=False, dry_run=False, open=False, execute=True, json=False)
        with self.assertRaises(SystemExit) as ctx:
            macbox_cli.cmd_backend_install(args)
        self.assertIn("--use-brew", str(ctx.exception))

    def test_backend_install_rejects_open_and_execute_together(self):
        args = Namespace(backend="macfuse", use_brew=True, dry_run=False, open=True, execute=True, json=False)
        with self.assertRaises(SystemExit) as ctx:
            macbox_cli.cmd_backend_install(args)
        self.assertIn("cannot be used together", str(ctx.exception))

    def test_backend_install_rejects_json_with_action_flags(self):
        args = Namespace(backend="macfuse", use_brew=True, dry_run=False, open=False, execute=True, json=True)
        with self.assertRaises(SystemExit) as ctx:
            macbox_cli.cmd_backend_install(args)
        self.assertIn("--json", str(ctx.exception))

    def test_backend_install_execute_runs_brew_when_explicit(self):
        args = Namespace(backend="macfuse", use_brew=True, dry_run=False, open=False, execute=True, json=False)
        old_status = macbox_cli.macfuse_status
        macbox_cli.macfuse_status = lambda: {
            "available": False,
            "filesystem": None,
            "framework": None,
            "mountCommand": None,
            "pythonBinding": False,
        }
        try:
            with mock.patch("macbox_cli.shutil.which", return_value="/opt/homebrew/bin/brew"), \
                    mock.patch("macbox_cli.subprocess.run") as run:
                run.return_value.returncode = 0
                rc = macbox_cli.cmd_backend_install(args)
            self.assertEqual(rc, 0)
            run.assert_called_once_with(["/opt/homebrew/bin/brew", "install", "--cask", "macfuse"])
        finally:
            macbox_cli.macfuse_status = old_status

    def test_setup_dry_run_does_not_start_installers(self):
        args = Namespace(dry_run=True, open=True, use_brew=True, install_python_binding=True, yes=True)
        old_status = macbox_cli.macfuse_status
        macbox_cli.macfuse_status = lambda: {
            "available": False,
            "filesystem": None,
            "framework": None,
            "mountCommand": None,
            "pythonBinding": False,
        }
        try:
            with mock.patch("macbox_cli.shutil.which", return_value="/opt/homebrew/bin/brew"), \
                    mock.patch("macbox_cli.subprocess.run") as run:
                self.assertEqual(macbox_cli.cmd_setup(args), 0)
            run.assert_not_called()
        finally:
            macbox_cli.macfuse_status = old_status

    def test_setup_yes_runs_brew_but_requires_explicit_python_binding_flag(self):
        args = Namespace(dry_run=False, open=False, use_brew=True, install_python_binding=False, yes=True)
        old_status = macbox_cli.macfuse_status
        macbox_cli.macfuse_status = lambda: {
            "available": False,
            "filesystem": None,
            "framework": None,
            "mountCommand": None,
            "pythonBinding": False,
        }
        try:
            with mock.patch("macbox_cli.shutil.which", return_value="/opt/homebrew/bin/brew"), \
                    mock.patch("macbox_cli.subprocess.run") as run, \
                    mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                run.return_value.returncode = 0
                self.assertEqual(macbox_cli.cmd_setup(args), 0)
            run.assert_any_call(["/opt/homebrew/bin/brew", "install", "--cask", "macfuse"])
            self.assertEqual(run.call_count, 1)
            self.assertIn("--install-python-binding --yes", out.getvalue())
        finally:
            macbox_cli.macfuse_status = old_status

    def test_setup_explicit_python_binding_install_failure_returns_failure(self):
        args = Namespace(dry_run=False, open=False, use_brew=False, install_python_binding=True, yes=False)
        old_status = macbox_cli.macfuse_status
        macbox_cli.macfuse_status = lambda: {
            "available": True,
            "filesystem": "/Library/Filesystems/macfuse.fs",
            "framework": None,
            "mountCommand": "/usr/local/bin/mount_macfuse",
            "pythonBinding": False,
        }
        try:
            with mock.patch("macbox_cli.shutil.which", return_value="/opt/homebrew/bin/brew"), \
                    mock.patch("macbox_cli.subprocess.run") as run:
                run.return_value.returncode = 42
                self.assertEqual(macbox_cli.cmd_setup(args), 42)
            run.assert_called_once_with([macbox_cli.sys.executable, "-m", "pip", "install", "fusepy"])
        finally:
            macbox_cli.macfuse_status = old_status

    def test_setup_yes_official_guide_path_does_not_prompt(self):
        args = Namespace(dry_run=False, open=False, use_brew=False, install_python_binding=False, yes=True)
        old_status = macbox_cli.macfuse_status
        macbox_cli.macfuse_status = lambda: {
            "available": False,
            "filesystem": None,
            "framework": None,
            "mountCommand": None,
            "pythonBinding": True,
        }
        try:
            with mock.patch("macbox_cli.shutil.which", return_value="/opt/homebrew/bin/brew"), \
                    mock.patch("macbox_cli.prompt_yes_no") as prompt, \
                    mock.patch("macbox_cli.subprocess.run") as run:
                self.assertEqual(macbox_cli.cmd_setup(args), 0)
            prompt.assert_not_called()
            run.assert_not_called()
        finally:
            macbox_cli.macfuse_status = old_status

    def test_fuse_backend_mount_reports_unavailable(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as mount:
            old_project_root = macbox_cli.project_root
            old_status = macbox_cli.macfuse_status
            macbox_cli.project_root = lambda: Path(project)
            macbox_cli.macfuse_status = lambda: {
                "available": False,
                "filesystem": None,
                "framework": None,
                "mountCommand": None,
                "pythonBinding": False,
            }
            try:
                with self.assertRaises(SystemExit) as ctx:
                    macbox_cli.fuse_backend().mount_readonly("fuse-unavailable", mount)
                self.assertIn("macFUSE is not available", str(ctx.exception))
            finally:
                macbox_cli.project_root = old_project_root
                macbox_cli.macfuse_status = old_status

    def test_fuse_backend_mount_reports_missing_python_binding(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as mount:
            old_project_root = macbox_cli.project_root
            old_status = macbox_cli.macfuse_status
            macbox_cli.project_root = lambda: Path(project)
            macbox_cli.macfuse_status = lambda: {
                "available": True,
                "filesystem": "/Library/Filesystems/macfuse.fs",
                "framework": None,
                "mountCommand": "/usr/local/bin/mount_macfuse",
                "pythonBinding": False,
            }
            try:
                with self.assertRaises(SystemExit) as ctx:
                    macbox_cli.fuse_backend().mount_readonly("fuse-no-binding", mount)
                self.assertIn("mounted FUSE overlay backend is disabled", str(ctx.exception))
            finally:
                macbox_cli.project_root = old_project_root
                macbox_cli.macfuse_status = old_status

    def test_fuse_readonly_operations_read_real_absolute_paths(self):
        with tempfile.TemporaryDirectory() as td:
            real = Path(td) / "readable.txt"
            real.write_text("hello fuse")
            ops = macbox_fuse.ReadOnlyMirrorOperations()

            attrs = ops.getattr(str(real))
            self.assertEqual(attrs["st_size"], len("hello fuse"))
            fh = ops.open(str(real), 0)
            try:
                self.assertEqual(ops.read(str(real), 5, 0, fh), b"hello")
            finally:
                ops.release(str(real), fh)
            self.assertIn("readable.txt", ops.readdir(td, None))
            with self.assertRaises(OSError):
                ops.mkdir(str(Path(td) / "blocked"), 0o755)

    def test_fuse_access_reports_missing_paths_as_enoent(self):
        ops = macbox_fuse.ReadOnlyMirrorOperations()
        with self.assertRaises(OSError) as ctx:
            ops.access("/definitely/missing/macbox/path", os.F_OK)
        self.assertEqual(ctx.exception.errno, errno.ENOENT)
        with self.assertRaises(OSError) as ctx:
            ops.access("/definitely/missing/macbox/path", os.W_OK)
        self.assertEqual(ctx.exception.errno, errno.ENOENT)

    def test_fuse_overlay_create_write_stages_without_real_write(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as real_dir:
            overlay = Path(project) / "overlay"
            deletes = Path(project) / "deletes.txt"
            real = Path(real_dir) / "new.txt"
            ops = macbox_fuse.ReadOnlyMirrorOperations(overlay_root=overlay, deletes_file=deletes)

            fh = ops.create(str(real), 0o644)
            try:
                self.assertEqual(ops.write(str(real), b"overlay", 0, fh), len(b"overlay"))
            finally:
                ops.release(str(real), fh)

            staged = overlay / str(real.resolve(strict=False)).lstrip("/")
            self.assertEqual(staged.read_text(), "overlay")
            self.assertFalse(real.exists())
            self.assertEqual(ops.getattr(str(real))["st_size"], len("overlay"))

    def test_fuse_overlay_failed_write_open_does_not_leave_copy_up(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as real_dir:
            overlay = Path(project) / "overlay"
            real = Path(real_dir) / "real.txt"
            real.write_text("real")
            ops = macbox_fuse.ReadOnlyMirrorOperations(overlay_root=overlay, deletes_file=Path(project) / "deletes.txt")

            with self.assertRaises(OSError) as ctx:
                ops.open(str(real), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            self.assertEqual(ctx.exception.errno, errno.EEXIST)
            self.assertFalse((overlay / str(real.resolve(strict=False)).lstrip("/")).exists())

    def test_fuse_overlay_mkdir_can_recreate_tombstoned_path(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as real_dir:
            real = Path(real_dir) / "replace-me"
            real.write_text("real")
            overlay = Path(project) / "overlay"
            deletes = Path(project) / "deletes.txt"
            ops = macbox_fuse.ReadOnlyMirrorOperations(overlay_root=overlay, deletes_file=deletes)

            ops.unlink(str(real))
            ops.mkdir(str(real), 0o755)

            staged = overlay / str(real.resolve(strict=False)).lstrip("/")
            self.assertTrue(staged.is_dir())
            self.assertTrue(real.exists())

    def test_fuse_overlay_recreated_tombstoned_entries_can_be_deleted_again(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as real_dir:
            file_path = Path(real_dir) / "replace-file"
            file_path.write_text("real")
            dir_path = Path(real_dir) / "replace-dir"
            dir_path.mkdir()
            overlay = Path(project) / "overlay"
            ops = macbox_fuse.ReadOnlyMirrorOperations(overlay_root=overlay, deletes_file=Path(project) / "deletes.txt")

            ops.unlink(str(file_path))
            fh = ops.create(str(file_path), 0o644)
            ops.release(str(file_path), fh)
            ops.unlink(str(file_path))
            self.assertFalse((overlay / str(file_path.resolve(strict=False)).lstrip("/")).exists())

            ops.rmdir(str(dir_path))
            ops.mkdir(str(dir_path), 0o755)
            ops.rmdir(str(dir_path))
            self.assertFalse((overlay / str(dir_path.resolve(strict=False)).lstrip("/")).exists())

    def test_fuse_overlay_read_prefers_staged_file(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as real_dir:
            overlay = Path(project) / "overlay"
            real = Path(real_dir) / "file.txt"
            real.write_text("real")
            staged = overlay / str(real.resolve(strict=False)).lstrip("/")
            staged.parent.mkdir(parents=True)
            staged.write_text("staged")
            ops = macbox_fuse.ReadOnlyMirrorOperations(overlay_root=overlay, deletes_file=Path(project) / "deletes.txt")

            fh = ops.open(str(real), os.O_RDONLY)
            try:
                self.assertEqual(ops.read(str(real), 20, 0, fh), b"staged")
            finally:
                ops.release(str(real), fh)

    def test_fuse_overlay_unlink_marks_tombstone_and_hides_real_file(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as real_dir:
            real = Path(real_dir) / "delete.txt"
            real.write_text("real")
            deletes = Path(project) / "deletes.txt"
            ops = macbox_fuse.ReadOnlyMirrorOperations(overlay_root=Path(project) / "overlay", deletes_file=deletes)

            ops.unlink(str(real))

            self.assertTrue(real.exists())
            self.assertIn(str(real.resolve(strict=False)), deletes.read_text())
            with self.assertRaises(OSError) as ctx:
                ops.getattr(str(real))
            self.assertEqual(ctx.exception.errno, errno.ENOENT)

    def test_fuse_overlay_deleted_directory_hides_children(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as real_dir:
            real = Path(real_dir) / "dir"
            real.mkdir()
            child = real / "child.txt"
            child.write_text("child")
            deletes = Path(project) / "deletes.txt"
            deletes.write_text(str(real.resolve(strict=False)) + "\n")
            ops = macbox_fuse.ReadOnlyMirrorOperations(overlay_root=Path(project) / "overlay", deletes_file=deletes)

            with self.assertRaises(OSError) as ctx:
                ops.getattr(str(child))
            self.assertEqual(ctx.exception.errno, errno.ENOENT)
            with self.assertRaises(OSError) as ctx:
                ops.readdir(str(real), None)
            self.assertEqual(ctx.exception.errno, errno.ENOENT)

    def test_fuse_overlay_rejects_unsafe_directory_deletes_and_existing_mkdir(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as real_dir:
            root = Path(real_dir)
            directory = root / "dir"
            directory.mkdir()
            (directory / "child.txt").write_text("child")
            file_path = root / "file.txt"
            file_path.write_text("file")
            ops = macbox_fuse.ReadOnlyMirrorOperations(overlay_root=Path(project) / "overlay", deletes_file=Path(project) / "deletes.txt")

            with self.assertRaises(OSError) as ctx:
                ops.unlink(str(directory))
            self.assertEqual(ctx.exception.errno, errno.EISDIR)

            with self.assertRaises(OSError) as ctx:
                ops.rmdir(str(directory))
            self.assertEqual(ctx.exception.errno, errno.ENOTEMPTY)

            with self.assertRaises(OSError) as ctx:
                ops.mkdir(str(file_path), 0o755)
            self.assertEqual(ctx.exception.errno, errno.EEXIST)

            with self.assertRaises(OSError) as ctx:
                ops.mkdir(str(directory), 0o755)
            self.assertEqual(ctx.exception.errno, errno.EEXIST)

    def test_fuse_overlay_rmdir_uses_virtual_children_after_tombstones(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as real_dir:
            directory = Path(real_dir) / "dir"
            directory.mkdir()
            child = directory / "child.txt"
            child.write_text("child")
            deletes = Path(project) / "deletes.txt"
            ops = macbox_fuse.ReadOnlyMirrorOperations(overlay_root=Path(project) / "overlay", deletes_file=deletes)

            ops.unlink(str(child))
            self.assertEqual([entry for entry in ops.readdir(str(directory), None) if entry not in (".", "..")], [])
            ops.rmdir(str(directory))

            deleted = deletes.read_text()
            self.assertIn(str(child.resolve(strict=False)), deleted)
            self.assertIn(str(directory.resolve(strict=False)), deleted)

    def test_fuse_overlay_rename_stages_new_path_and_tombstones_old_path(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as real_dir:
            old = Path(real_dir) / "old.txt"
            new = Path(real_dir) / "new.txt"
            old.write_text("real")
            overlay = Path(project) / "overlay"
            deletes = Path(project) / "deletes.txt"
            ops = macbox_fuse.ReadOnlyMirrorOperations(overlay_root=overlay, deletes_file=deletes)

            ops.rename(str(old), str(new))

            staged_new = overlay / str(new.resolve(strict=False)).lstrip("/")
            self.assertEqual(staged_new.read_text(), "real")
            self.assertTrue(old.exists())
            self.assertFalse(new.exists())
            self.assertIn(str(old.resolve(strict=False)), deletes.read_text())

    def test_fuse_overlay_write_to_symlink_does_not_modify_real_target(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as real_dir:
            target = Path(real_dir) / "target.txt"
            target.write_text("real")
            link = Path(real_dir) / "link.txt"
            os.symlink(str(target), link)
            overlay = Path(project) / "overlay"
            ops = macbox_fuse.ReadOnlyMirrorOperations(overlay_root=overlay, deletes_file=Path(project) / "deletes.txt")

            fh = ops.open(str(link), os.O_WRONLY | os.O_TRUNC)
            try:
                ops.write(str(link), b"virtual", 0, fh)
            finally:
                ops.release(str(link), fh)

            staged = overlay / str((link.parent.resolve(strict=False) / link.name)).lstrip("/")
            self.assertEqual(staged.read_text(), "virtual")
            self.assertFalse(staged.is_symlink())
            self.assertEqual(target.read_text(), "real")

    def test_fuse_backend_mount_starts_helper_and_records_metadata(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as mount:
            old_project_root = macbox_cli.project_root
            old_status = macbox_cli.macfuse_status
            macbox_cli.project_root = lambda: Path(project)
            macbox_cli.macfuse_status = lambda: {
                "available": True,
                "filesystem": "/Library/Filesystems/macfuse.fs",
                "framework": None,
                "mountCommand": "/usr/local/bin/mount_macfuse",
                "pythonBinding": True,
            }
            helper = Path(project) / "macbox_fuse.py"
            helper.write_text("# helper")

            class FakeProcess:
                pid = 4242

                def poll(self):
                    return None

            try:
                with mock.patch("macbox_cli.subprocess.Popen", return_value=FakeProcess()) as popen, \
                        mock.patch("macbox_cli.wait_for_mount", return_value=True):
                    mounted = macbox_cli.fuse_backend().mount_readonly("mounted", mount)

                self.assertEqual(mounted, macbox_cli.normalize_abs(mount))
                metadata = macbox_cli.read_metadata("mounted")
                self.assertEqual(metadata["status"], "mounted")
                self.assertEqual(metadata["mountPath"], str(macbox_cli.normalize_abs(mount)))
                self.assertEqual(metadata["mountPid"], 4242)
                self.assertFalse(metadata["readOnly"])
                command = popen.call_args.args[0]
                self.assertIn(str(helper), command)
                self.assertIn("--session", command)
                self.assertIn("--mount", command)
                self.assertIn("--overlay", command)
                self.assertIn("--deletes", command)
                self.assertIn("--foreground", command)
                self.assertTrue(macbox_cli.mount_record_path("mounted").exists())
            finally:
                macbox_cli.project_root = old_project_root
                macbox_cli.macfuse_status = old_status

    def test_fuse_backend_foreground_timeout_terminates_helper(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as mount:
            old_project_root = macbox_cli.project_root
            old_status = macbox_cli.macfuse_status
            macbox_cli.project_root = lambda: Path(project)
            macbox_cli.macfuse_status = lambda: {
                "available": True,
                "filesystem": "/Library/Filesystems/macfuse.fs",
                "framework": None,
                "mountCommand": "/usr/local/bin/mount_macfuse",
                "pythonBinding": True,
            }
            (Path(project) / "macbox_fuse.py").write_text("# helper")

            class FakeProcess:
                pid = 4242
                terminated = False

                def poll(self):
                    return None

                def terminate(self):
                    self.terminated = True

            proc = FakeProcess()
            try:
                with mock.patch("macbox_cli.subprocess.Popen", return_value=proc), \
                        mock.patch("macbox_cli.wait_for_mount", return_value=False):
                    with self.assertRaises(SystemExit):
                        macbox_cli.fuse_backend().mount_readonly("foreground-timeout", mount, foreground=True)
                self.assertTrue(proc.terminated)
            finally:
                macbox_cli.project_root = old_project_root
                macbox_cli.macfuse_status = old_status

    def test_fuse_backend_foreground_exit_removes_mount_record(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as mount:
            old_project_root = macbox_cli.project_root
            old_status = macbox_cli.macfuse_status
            macbox_cli.project_root = lambda: Path(project)
            macbox_cli.macfuse_status = lambda: {
                "available": True,
                "filesystem": "/Library/Filesystems/macfuse.fs",
                "framework": None,
                "mountCommand": "/usr/local/bin/mount_macfuse",
                "pythonBinding": True,
            }
            (Path(project) / "macbox_fuse.py").write_text("# helper")

            class FakeProcess:
                pid = 4242

                def poll(self):
                    return None

                def wait(self):
                    return 0

            try:
                with mock.patch("macbox_cli.subprocess.Popen", return_value=FakeProcess()), \
                        mock.patch("macbox_cli.wait_for_mount", return_value=True):
                    macbox_cli.fuse_backend().mount_readonly("foreground-exit", mount, foreground=True)
                metadata = macbox_cli.read_metadata("foreground-exit")
                self.assertEqual(metadata["status"], "idle")
                self.assertIsNone(metadata["mountPath"])
                self.assertFalse(macbox_cli.mount_record_path("foreground-exit").exists())
            finally:
                macbox_cli.project_root = old_project_root
                macbox_cli.macfuse_status = old_status

    def test_cmd_mount_foreground_reports_exit_not_mounted(self):
        args = Namespace(backend="fuse", name="fg", mount="/tmp/fg", read=None, write=None, foreground=True)
        out = io.StringIO()
        with mock.patch("macbox_cli.fuse_backend") as backend_factory, redirect_stdout(out):
            backend_factory.return_value.mount_readonly.return_value = Path("/tmp/fg")
            rc = macbox_cli.cmd_mount(args)
        self.assertEqual(rc, 0)
        self.assertIn("foreground mount exited", out.getvalue())
        self.assertNotIn("mounted fg", out.getvalue())

    def test_fuse_backend_unmount_preserves_metadata_on_failure(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as mount:
            old_project_root = macbox_cli.project_root
            macbox_cli.project_root = lambda: Path(project)
            try:
                macbox_cli.write_metadata("mounted", backend="fuse", status="mounted", mountPath=str(mount), mountPid=4242, readOnly=True)
                failed = mock.Mock()
                failed.returncode = 1
                failed.stderr = "busy"
                failed.stdout = ""
                with mock.patch("macbox_cli.subprocess.run", return_value=failed):
                    with self.assertRaises(SystemExit) as ctx:
                        macbox_cli.fuse_backend().unmount("mounted")
                self.assertIn("failed to unmount", str(ctx.exception))
                metadata = macbox_cli.read_metadata("mounted")
                self.assertEqual(metadata["mountPath"], mount)
                self.assertEqual(metadata["status"], "mounted")
            finally:
                macbox_cli.project_root = old_project_root

    def test_fuse_backend_unmount_removes_mount_record_on_success(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as mount:
            old_project_root = macbox_cli.project_root
            macbox_cli.project_root = lambda: Path(project)
            try:
                macbox_cli.write_metadata("mounted", backend="fuse", status="mounted", mountPath=str(mount), mountPid=4242, readOnly=True)
                macbox_cli.mount_record_path("mounted").parent.mkdir(parents=True, exist_ok=True)
                macbox_cli.mount_record_path("mounted").write_text("{}")
                ok = mock.Mock()
                ok.returncode = 0
                ok.stderr = ""
                ok.stdout = ""
                with mock.patch("macbox_cli.subprocess.run", return_value=ok):
                    macbox_cli.fuse_backend().unmount("mounted")
                metadata = macbox_cli.read_metadata("mounted")
                self.assertIsNone(metadata["mountPath"])
                self.assertEqual(metadata["status"], "idle")
                self.assertFalse(macbox_cli.mount_record_path("mounted").exists())
            finally:
                macbox_cli.project_root = old_project_root

    def test_fuse_readlink_rewrites_absolute_targets_into_virtual_root(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "target.txt"
            target.write_text("target")
            link = Path(td) / "absolute-link"
            os.symlink(str(target), link)
            virtual_root = Path("/tmp/macbox-virtual-root")
            ops = macbox_fuse.ReadOnlyMirrorOperations(virtual_root)
            self.assertEqual(ops.readlink(str(link)), str(virtual_root / str(target).lstrip("/")))

    def test_apply_writes_only_configured_write_root(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as allowed:
            old = macbox_cli.project_root
            macbox_cli.project_root = lambda: Path(project)
            try:
                real = Path(allowed) / "applied.txt"
                macbox_cli.ensure_sandbox("apply-allowed", writes=[allowed])
                pending = macbox_cli.virtual_path("apply-allowed", str(real))
                pending.parent.mkdir(parents=True)
                pending.write_text("virtual")

                rc = macbox_cli.cmd_apply(Namespace(name="apply-allowed", clear=False))

                self.assertEqual(rc, 0)
                self.assertEqual(real.read_text(), "virtual")
            finally:
                macbox_cli.project_root = old

    def test_apply_refuses_write_outside_configured_write_root(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as allowed, tempfile.TemporaryDirectory() as denied:
            old = macbox_cli.project_root
            macbox_cli.project_root = lambda: Path(project)
            try:
                real = Path(denied) / "blocked.txt"
                macbox_cli.ensure_sandbox("apply-denied", writes=[allowed])
                pending = macbox_cli.virtual_path("apply-denied", str(real))
                pending.parent.mkdir(parents=True)
                pending.write_text("blocked")

                with self.assertRaises(SystemExit) as ctx:
                    macbox_cli.cmd_apply(Namespace(name="apply-denied", clear=False))

                self.assertIn("refusing write outside configured write roots", str(ctx.exception))
                self.assertFalse(real.exists())
            finally:
                macbox_cli.project_root = old

    def test_delete_marker_is_applied_with_backup(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as allowed:
            old = macbox_cli.project_root
            macbox_cli.project_root = lambda: Path(project)
            try:
                real = Path(allowed) / "delete-me.txt"
                real.write_text("original")
                macbox_cli.ensure_sandbox("delete-marker", writes=[allowed])

                macbox_cli.cmd_delete(Namespace(name="delete-marker", real_path=str(real)))
                changes = macbox_cli.collect_changes("delete-marker")
                self.assertEqual(changes[0]["change"], "delete")
                self.assertEqual(changes[0]["realPath"], str(macbox_cli.normalize_abs(str(real))))

                rc = macbox_cli.cmd_apply(Namespace(name="delete-marker", clear=False))

                self.assertEqual(rc, 0)
                self.assertFalse(real.exists())
                backups = list((Path(project) / ".macbox/sessions/delete-marker/backups").rglob("delete-me.txt"))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_text(), "original")
            finally:
                macbox_cli.project_root = old

    def test_apply_refuses_delete_outside_configured_write_root(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as allowed, tempfile.TemporaryDirectory() as denied:
            old = macbox_cli.project_root
            macbox_cli.project_root = lambda: Path(project)
            try:
                real = Path(denied) / "blocked-delete.txt"
                real.write_text("keep")
                macbox_cli.ensure_sandbox("delete-denied", writes=[allowed])
                macbox_cli.cmd_delete(Namespace(name="delete-denied", real_path=str(real)))

                with self.assertRaises(SystemExit) as ctx:
                    macbox_cli.cmd_apply(Namespace(name="delete-denied", clear=False))

                self.assertIn("refusing delete outside configured write roots", str(ctx.exception))
                self.assertEqual(real.read_text(), "keep")
            finally:
                macbox_cli.project_root = old

    def test_apply_clear_removes_overlay_and_delete_markers(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as allowed:
            old = macbox_cli.project_root
            macbox_cli.project_root = lambda: Path(project)
            try:
                real = Path(allowed) / "clear-write.txt"
                to_delete = Path(allowed) / "clear-delete.txt"
                to_delete.write_text("remove")
                macbox_cli.ensure_sandbox("clear-overlay", writes=[allowed])
                pending = macbox_cli.virtual_path("clear-overlay", str(real))
                pending.parent.mkdir(parents=True)
                pending.write_text("write")
                macbox_cli.cmd_delete(Namespace(name="clear-overlay", real_path=str(to_delete)))

                rc = macbox_cli.cmd_apply(Namespace(name="clear-overlay", clear=True))

                self.assertEqual(rc, 0)
                self.assertEqual(real.read_text(), "write")
                self.assertFalse(to_delete.exists())
                self.assertEqual(list(macbox_cli.overlay_root("clear-overlay").rglob("*")), [])
                self.assertEqual(macbox_cli.deletes_path("clear-overlay").read_text(), "")
            finally:
                macbox_cli.project_root = old


if __name__ == "__main__":
    unittest.main()
