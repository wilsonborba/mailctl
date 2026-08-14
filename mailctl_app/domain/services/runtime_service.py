from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SERVICE_NAME = "mailctl-api.service"
DEFAULT_API_HOST = "0.0.0.0"
DEFAULT_API_PORT = 18080




def is_port_available(host: str, port: int) -> bool:
    bind_host = "127.0.0.1" if host in {"localhost", "127.0.0.1"} else "0.0.0.0"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((bind_host, port))
        except OSError:
            return False
    return True


def find_available_port(host: str, preferred_port: int) -> tuple[int, bool]:
    if is_port_available(host, preferred_port):
        return preferred_port, False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        bind_host = "127.0.0.1" if host in {"localhost", "127.0.0.1"} else "0.0.0.0"
        sock.bind((bind_host, 0))
        return int(sock.getsockname()[1]), True


def wait_for_port(host: str, port: int, *, timeout_seconds: float = 5.0) -> bool:
    connect_host = "127.0.0.1" if host in {"0.0.0.0", "::", "localhost"} else host
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex((connect_host, port)) == 0:
                return True
        time.sleep(0.1)
    return False

def runtime_root() -> Path:
    return Path.home() / ".local" / "share" / "mailctl"


def runtime_venv_path() -> Path:
    return runtime_root() / ".venv"


def runtime_python_path() -> Path:
    return runtime_venv_path() / "bin" / "python"


def service_config_path() -> Path:
    return runtime_root() / "api-service.json"


def install_manifest_path() -> Path:
    return runtime_root() / "install.json"


def systemd_user_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def systemd_unit_path() -> Path:
    return systemd_user_dir() / SERVICE_NAME


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def load_json_file(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json_file(path: Path, payload: dict[str, Any]) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_service_config() -> dict[str, Any]:
    return load_json_file(
        service_config_path(),
        {"host": DEFAULT_API_HOST, "port": DEFAULT_API_PORT, "profile": "cli"},
    )


def save_service_config(*, host: str, port: int, profile: str | None = None) -> dict[str, Any]:
    current = load_service_config()
    payload = {
        "host": host,
        "port": port,
        "profile": profile or current.get("profile", "cli"),
    }
    save_json_file(service_config_path(), payload)
    return payload


def save_install_manifest(profile: str) -> dict[str, Any]:
    payload = {"profile": profile, "service_manager": "systemd-user", "os_family": "debian"}
    save_json_file(install_manifest_path(), payload)
    return payload


def load_install_manifest() -> dict[str, Any]:
    return load_json_file(install_manifest_path(), {"profile": "cli"})


def write_systemd_unit(*, host: str, port: int) -> Path:
    ensure_dir(systemd_user_dir())
    python_bin = runtime_python_path()
    if not python_bin.exists():
        python_bin = Path(sys.executable)
    unit = "\n".join(
        [
            "[Unit]",
            "Description=mailctl API service",
            "After=network.target",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={python_bin} -m mailctl api serve --host {host} --port {port}",
            "Restart=on-failure",
            "RestartSec=2",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )
    path = systemd_unit_path()
    path.write_text(unit, encoding="utf-8")
    return path


def has_systemctl() -> bool:
    return shutil.which("systemctl") is not None


def run_systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def ensure_systemd_ready(*, host: str, port: int) -> dict[str, Any]:
    selected_port, fallback_used = find_available_port(host, port)
    unit_path = write_systemd_unit(host=host, port=selected_port)
    daemon_reload = run_systemctl("daemon-reload")
    return {
        "unit_path": str(unit_path),
        "daemon_reload_returncode": daemon_reload.returncode,
        "daemon_reload_stderr": daemon_reload.stderr.strip(),
        "requested_port": port,
        "selected_port": selected_port,
        "fallback_used": fallback_used,
    }


def service_status() -> dict[str, Any]:
    config = load_service_config()
    status = run_systemctl("is-active", SERVICE_NAME) if has_systemctl() else None
    enabled = run_systemctl("is-enabled", SERVICE_NAME) if has_systemctl() else None
    return {
        "service_name": SERVICE_NAME,
        "host": config["host"],
        "port": config["port"],
        "unit_path": str(systemd_unit_path()),
        "active": status.stdout.strip() if status else "systemctl-unavailable",
        "enabled": enabled.stdout.strip() if enabled else "systemctl-unavailable",
        "active_returncode": status.returncode if status else 127,
        "enabled_returncode": enabled.returncode if enabled else 127,
        "profile": load_install_manifest().get("profile", "cli"),
    }


def service_start(*, host: str, port: int) -> dict[str, Any]:
    readiness = ensure_systemd_ready(host=host, port=port)
    selected_port = int(readiness["selected_port"])
    save_service_config(host=host, port=selected_port)
    enable = run_systemctl("enable", SERVICE_NAME)
    start = run_systemctl("start", SERVICE_NAME)
    result = service_status()
    listening = wait_for_port(host, selected_port)
    result.update(
        {
            "enable_returncode": enable.returncode,
            "enable_stderr": enable.stderr.strip(),
            "start_returncode": start.returncode,
            "start_stderr": start.stderr.strip(),
            "systemd": readiness,
            "requested_port": port,
            "selected_port": selected_port,
            "fallback_used": readiness["fallback_used"],
            "listening": listening,
        }
    )
    return result


def service_stop() -> dict[str, Any]:
    stop = run_systemctl("stop", SERVICE_NAME) if has_systemctl() else None
    result = service_status()
    result.update(
        {
            "stop_returncode": stop.returncode if stop else 127,
            "stop_stderr": stop.stderr.strip() if stop else "systemctl not available",
        }
    )
    return result


def service_restart(*, host: str, port: int) -> dict[str, Any]:
    readiness = ensure_systemd_ready(host=host, port=port)
    selected_port = int(readiness["selected_port"])
    save_service_config(host=host, port=selected_port)
    restart = run_systemctl("restart", SERVICE_NAME)
    result = service_status()
    listening = wait_for_port(host, selected_port)
    result.update(
        {
            "restart_returncode": restart.returncode,
            "restart_stderr": restart.stderr.strip(),
            "systemd": readiness,
            "requested_port": port,
            "selected_port": selected_port,
            "fallback_used": readiness["fallback_used"],
            "listening": listening,
        }
    )
    return result

