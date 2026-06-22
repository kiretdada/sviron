from __future__ import annotations

import argparse
import secrets
import subprocess
import sys
from pathlib import Path

from common import BASE_PORT, CLONE_COUNT, CLONES_DIR, ROOT_DIR, parse_env_file, write_env_file


def make_token_path() -> str:
    return f"/{secrets.token_hex(6)}"


def launcher_cmd() -> str:
    return "\r\n".join(
        [
            "@echo off",
            'cd /d "%~dp0\\..\\.."',
            'python tools\\run_clone.py --clone "%~dp0"',
            "",
        ]
    )


def launcher_ps1() -> str:
    return "\r\n".join(
        [
            '$Root = Resolve-Path (Join-Path $PSScriptRoot "..\\..")',
            "Set-Location $Root",
            '& python .\\tools\\run_clone.py --clone "$PSScriptRoot"',
            "",
        ]
    )


def create_clone(index: int, used_paths: set[str], reset_env: bool) -> tuple[str, Path, dict[str, str]]:
    clone_name = f"chromium-{index:02d}"
    clone_dir = CLONES_DIR / clone_name
    env_file = clone_dir / ".env"

    for child in ["profile", "cache", "downloads", "config", "logs"]:
        (clone_dir / child).mkdir(parents=True, exist_ok=True)

    if reset_env or not env_file.exists():
        token_path = make_token_path()
        while token_path in used_paths:
            token_path = make_token_path()
        used_paths.add(token_path)
        write_env_file(
            env_file,
            {
                "PORT": str(BASE_PORT + index - 1),
                "PATH": token_path,
            },
        )
    else:
        existing = parse_env_file(env_file)
        if existing.get("PATH"):
            used_paths.add(existing["PATH"])

    (clone_dir / "launch.cmd").write_text(launcher_cmd(), encoding="utf-8")
    (clone_dir / "launch.ps1").write_text(launcher_ps1(), encoding="utf-8")

    return clone_name, clone_dir, parse_env_file(env_file)


def create_clones(reset_env: bool) -> None:
    CLONES_DIR.mkdir(parents=True, exist_ok=True)
    used_paths: set[str] = set()
    clones = [create_clone(index, used_paths, reset_env) for index in range(1, CLONE_COUNT + 1)]

    print("Created Chromium clone folders:")
    for clone_name, _, env in clones:
        print(f"- {clone_name}: PORT={env['PORT']} PATH={env['PATH']}")


def install_chromium() -> bool:
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        cwd=ROOT_DIR,
        check=False,
    )
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset-env", action="store_true", help="Regenerate all per-clone .env files.")
    parser.add_argument("--skip-browser-download", action="store_true", help="Create clones without downloading Chromium.")
    parser.add_argument("--only-browser-download", action="store_true", help="Only download Chromium.")
    args = parser.parse_args()

    if not args.only_browser_download:
        create_clones(args.reset_env)

    if args.skip_browser_download:
        return 0

    try:
        import playwright  # noqa: F401
    except ImportError:
        print("Playwright is not installed yet. Run `python -m pip install -r requirements.txt` first.")
        return 1

    if not install_chromium():
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
