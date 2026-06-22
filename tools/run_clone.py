from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse, urlsplit
from urllib.request import Request, urlopen

from common import (
    DEFAULT_VIEWPORT,
    HOST,
    ROOT_DIR,
    env_bool,
    env_get,
    load_project_env,
    normalize_route_path,
    parse_env_file,
    public_path,
)

MAX_BODY_BYTES = 64 * 1024
AUTO_LOGIN_DONE_FILE = "auto-login.done"
AUTO_LOGIN_FAILED_FILE = "auto-login.failed"
AUTO_LOGIN_RUNNING_FILE = "auto-login.running"
DEFAULT_AUTO_LOGIN_URL = "https://pro-siv.interieur.gouv.fr/map-ppa-ui/do/home"


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class FrameHub:
    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.frame: bytes | None = None
        self.meta: dict[str, Any] = {
            "deviceWidth": DEFAULT_VIEWPORT["width"],
            "deviceHeight": DEFAULT_VIEWPORT["height"],
        }
        self.sequence = 0

    def publish(self, frame: bytes, meta: dict[str, Any]) -> None:
        with self.condition:
            self.frame = frame
            self.meta = meta or self.meta
            self.sequence += 1
            self.condition.notify_all()

    def size(self) -> tuple[float, float]:
        with self.condition:
            width = float(self.meta.get("deviceWidth") or DEFAULT_VIEWPORT["width"])
            height = float(self.meta.get("deviceHeight") or DEFAULT_VIEWPORT["height"])
        return width, height


