from __future__ import annotations

import subprocess
import sys
import time
import os
import signal

from common import CLONE_COUNT, CLONES_DIR, ROOT_DIR, env_bool, env_get, load_project_env

AUTO_LOGIN_DONE_FILE = "auto-login.done"
AUTO_LOGIN_FAILED_FILE = "auto-login.failed"


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


def int_config(config: dict[str, str], key: str, default: int) -> int:
    value = env_get(config, key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def wait_for_auto_login(clone_dir, child: subprocess.Popen[bytes], timeout_seconds: int) -> None:
    done_file = clone_dir / "logs" / AUTO_LOGIN_DONE_FILE
    failed_file = clone_dir / "logs" / AUTO_LOGIN_FAILED_FILE
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        if done_file.exists():
            print(f"[{clone_dir.name}] Auto-login marker found.")
            return
        if failed_file.exists():
            message = failed_file.read_text(encoding="utf-8").strip()
            print(f"[{clone_dir.name}] Auto-login failed: {message}", file=sys.stderr)
            return
        if child.poll() is not None:
            raise RuntimeError(f"{clone_dir.name} exited before auto-login completed.")
        time.sleep(0.5)

    raise TimeoutError(f"Timed out waiting for auto-login marker for {clone_dir.name}.")


def main() -> int:
    children: list[subprocess.Popen[bytes]] = []
    config = load_project_env()
    auto_login = env_bool(config, "AUTO_LOGIN", False)
    auto_login_wait = int_config(config, "AUTO_LOGIN_TIMEOUT_SECONDS", 90) + 20

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

            if auto_login:
                wait_for_auto_login(clone_dir, child, auto_login_wait)

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
