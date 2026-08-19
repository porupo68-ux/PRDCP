from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Compile only the maintained runtime and test surface.  ``compileall .`` also
# descends into archived prototype snapshots and persisted data; on Windows
# those historical nested paths can exceed MAX_PATH even though no active
# source file is affected.
ACTIVE_PYTHON_TARGETS = (
    "main.py",
    "runtime.py",
    "cli_app",
    "common",
    "conclusion",
    "config",
    "deliberation",
    "discord_app",
    "playwright",
    "producer",
    "providers",
    "researcher",
    "retrieval",
    "role_definitions",
    "scripts",
    "specifications",
    "tests",
)


def run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=PROJECT_ROOT, env=env, check=True)


def isolated_environment(data_dir: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PRDCP_PROVIDER": "mock",
            "PRDCP_RETRIEVAL_PROVIDER": "mock",
            "PRDCP_DATA_DIR": data_dir,
            "DISCORD_BOT_TOKEN": "",
        }
    )
    return env


def main() -> int:
    run([sys.executable, "-m", "compileall", "-q", *ACTIVE_PYTHON_TARGETS])
    with tempfile.TemporaryDirectory(prefix="prdcp-verify-tests-") as test_data_dir:
        run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            env=isolated_environment(test_data_dir),
        )
    with tempfile.TemporaryDirectory(prefix="prdcp-verify-e2e-") as temporary_dir:
        env = isolated_environment(temporary_dir)
        run([sys.executable, "main.py", "--doctor"], env=env)
        run(
            [
                sys.executable,
                "main.py",
                "--demo-e2e",
                "--topic",
                "生成AIは人間の仕事を奪うのか",
            ],
            env=env,
        )
        delivery_roots = list((Path(temporary_dir) / "deliveries").glob("*"))
        if len(delivery_roots) != 1:
            raise RuntimeError(f"expected one delivery directory, found {len(delivery_roots)}")
        expected = {
            "final_script_package.json",
            "script.md",
            "citation_manifest.json",
            "source_list.md",
            "visual_plan.md",
            "production_notes.md",
        }
        actual = {path.name for path in delivery_roots[0].iterdir() if path.is_file()}
        if actual != expected:
            raise RuntimeError(f"delivery mismatch: expected={expected}, actual={actual}")
    print("Verification completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
