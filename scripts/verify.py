"""One command to check a change before calling it done:  python scripts/verify.py

Byte-compiles every module, syntax-checks the UI JavaScript (needs node), lints with ruff when installed,
then runs the test suite. Exits non-zero on the first failing step and says which one.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def step(name: str, cmd: list[str], required: bool = True) -> bool:
    exe = shutil.which(cmd[0]) if cmd[0] != sys.executable else sys.executable
    if not exe:
        print(f"-- {name}: skipped ({cmd[0]} not installed)")
        return not required
    print(f"-- {name}: {' '.join(cmd[1:])}")
    env = {**os.environ, "REELWRIGHT_DATA": tempfile.mkdtemp(prefix="rw-verify-")}  # never touch the real library
    ok = subprocess.run([exe, *cmd[1:]], cwd=ROOT, env=env).returncode == 0
    print(f"   {'PASS' if ok else 'FAIL'}")
    return ok


def main() -> int:
    steps = [
        ("compile", [sys.executable, "-m", "compileall", "-q", "reelwright", "scripts", "tests"], True),
        ("ui syntax", ["node", "--check", "reelwright/static/app.js"], False),
        ("lint", ["ruff", "check", "--select", "E9,F63,F7,F82,F401,F811,F841", "reelwright", "tests"], False),
        ("tests", [sys.executable, "-m", "pytest", "-q", "tests"], True),
    ]
    for name, cmd, required in steps:
        if not step(name, cmd, required):
            print(f"\nVERIFY FAILED at: {name}")
            return 1
    print("\nVERIFY PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
