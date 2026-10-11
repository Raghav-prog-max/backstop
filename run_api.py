"""Start the console's API with the project's own interpreter.

`python run_api.py` works whether or not the caller's `python` is the one that has
the [serve] extra installed: if a project virtualenv exists and is not the running
interpreter, this re-executes itself with that one. Keeps .claude/launch.json
cross-platform — the alternative is a hard-coded .venv/Scripts/python.exe, which is
Windows-only and machine-specific.

    python run_api.py            # same as python -m backstop.api
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _venv_python() -> Path | None:
    for candidate in (ROOT / ".venv" / "Scripts" / "python.exe",
                      ROOT / ".venv" / "bin" / "python"):
        if candidate.exists():
            return candidate
    return None


def main() -> int:
    venv = _venv_python()
    running = Path(sys.executable).resolve()
    if venv is not None and venv.resolve() != running:
        # Run the server as a child and stay alive until it exits, rather than
        # os.execv: on Windows exec leaves the launcher watching a process that has
        # already gone, and a dev-server manager reads that as "exited at startup".
        # The child sees itself as `venv`, so it takes the branch below — no loop.
        child = subprocess.run([str(venv), str(Path(__file__).resolve()), *sys.argv[1:]])
        return child.returncode

    # Works from a clean checkout too, installed or not.
    src = ROOT / "src"
    if src.is_dir():
        sys.path.insert(0, str(src))

    try:
        import uvicorn
    except ModuleNotFoundError:
        sys.stderr.write(
            "The API needs the [serve] extra:\n"
            '    python -m pip install -e ".[serve]"\n'
        )
        return 1

    uvicorn.run("backstop.api.app:app", host="127.0.0.1", port=8010, reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