class CDPClient:
    def __init__(self, websocket_url: str) -> None:
        self.websocket_url = websocket_url
        self.socket: socket.socket | None = None
        self.next_id = 1
        self.id_lock = threading.Lock()
        self.send_lock = threading.Lock()
        self.pending: dict[int, dict[str, Any]] = {}
        self.pending_lock = threading.Lock()
        self.handlers: dict[str, list[Any]] = {}
        self.closed = threading.Event()
        self.reader: threading.Thread | None = None

    def connect(self) -> None:
        parsed = urlparse(self.websocket_url)
        if parsed.scheme != "ws":
            raise ValueError(f"Only ws:// CDP endpoints are supported: {self.websocket_url}")

        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        sock = socket.create_connection((parsed.hostname, port), timeout=10)
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        sock.sendall(request.encode("ascii"))
        response = self._read_http_headers(sock)
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"CDP WebSocket upgrade failed: {response[:120]!r}")
        sock.settimeout(None)

        self.socket = sock
        self.reader = threading.Thread(target=self._read_loop, name="cdp-reader", daemon=True)
        self.reader.start()

    def on(self, method: str, handler: Any) -> None:
        self.handlers.setdefault(method, []).append(handler)

    def send(self, method: str, params: dict[str, Any] | None = None, wait: bool = True, timeout: float = 10) -> Any:
        if self.closed.is_set():
            raise RuntimeError("CDP connection is closed")

        with self.id_lock:
            message_id = self.next_id
            self.next_id += 1

        pending = {"event": threading.Event(), "response": None}
        if wait:
            with self.pending_lock:
                self.pending[message_id] = pending

        payload = json.dumps(
            {
                "id": message_id,
                "method": method,
                "params": params or {},
            },
            separators=(",", ":"),
        ).encode("utf-8")
        self._send_frame(0x1, payload)

        if not wait:
            return None

        if not pending["event"].wait(timeout):
            with self.pending_lock:
                self.pending.pop(message_id, None)
            raise TimeoutError(f"Timed out waiting for CDP response to {method}")

        response = pending["response"]
        if isinstance(response, dict) and "error" in response:
            raise RuntimeError(response["error"])
        return response.get("result") if isinstance(response, dict) else response

    def close(self) -> None:
        self.closed.set()
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        if self.socket:
            try:
                self.socket.close()
            except OSError:
                pass

    def _read_http_headers(self, sock: socket.socket) -> bytes:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
            if len(data) > 32768:
                break
        return data

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if not self.socket:
            raise RuntimeError("CDP socket is not connected")

        length = len(payload)
        header = bytearray([0x80 | opcode])
        if length <= 125:
            header.append(0x80 | length)
        elif length <= 65535:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))

        mask = secrets.token_bytes(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        with self.send_lock:
            self.socket.sendall(bytes(header) + mask + masked)

    def _read_frame(self) -> tuple[bool, int, bytes] | None:
        if not self.socket:
            return None

        header = self._recv_exact(2)
        if not header:
            return None
        first, second = header
        final = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]

        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length)
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return final, opcode, payload

    def _recv_exact(self, length: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < length:
            chunk = self.socket.recv(length - len(chunks)) if self.socket else b""
            if not chunk:
                raise OSError("WebSocket closed")
            chunks.extend(chunk)
        return bytes(chunks)

    def _read_loop(self) -> None:
        message_opcode: int | None = None
        message_parts: list[bytes] = []

        while not self.closed.is_set():
            try:
                frame = self._read_frame()
                if frame is None:
                    break
                final, opcode, payload = frame
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    self._send_frame(0xA, payload)
                    continue
                if opcode == 0xA:
                    continue

                if opcode in (0x1, 0x2):
                    message_opcode = opcode
                    message_parts = [payload]
                elif opcode == 0x0 and message_opcode is not None:
                    message_parts.append(payload)
                else:
                    continue

                if not final:
                    continue

                complete_opcode = message_opcode
                complete_payload = b"".join(message_parts)
                message_opcode = None
                message_parts = []

                if complete_opcode != 0x1:
                    continue

                message = json.loads(complete_payload.decode("utf-8"))
                if "id" in message:
                    with self.pending_lock:
                        pending = self.pending.pop(int(message["id"]), None)
                    if pending:
                        pending["response"] = message
                        pending["event"].set()
                    continue

                method = message.get("method")
                params = message.get("params") or {}
                for handler in self.handlers.get(method, []):
                    handler(params)
            except Exception:
                break

        self.closed.set()


class BrowserManager:
    def __init__(self, clone_dir: Path, hub: FrameHub) -> None:
        self.clone_dir = clone_dir
        self.hub = hub
        self.process: subprocess.Popen[bytes] | None = None
        self.cdp: CDPClient | None = None
        self.devtools_port: int | None = None
        self.active_target_id: str | None = None
        self.active_url = "about:blank"

    def start(self) -> None:
        profile_dir = self.clone_dir / "profile"
        cache_dir = self.clone_dir / "cache"
        downloads_dir = self.clone_dir / "downloads"
        for directory in [profile_dir, cache_dir, downloads_dir, self.clone_dir / "logs"]:
            directory.mkdir(parents=True, exist_ok=True)

        active_port_file = profile_dir / "DevToolsActivePort"
        if active_port_file.exists():
            active_port_file.unlink()

        chromium = find_chromium_executable()
        args = [
            str(chromium),
            f"--user-data-dir={profile_dir}",
            f"--disk-cache-dir={cache_dir}",
            f"--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate",
            f"--window-size={DEFAULT_VIEWPORT['width']},{DEFAULT_VIEWPORT['height']}",
            "about:blank",
        ]

        self.process = subprocess.Popen(args, cwd=self.clone_dir)
        self.devtools_port = self._wait_for_devtools_port(active_port_file)
        self._connect_to_page()

    def _wait_for_devtools_port(self, active_port_file: Path) -> int:
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.process and self.process.poll() is not None:
                raise RuntimeError(f"Chromium exited with code {self.process.returncode}")
            if active_port_file.exists():
                lines = active_port_file.read_text(encoding="utf-8").splitlines()
                if lines and lines[0].isdigit():
                    return int(lines[0])
            time.sleep(0.1)
        raise TimeoutError("Timed out waiting for Chromium DevTools endpoint")

    def _connect_to_page(self) -> None:
        if not self.devtools_port:
            raise RuntimeError("DevTools port is not ready")

        target = self._get_page_target()
        self.active_target_id = target.get("id")
        self.active_url = target.get("url") or "about:blank"
        self.cdp = CDPClient(target["webSocketDebuggerUrl"])
        self.cdp.connect()
        self.cdp.on("Page.frameNavigated", self._on_frame_navigated)
        self.cdp.on("Page.screencastFrame", self._on_screencast_frame)
        self.cdp.send("Page.enable")
        self.cdp.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": 82,
                "maxWidth": 1600,
                "maxHeight": 1200,
                "everyNthFrame": 1,
            },
        )

    def _get_page_target(self) -> dict[str, Any]:
        targets = self._json_get("/json/list")
        pages = [
            target
            for target in targets
            if target.get("type") == "page" and target.get("webSocketDebuggerUrl")
        ]
        if pages:
            return pages[0]
        return self._json_open("/json/new?about%3Ablank", method="PUT")

    def _json_get(self, path: str) -> Any:
        with urlopen(f"http://127.0.0.1:{self.devtools_port}{path}", timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def _json_open(self, path: str, method: str = "GET") -> Any:
        request = Request(f"http://127.0.0.1:{self.devtools_port}{path}", method=method)
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def _on_screencast_frame(self, params: dict[str, Any]) -> None:
        frame = base64.b64decode(params["data"])
        self.hub.publish(frame, params.get("metadata") or {})
        if self.cdp:
            self.cdp.send("Page.screencastFrameAck", {"sessionId": params.get("sessionId")}, wait=False)

    def _on_frame_navigated(self, params: dict[str, Any]) -> None:
        frame = params.get("frame") or {}
        url = frame.get("url")
        if url:
            self.active_url = url

    def dispatch_input(self, payload: dict[str, Any]) -> None:
        if payload.get("kind") == "mouse":
            self.dispatch_mouse(payload)
        elif payload.get("kind") == "keyboard":
            self.dispatch_keyboard(payload)
        elif payload.get("kind") == "text":
            self.dispatch_text(payload)

    def dispatch_mouse(self, payload: dict[str, Any]) -> None:
        if not self.cdp:
            return
        width, height = self.hub.size()
        event_type = str(payload.get("type") or "mouseMoved")
        params: dict[str, Any] = {
            "type": event_type,
            "x": clamp_unit(payload.get("x")) * width,
            "y": clamp_unit(payload.get("y")) * height,
            "modifiers": int(payload.get("modifiers") or 0),
        }

        if event_type == "mouseWheel":
            params["deltaX"] = float(payload.get("deltaX") or 0)
            params["deltaY"] = float(payload.get("deltaY") or 0)
        else:
            params["button"] = "none" if event_type == "mouseMoved" else button_name(payload.get("button"))
            params["buttons"] = int(payload.get("buttons") or 0)
            params["clickCount"] = max(1, int(payload.get("clickCount") or 1))

        self.cdp.send("Page.bringToFront", wait=False)
        self.cdp.send("Input.dispatchMouseEvent", params, wait=False)

    def dispatch_keyboard(self, payload: dict[str, Any]) -> None:
        if not self.cdp:
            return
        is_key_up = payload.get("type") == "keyup"
        key = str(payload.get("key") or "")
        key_code = int(payload.get("keyCode") or 0)
        modifiers = int(payload.get("modifiers") or 0)
        params = {
            "type": "keyUp" if is_key_up else "keyDown",
            "modifiers": modifiers,
            "windowsVirtualKeyCode": key_code,
            "nativeVirtualKeyCode": key_code,
            "key": key,
            "code": str(payload.get("code") or ""),
            "autoRepeat": bool(payload.get("repeat")),
        }

        self.cdp.send("Page.bringToFront", wait=False)
        self.cdp.send("Input.dispatchKeyEvent", params, wait=False)
        if not is_key_up and len(key) == 1 and not (modifiers & 1) and not (modifiers & 4):
            self.cdp.send(
                "Input.dispatchKeyEvent",
                {
                    "type": "char",
                    "modifiers": modifiers,
                    "text": key,
                    "unmodifiedText": key,
                    "key": key,
                    "code": params["code"],
                    "windowsVirtualKeyCode": key_code,
                    "nativeVirtualKeyCode": key_code,
                },
                wait=False,
            )

    def dispatch_text(self, payload: dict[str, Any]) -> None:
        if self.cdp:
            text = str(payload.get("text") or "")
            if text:
                self.cdp.send("Input.insertText", {"text": text}, wait=False)

    def stop(self) -> None:
        if self.cdp:
            self.cdp.close()
            self.cdp = None
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()


def reset_auto_login_markers(clone_dir: Path) -> None:
    logs_dir = clone_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    for name in [AUTO_LOGIN_DONE_FILE, AUTO_LOGIN_FAILED_FILE, AUTO_LOGIN_RUNNING_FILE]:
        marker = logs_dir / name
        if marker.exists():
            marker.unlink()


def write_auto_login_marker(clone_dir: Path, name: str, message: str) -> None:
    logs_dir = clone_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / name).write_text(message, encoding="utf-8")


