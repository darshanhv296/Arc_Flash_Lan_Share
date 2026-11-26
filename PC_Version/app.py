#!/usr/bin/env python3
"""
Arc Flash PC Companion — High-Speed LAN File Sharing (PC <-> Android & PC <-> PC).

Launches:
- Dual-mode LAN server on port 53317 (AF01 high-speed binary + HTTP/1.1 + Web UI)
- UDP LAN discovery on port 45890 (+ optional mDNS if `zeroconf` is installed)
- Automatically opens the Arc Flash desktop Web UI in the default browser
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

from arc_protocol import DEFAULT_PORT
from discovery import QrPayloadCodec
from transfer_engine import ArcTransferManager


def default_config_path() -> Path:
    if getattr(sys, "frozen", False):
        app_data = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        config_dir = app_data / "ArcFlash"
        config_dir.mkdir(parents=True, exist_ok=True)
        return config_dir / "pc_config.json"
    return Path(__file__).resolve().parent / "pc_config.json"


def default_save_dir() -> Path:
    # Global default storage folder: C:\ARC-FLASH (auto-created on first run).
    # Users can override via Settings later.
    return Path(r"C:\ARC-FLASH") / "ArcFlash"


def load_or_init_config(config_path: Path) -> dict:
    if config_path.is_file():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("device_id"):
                return data
        except Exception:
            pass
    hostname = socket.gethostname().strip() or "Arc-PC"
    cfg = {
        "device_id": QrPayloadCodec.new_device_id(),
        "device_name": f"{hostname}",
        "save_dir": str(default_save_dir()),
        "theme": "system",
    }
    save_config(config_path, cfg)
    return cfg


def save_config(config_path: Path, data: dict) -> None:
    try:
        config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Arc Flash PC — High-Speed LAN File Sharing")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Preferred LAN port (default: 53317)")
    parser.add_argument("--save-dir", type=str, default=None, help="Directory to store received files")
    parser.add_argument("--name", type=str, default=None, help="Display name visible to nearby devices")
    parser.add_argument("--no-ui", action="store_true", help="Run server only without opening the UI window")
    args = parser.parse_args()

    cfg_path = default_config_path()
    cfg = load_or_init_config(cfg_path)

    if args.name:
        cfg["device_name"] = args.name.strip()[:40]
    if args.save_dir:
        cfg["save_dir"] = os.path.abspath(os.path.expanduser(args.save_dir))

    manager = ArcTransferManager(
        device_id=str(cfg["device_id"]),
        device_name=str(cfg["device_name"]),
        save_dir=str(cfg.get("save_dir") or default_save_dir()),
        theme=str(cfg.get("theme") or "system"),
        preferred_port=args.port,
        on_config_save=lambda updated: save_config(cfg_path, updated),
    )

    # Start the LAN server only when the app is opened.
    actual_port = manager.start()
    ui_url = f"http://127.0.0.1:{actual_port}/"
    print(f"Arc Flash PC ready for '{manager.device_name}'")
    print(f"Desktop UI: {ui_url}")
    print(f"Received files save to: {manager.save_dir}")
    print("Close the window to stop the server.")

    if not args.no_ui:
        try:
            import webview
        except ImportError as exc:
            print(f"pywebview not available, falling back to default browser: {exc}")
            import webbrowser
            webbrowser.open(ui_url)
        else:
            window = webview.create_window(
                "Arc Flash PC",
                ui_url,
                width=900,
                height=700,
                min_size=(700, 500),
            )

            def _on_closed() -> None:
                print("Arc Flash PC window closed — stopping server...")
                manager.stop()

            window.events.closed += _on_closed
            webview.start()
    else:
        print("Server running (--no-ui). Press Ctrl+C to stop.")
        stop_event = threading.Event()

        def _on_signal(_sig, _frame):
            stop_event.set()

        signal.signal(signal.SIGINT, _on_signal)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _on_signal)

        try:
            while not stop_event.is_set():
                time.sleep(0.5)
        finally:
            print("\nShutting down Arc Flash PC...")
            manager.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
