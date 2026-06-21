import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class MacBoxIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        probe = subprocess.run(
            ["sandbox-exec", "-p", "(version 1)\n(allow default)\n", "/usr/bin/true"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if probe.returncode != 0:
            raise unittest.SkipTest(f"sandbox-exec unavailable in this test context: {probe.stderr.strip()}")

    def run_macbox(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(ROOT / "macbox"), *args],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_sandbox_redirects_absolute_tmp_write_to_overlay(self):
        name = f"it-{uuid.uuid4().hex[:8]}"
        real = Path(tempfile.gettempdir()) / f"macbox-{uuid.uuid4().hex}.txt"
        overlay = ROOT / ".macbox" / "sessions" / name / "overlay" / str(real.resolve(strict=False)).lstrip("/")
        shutil.rmtree(ROOT / ".macbox" / "sessions" / name, ignore_errors=True)
        real.unlink(missing_ok=True)

        result = self.run_macbox(
            "run",
            "--name",
            name,
            "--",
            "/bin/zsh",
            "-lc",
            f'echo virtual > "{real}" && read line < "{real}" && echo inside=$line',
        )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("inside=virtual", result.stdout)
        self.assertFalse(real.exists(), f"real file should not be written: {real}")
        self.assertEqual(overlay.read_text().strip(), "virtual")

        changes = self.run_macbox("changes", "--name", name)
        self.assertEqual(changes.returncode, 0, changes.stderr + changes.stdout)
        self.assertIn(str(real.resolve(strict=False)), changes.stdout)

    def test_rewrite_subcommand_runs_inside_sandbox_without_profile_write(self):
        name = f"it-{uuid.uuid4().hex[:8]}"
        shutil.rmtree(ROOT / ".macbox" / "sessions" / name, ignore_errors=True)

        result = self.run_macbox(
            "run",
            "--name",
            name,
            "--",
            "/bin/zsh",
            "-lc",
            f'"{os.sys.executable}" macbox_cli.py rewrite --name {name} -- "echo ok > /tmp/macbox-hook.txt"',
        )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertNotIn("PermissionError", result.stderr + result.stdout)
        self.assertIn(f".macbox/sessions/{name}/overlay", result.stdout)

    def test_interactive_session_redirects_relative_writes_after_mkdir(self):
        name = f"it-{uuid.uuid4().hex[:8]}"
        relative_dir = f"macbox-it-{uuid.uuid4().hex[:8]}"
        real_dir = ROOT / relative_dir
        session_root = ROOT / ".macbox" / "sessions" / name
        overlay_dir = session_root / "overlay" / str(real_dir.resolve(strict=False)).lstrip("/")
        shutil.rmtree(session_root, ignore_errors=True)
        shutil.rmtree(real_dir, ignore_errors=True)

        result = subprocess.run(
            [str(ROOT / "macbox"), "session", "--name", name],
            cwd=ROOT,
            input=(
                f"mkdir {relative_dir}\n"
                f"touch {relative_dir}/touched.txt\n"
                f"echo redirected > {relative_dir}/redirect.txt\n"
                f"cat {relative_dir}/redirect.txt\n"
                "mb-changes\n"
                "exit\n"
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertNotIn("No such file or directory", output)
        self.assertIn("redirected", output)
        self.assertFalse(real_dir.exists(), f"real directory should not be written: {real_dir}")
        self.assertEqual((overlay_dir / "touched.txt").read_text(), "")
        self.assertEqual((overlay_dir / "redirect.txt").read_text().strip(), "redirected")


if __name__ == "__main__":
    unittest.main()
