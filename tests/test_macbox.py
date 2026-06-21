import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

import macbox_cli


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
