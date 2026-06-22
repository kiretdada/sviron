from __future__ import annotations

import subprocess
import sys
import time
import os
import signal

from common import CLONE_COUNT, CLONES_DIR, ROOT_DIR


def stop_child(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is not None:
        return

    if os.name == "nt":
        child.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        child.terminate()

    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()


def main() -> int:
    children: list[subprocess.Popen[bytes]] = []

    try:
        for index in range(1, CLONE_COUNT + 1):
            clone_dir = CLONES_DIR / f"chromium-{index:02d}"
            if not (clone_dir / ".env").exists():
                print(f"Missing {clone_dir / '.env'}. Run `python tools\\setup.py` first.")
                return 1

            child = subprocess.Popen(
                [sys.executable, str(ROOT_DIR / "tools" / "run_clone.py"), "--clone", str(clone_dir)],
                cwd=ROOT_DIR,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
            children.append(child)

        while True:
            failed = [child.returncode for child in children if child.poll() not in (None, 0)]
            if failed:
                return failed[0] or 1
            time.sleep(1)
    except KeyboardInterrupt:
        return 0
    finally:
        for child in children:
            stop_child(child)


if __name__ == "__main__":
    raise SystemExit(main())