def int_config(config: dict[str, str], key: str, default: int) -> int:
    value = env_get(config, key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def run_auto_login(clone_dir: Path, manager: BrowserManager, config: dict[str, str]) -> None:
    script_name = env_get(config, "AUTO_LOGIN_SCRIPT", "Login-SIV.ps1") or "Login-SIV.ps1"
    script_path = Path(script_name)
    if not script_path.is_absolute():
        script_path = ROOT_DIR / script_path

    if not script_path.exists():
        raise FileNotFoundError(f"Auto-login script was not found: {script_path}")

    if not manager.process:
        raise RuntimeError("Chromium process is not available for auto-login")

    timeout_seconds = int_config(config, "AUTO_LOGIN_TIMEOUT_SECONDS", 90)
    certificate_delay_ms = int_config(config, "AUTO_LOGIN_CERTIFICATE_DELAY_MS", 800)
    url = env_get(config, "AUTO_LOGIN_URL", DEFAULT_AUTO_LOGIN_URL) or DEFAULT_AUTO_LOGIN_URL
    logs_dir = clone_dir / "logs"
    stdout_log = logs_dir / "auto-login.out.log"
    stderr_log = logs_dir / "auto-login.err.log"
    chromium = find_chromium_executable()

    if manager.cdp:
        manager.cdp.send("Page.navigate", {"url": url}, wait=False)

    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        "-Url",
        url,
        "-ChromeExecutable",
        str(chromium),
        "-UserDataDir",
        str(clone_dir / "profile"),
        "-DiskCacheDir",
        str(clone_dir / "cache"),
        "-BrowserProcessId",
        str(manager.process.pid),
        "-CertificateDelayMs",
        str(certificate_delay_ms),
        "-UseExistingBrowserWindow",
    ]

    write_auto_login_marker(clone_dir, AUTO_LOGIN_RUNNING_FILE, f"started {time.time()}\n")
    try:
        with stdout_log.open("w", encoding="utf-8") as stdout, stderr_log.open("w", encoding="utf-8") as stderr:
            result = subprocess.run(
                command,
                cwd=ROOT_DIR,
                stdout=stdout,
                stderr=stderr,
                timeout=timeout_seconds,
                check=False,
            )
    finally:
        running_marker = logs_dir / AUTO_LOGIN_RUNNING_FILE
        if running_marker.exists():
            running_marker.unlink()

    if result.returncode != 0:
        raise RuntimeError(f"Auto-login failed with exit code {result.returncode}. See {stderr_log}")


