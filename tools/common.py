from __future__ import annotations

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
CLONES_DIR = ROOT_DIR / "clones"
CLONE_COUNT = 10
BASE_PORT = 1000
HOST = "0.0.0.0"
DEFAULT_VIEWPORT = {"width": 1280, "height": 850}


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def write_env_file(path: Path, values: dict[str, str]) -> None:
    path.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="utf-8",
    )


def normalize_route_path(value: str | None) -> str:
    if not value:
        raise ValueError("PATH is missing from .env")

    route_path = value if value.startswith("/") else f"/{value}"
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~/-")
    if any(char not in allowed for char in route_path) or ".." in route_path:
        raise ValueError(f"Invalid PATH value: {value}")

    route_path = route_path.rstrip("/")
    return route_path or "/"


def public_path(base_path: str, suffix: str = "") -> str:
    suffix = suffix.lstrip("/")
    if base_path == "/":
        return f"/{suffix}" if suffix else "/"
    return f"{base_path}/{suffix}" if suffix else base_path
