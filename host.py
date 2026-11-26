"""Single entry point for Bot-Hosting.net: one bot poller and one API worker."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from bot.config import _load_dotenv


def api_port() -> int:
    try:
        port = int(os.environ["SERVER_PORT"])
    except (KeyError, ValueError) as exc:
        raise ValueError("SERVER_PORT must be set by the hosting platform") from exc
    if not 1 <= port <= 65535:
        raise ValueError("SERVER_PORT is outside the valid range")
    return port


def stop_children(children):
    for child in children:
        if child.poll() is None:
            child.terminate()
    deadline = time.monotonic() + 15
    for child in children:
        try:
            child.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def main() -> int:
    _load_dotenv()
    port = api_port()
    # Validate API settings before starting Telegram polling.
    from dashboard.api import create_app
    create_app()
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    children = []
    commands = [
        [sys.executable, "-m", "uvicorn", "dashboard.api:create_app", "--factory",
         "--host", "0.0.0.0", "--port", str(port), "--workers", "1"],
        [sys.executable, "-m", "bot.main"],
    ]
    try:
        for command in commands:
            if stopping:
                return 0
            children.append(subprocess.Popen(command))
        while not stopping:
            for child in children:
                result = child.poll()
                if result is not None:
                    print("HyperBot or API exited; stopping both processes.", flush=True)
                    return result if result > 0 else 1
            time.sleep(0.25)
        return 0
    finally:
        stop_children(children)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