def find_chromium_executable() -> Path:
    env_path = os.environ.get("CHROMIUM_EXECUTABLE")
    candidates: list[Path] = []
    if env_path:
        candidates.append(Path(env_path))

    local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    playwright_dir = local_app_data / "ms-playwright"
    candidates.extend(sorted(playwright_dir.glob("chromium-*/chrome-win64/chrome.exe"), reverse=True))
    candidates.extend(sorted(playwright_dir.glob("chromium-*/chrome-win/chrome.exe"), reverse=True))

    program_files = [os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)")]
    for root in [Path(value) for value in program_files if value]:
        candidates.append(root / "Google" / "Chrome" / "Application" / "chrome.exe")
        candidates.append(root / "Microsoft" / "Edge" / "Application" / "msedge.exe")

    for candidate in candidates:
        if candidate.exists():
            return candidate

    raise FileNotFoundError(
        "Could not find Chromium. Run `python -m pip install -r requirements.txt` and "
        "`python -m playwright install chromium`, or set CHROMIUM_EXECUTABLE."
    )


def clamp_unit(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, number))


def button_name(value: Any) -> str:
    if value in (1, "middle"):
        return "middle"
    if value in (2, "right"):
        return "right"
    return "left"


def make_html(base_path: str, clone_name: str) -> bytes:
    stream_path = public_path(base_path, "/stream")
    input_path = public_path(base_path, "/input")
    health_path = public_path(base_path, "/health")
    title = clone_name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    source = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    html, body {{
      width: 100%;
      height: 100%;
      margin: 0;
      background: #111;
      overflow: hidden;
      font-family: Arial, sans-serif;
    }}
    body {{
      display: grid;
      place-items: center;
    }}
    #screen {{
      width: 100vw;
      height: 100vh;
      object-fit: contain;
      display: block;
      user-select: none;
      touch-action: none;
      outline: none;
    }}
    #status {{
      position: fixed;
      left: 10px;
      bottom: 10px;
      padding: 6px 8px;
      border-radius: 4px;
      background: rgba(0, 0, 0, 0.58);
      color: #fff;
      font-size: 12px;
      line-height: 1;
      pointer-events: none;
      opacity: 0;
      transition: opacity 160ms ease;
    }}
    #status[data-visible="true"] {{
      opacity: 1;
    }}
  </style>
