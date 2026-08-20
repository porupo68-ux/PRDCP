from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class OperatorCliIntegrationTests(unittest.TestCase):
    def test_help_forces_utf8_output_on_windows_code_page(self) -> None:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "cp932"

        result = subprocess.run(
            [sys.executable, "main.py", "--help"],
            cwd=PROJECT_ROOT,
            env=env,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("保存済みcheckpoint", result.stdout)
        self.assertIn("--researcher-evidence", result.stdout)
        self.assertIn("--researcher-accept-limitations", result.stdout)
        self.assertIn("--researcher-revise", result.stdout)
        self.assertIn("--researcher-integrity-repair", result.stdout)

    def test_default_e2e_output_is_concise_and_status_is_inspectable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            env = os.environ.copy()
            env.update(
                {
                    "PRDCP_PROVIDER": "mock",
                    "PRDCP_RETRIEVAL_PROVIDER": "mock",
                    "PRDCP_DATA_DIR": temporary_dir,
                    "DISCORD_BOT_TOKEN": "",
                }
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "main.py",
                    "--demo-e2e",
                    "--topic",
                    "生成AIは人間の仕事を奪うのか",
                ],
                cwd=PROJECT_ROOT,
                env=env,
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertLess(len(result.stdout.splitlines()), 100)
            self.assertNotIn('"message_history"', result.stdout)
            self.assertIn("[Playwright] COMPLETED", result.stdout)
            self.assertIn("Mock Human Evidence Decision", result.stdout)

            workflow_paths = list(
                (Path(temporary_dir) / "workflows" / "playwright").glob("*.json")
            )
            self.assertEqual(1, len(workflow_paths))
            workflow_id = workflow_paths[0].stem
            status = subprocess.run(
                [sys.executable, "main.py", "--status", workflow_id],
                cwd=PROJECT_ROOT,
                env=env,
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, status.returncode, status.stderr)
            self.assertEqual(5, status.stdout.count("] COMPLETED"))
            self.assertEqual(1, status.stdout.count("  next:"))


if __name__ == "__main__":
    unittest.main()
