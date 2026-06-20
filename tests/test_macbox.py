import tempfile
import unittest
from pathlib import Path

import macbox_cli


class MacBoxTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