</head>
<body tabindex="0">
  <img id="screen" alt="" draggable="false">
  <div id="status"></div>
  <script>
    const screen = document.getElementById("screen");
    const status = document.getElementById("status");
    const streamPath = {json.dumps(stream_path)};
    const inputPath = {json.dumps(input_path)};
    const healthPath = {json.dumps(health_path)};
    let statusTimer = null;
    let reconnectTimer = null;

    function connectStream() {{
      clearTimeout(reconnectTimer);
      screen.src = streamPath + "?t=" + Date.now().toString(36);
    }}

    function showStatus(text) {{
      status.textContent = text;
      status.dataset.visible = "true";
      clearTimeout(statusTimer);
      statusTimer = setTimeout(() => {{
        status.dataset.visible = "false";
      }}, 1200);
    }}

    function modifiers(event) {{
      return (event.altKey ? 1 : 0) |
        (event.ctrlKey ? 2 : 0) |
        (event.metaKey ? 4 : 0) |
        (event.shiftKey ? 8 : 0);
    }}

    function button(event) {{
      if (event.button === 1) return "middle";
      if (event.button === 2) return "right";
      return "left";
    }}

    function pointerPosition(event) {{
      const rect = screen.getBoundingClientRect();
      const objectRatio = screen.naturalWidth / Math.max(1, screen.naturalHeight);
      const rectRatio = rect.width / Math.max(1, rect.height);
      let contentWidth = rect.width;
      let contentHeight = rect.height;
      let offsetX = 0;
      let offsetY = 0;

      if (Number.isFinite(objectRatio) && objectRatio > 0) {{
        if (rectRatio > objectRatio) {{
          contentWidth = rect.height * objectRatio;
          offsetX = (rect.width - contentWidth) / 2;
        }} else {{
          contentHeight = rect.width / objectRatio;
          offsetY = (rect.height - contentHeight) / 2;
        }}
      }}

      const x = (event.clientX - rect.left - offsetX) / Math.max(1, contentWidth);
      const y = (event.clientY - rect.top - offsetY) / Math.max(1, contentHeight);
      return {{
        x: Math.max(0, Math.min(1, x)),
        y: Math.max(0, Math.min(1, y))
      }};
    }}

    async function postInput(payload) {{
      try {{
        await fetch(inputPath, {{
          method: "POST",
          headers: {{ "content-type": "application/json" }},
          body: JSON.stringify(payload)
        }});
      }} catch (error) {{
        showStatus("Disconnected");
      }}
    }}

    function sendPointer(event, type) {{
      event.preventDefault();
      screen.focus();
      document.body.focus();
      const pos = pointerPosition(event);
      postInput({{
        kind: "mouse",
        type,
        x: pos.x,
        y: pos.y,
        button: button(event),
        buttons: event.buttons || 0,
        clickCount: event.detail || 1,
        modifiers: modifiers(event)
      }});
    }}

    screen.addEventListener("pointerdown", (event) => {{
      screen.setPointerCapture(event.pointerId);
      sendPointer(event, "mousePressed");
    }});
    screen.addEventListener("pointermove", (event) => sendPointer(event, "mouseMoved"));
    screen.addEventListener("pointerup", (event) => {{
      sendPointer(event, "mouseReleased");
      try {{ screen.releasePointerCapture(event.pointerId); }} catch (_) {{}}
    }});
    screen.addEventListener("contextmenu", (event) => event.preventDefault());
    screen.addEventListener("wheel", (event) => {{
      event.preventDefault();
      const pos = pointerPosition(event);
      postInput({{
        kind: "mouse",
        type: "mouseWheel",
        x: pos.x,
        y: pos.y,
        deltaX: event.deltaX,
        deltaY: event.deltaY,
        modifiers: modifiers(event)
      }});
    }}, {{ passive: false }});

    window.addEventListener("keydown", (event) => {{
      if ((event.ctrlKey || event.metaKey) && ["r", "w", "l"].includes(event.key.toLowerCase())) {{
        event.preventDefault();
      }}
      postInput({{
        kind: "keyboard",
        type: "keydown",
        key: event.key,
        code: event.code,
        keyCode: event.keyCode,
        modifiers: modifiers(event),
        repeat: event.repeat
      }});
    }});
    window.addEventListener("keyup", (event) => {{
      postInput({{
        kind: "keyboard",
        type: "keyup",
        key: event.key,
        code: event.code,
        keyCode: event.keyCode,
        modifiers: modifiers(event),
        repeat: false
      }});
    }});
    window.addEventListener("paste", (event) => {{
      const text = event.clipboardData && event.clipboardData.getData("text");
      if (text) {{
        event.preventDefault();
        postInput({{ kind: "text", text }});
      }}
    }});
    screen.addEventListener("error", () => {{
      showStatus("Waiting for stream");
      reconnectTimer = setTimeout(connectStream, 800);
    }});
    screen.addEventListener("load", () => {{
      status.dataset.visible = "false";
    }});
    setInterval(() => {{
      fetch(healthPath, {{ cache: "no-store" }}).catch(() => showStatus("Disconnected"));
    }}, 5000);
    window.focus();
    document.body.focus();
    connectStream();
  </script>
</body>
</html>"""
    return source.encode("utf-8")


def make_handler(
    manager: BrowserManager,
    hub: FrameHub,
    base_path: str,
    clone_name: str,
) -> type[BaseHTTPRequestHandler]:
    class CloneHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            request_path = urlsplit(self.path).path.rstrip("/") or "/"
            if request_path == base_path:
                self.send_bytes(HTTPStatus.OK, make_html(base_path, clone_name), "text/html; charset=utf-8")
                return
            if request_path == public_path(base_path, "/health"):
                self.send_json(
                    HTTPStatus.OK,
                    {"ok": True, "clone": clone_name, "activeUrl": manager.active_url},
                )
                return
            if request_path == public_path(base_path, "/stream"):
                self.stream_frames()
                return
            self.send_bytes(HTTPStatus.NOT_FOUND, b"Not found", "text/plain; charset=utf-8")

        def do_POST(self) -> None:
            request_path = urlsplit(self.path).path.rstrip("/") or "/"
            if request_path != public_path(base_path, "/input"):
                self.send_bytes(HTTPStatus.NOT_FOUND, b"Not found", "text/plain; charset=utf-8")
                return

            try:
                length = int(self.headers.get("content-length", "0"))
                if length > MAX_BODY_BYTES:
                    raise ValueError("Request body is too large")
                payload = json.loads(self.rfile.read(length) or b"{}")
                manager.dispatch_input(payload)
                self.send_json(HTTPStatus.OK, {"ok": True})
            except Exception as exc:
                self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})

        def stream_frames(self) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("content-type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("cache-control", "no-cache, no-store, must-revalidate")
            self.send_header("pragma", "no-cache")
            self.send_header("connection", "close")
            self.send_header("x-accel-buffering", "no")
            self.end_headers()

            last_sequence = -1
            while True:
                with hub.condition:
                    hub.condition.wait_for(lambda: hub.sequence != last_sequence, timeout=1.0)
                    frame = hub.frame
                    last_sequence = hub.sequence
                if not frame:
                    continue
                try:
                    self.wfile.write(
                        f"--frame\r\ncontent-type: image/jpeg\r\ncontent-length: {len(frame)}\r\n\r\n".encode(
                            "ascii"
                        )
                    )
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                except OSError:
                    return

        def send_json(self, status: HTTPStatus, value: dict[str, Any]) -> None:
            self.send_bytes(status, json.dumps(value).encode("utf-8"), "application/json; charset=utf-8")

        def send_bytes(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return CloneHandler


async def run(args: argparse.Namespace) -> None:
    clone_dir = Path(args.clone).resolve()
    env_file = clone_dir / ".env"
    if not env_file.exists():
        raise FileNotFoundError(f"Missing .env file: {env_file}")

    project_env = load_project_env()
    env = parse_env_file(env_file)
    port = int(env["PORT"])
    base_path = normalize_route_path(env.get("PATH"))
    clone_name = clone_dir.name
    reset_auto_login_markers(clone_dir)

    hub = FrameHub()
    manager = BrowserManager(clone_dir, hub)
    manager.start()

    if env_bool(project_env, "AUTO_LOGIN", False):
        print(f"[{clone_name}] Auto-login started.", flush=True)
        try:
            run_auto_login(clone_dir, manager, project_env)
            write_auto_login_marker(clone_dir, AUTO_LOGIN_DONE_FILE, f"completed {time.time()}\n")
            print(f"[{clone_name}] Auto-login completed.", flush=True)
        except Exception as exc:
            write_auto_login_marker(clone_dir, AUTO_LOGIN_FAILED_FILE, f"{exc}\n")
            print(f"[{clone_name}] Auto-login failed: {exc}", file=sys.stderr, flush=True)

    handler = make_handler(manager, hub, base_path, clone_name)
    server = ExclusiveThreadingHTTPServer((HOST, port), handler)
    thread = threading.Thread(target=server.serve_forever, name=f"{clone_name}-http", daemon=True)
    thread.start()

    print(f"[{clone_name}] Chromium profile: {clone_dir / 'profile'}")
    print(f"[{clone_name}] Remote URL: http://SERVER_IP:{port}{base_path}")
    print(f"[{clone_name}] Listening on {HOST}:{port}")

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        server.shutdown()
        server.server_close()
        manager.stop()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clone", required=True, help="Path to a clone folder, for example .\\clones\\chromium-01")
    args = parser.parse_args()

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
