#!/usr/bin/env python3
"""One-command developer launcher (standard library only; works on Windows, macOS, Linux).

    python run_dev.py            # check setup, start mock bank + assistant, print the URL
    python run_dev.py --check    # only run the pre-flight checks
    python run_dev.py --evals    # run the live-model evals (extra args are passed through)
    python run_dev.py --port 9000 --bank-port 9001

It reads `.env` (real environment variables win over the file), refuses to start with a clear
message if something is missing, then runs both services and stops both on Ctrl+C.
"""
from __future__ import annotations

import argparse
import collections
import importlib.util
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MIN_PY = (3, 11)


class LaunchError(Exception):
    pass


# ----------------------------------------------------------------- config
def parse_env(text: str) -> dict[str, str]:
    """Minimal .env parser: KEY=VALUE, comments, optional quotes, `export` prefix, inline ` # comments`."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        key, value = line.split("=", 1)
        value = value.strip()
        if value and value[0] in "\"'":
            end = value.find(value[0], 1)
            value = value[1:end] if end != -1 else value[1:]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        out[key.strip()] = value
    return out


def load_dotenv(path: Path) -> dict[str, str]:
    return parse_env(path.read_text(encoding="utf-8-sig")) if path.exists() else {}


def build_env(dotenv_path: Path = ROOT / ".env", environ=None) -> dict[str, str]:
    """.env values first, then real environment variables on top (empty ones are ignored)."""
    shell = {k: v for k, v in (os.environ if environ is None else environ).items() if v != ""}
    return {**load_dotenv(dotenv_path), **shell}


# -------------------------------------------------------------- pre-flight
_PLACEHOLDER_HINTS = ("...", "xxx", "your", "paste", "changeme", "change-me", "<", "example")


def is_placeholder(value: str) -> bool:
    v = value.strip().lower()
    return not v or any(h in v for h in _PLACEHOLDER_HINTS)


def check_python(version=None) -> list[str]:
    v = tuple(version or sys.version_info[:2])
    if v < MIN_PY:
        return [f"Python {MIN_PY[0]}.{MIN_PY[1]}+ is required (you have {v[0]}.{v[1]}). Install a newer Python from python.org."]
    return []


def check_deps(provider: str) -> list[str]:
    needed = ["fastapi", "uvicorn", "httpx", "pydantic"] + (["anthropic"] if provider == "anthropic" else [])
    missing = [m for m in needed if importlib.util.find_spec(m) is None]
    if missing:
        return [f"Missing Python packages: {', '.join(missing)}. Run:  {sys.executable} -m pip install -r requirements.txt"]
    return []


def check_key(env: dict[str, str]) -> list[str]:
    provider = env.get("LLM_PROVIDER", "anthropic").lower()
    if provider == "anthropic":
        key = env.get("ANTHROPIC_API_KEY", "")
        if not key.strip():
            return ["ANTHROPIC_API_KEY is not set. Put it in .env (get one at https://console.anthropic.com), "
                    "or use a free provider: set LLM_PROVIDER=openai and OPENAI_API_KEY (Groq: https://console.groq.com)."]
        if is_placeholder(key) or not key.startswith("sk-ant-"):
            return ["ANTHROPIC_API_KEY looks like a placeholder, not a real key (real keys start with 'sk-ant-' and are long)."]
        return []
    if provider == "openai":
        key = env.get("OPENAI_API_KEY", "")
        if not key.strip():
            return ["OPENAI_API_KEY is not set. Put your provider key in .env (free Groq key: https://console.groq.com)."]
        if is_placeholder(key):
            return ["OPENAI_API_KEY looks like a placeholder, not a real key."]
        return []
    return [f"LLM_PROVIDER must be 'anthropic' or 'openai' (got '{provider}')."]


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def preflight(env: dict[str, str], ports: list[int] | None) -> list[str]:
    provider = env.get("LLM_PROVIDER", "anthropic").lower()
    problems = check_python() + check_deps(provider) + check_key(env)
    for p in ports or []:
        if not port_free(p):
            problems.append(f"Port {p} is already in use. Stop whatever is using it, or choose another with --port / --bank-port.")
    if problems and not (ROOT / ".env").exists() and any("not set" in x for x in problems):
        problems.append("There is no .env file yet. Create one:  copy .env.example .env  (Windows)  or  cp .env.example .env")
    return problems


# --------------------------------------------------------------- processes
class Service:
    def __init__(self, name: str, cmd: list[str], env: dict[str, str], echo: bool = True):
        self.name, self.cmd, self.env, self.echo = name, cmd, env, echo
        self.lines: collections.deque[str] = collections.deque(maxlen=40)
        self.proc: subprocess.Popen | None = None
        self._pump_thread: threading.Thread | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(
            self.cmd, cwd=ROOT, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        self._pump_thread = threading.Thread(target=self._pump, daemon=True)
        self._pump_thread.start()

    def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.rstrip()
            self.lines.append(line)
            if self.echo:
                print(f"[{self.name}] {line}", flush=True)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if not self.proc or self.proc.poll() is not None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)


def wait_healthy(svc: Service, url: str, timeout: float = 30.0, interval: float = 0.3) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not svc.alive():
            if svc._pump_thread:
                svc._pump_thread.join(timeout=1)
            tail = "\n".join(svc.lines) or "(no output)"
            raise LaunchError(f"{svc.name} exited during startup. Last output:\n{tail}")
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(interval)
    raise LaunchError(f"{svc.name} did not become healthy at {url} within {timeout:.0f}s.")


# -------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="only run the pre-flight checks")
    ap.add_argument("--evals", action="store_true", help="run the live-model evals instead of the servers")
    ap.add_argument("--port", type=int, default=8000, help="assistant (and UI) port")
    ap.add_argument("--bank-port", type=int, default=8001, help="mock bank port")
    args, extra = ap.parse_known_args(argv)

    env = build_env()
    env.setdefault("LOG_FORMAT", "text")  # readable in a shared terminal
    env.setdefault("DEMO_AUTH_ENABLED", "true")
    env.update(PYTHONUNBUFFERED="1", PYTHONUTF8="1", BANK_BASE_URL=f"http://127.0.0.1:{args.bank_port}")

    ports = None if args.evals else [args.port, args.bank_port]
    problems = preflight(env, ports)
    if problems:
        print("Cannot start yet:\n")
        for p in problems:
            print(f"  - {p}")
        return 2
    if args.check:
        print("All checks passed.")
        return 0
    if args.evals:
        return subprocess.call([sys.executable, "-m", "evals.run_evals", *extra], cwd=ROOT, env=env)
    if extra:
        ap.error(f"unrecognized arguments: {' '.join(extra)}")

    py = sys.executable
    bank = Service("bank", [py, "-m", "uvicorn", "mock_bank.main:app", "--port", str(args.bank_port)], env)
    assistant = Service("assistant", [py, "-m", "uvicorn", "assistant.main:create_app", "--factory",
                                      "--port", str(args.port)], env)
    services = [bank, assistant]
    try:
        bank.start()
        wait_healthy(bank, f"http://127.0.0.1:{args.bank_port}/healthz")
        assistant.start()
        wait_healthy(assistant, f"http://127.0.0.1:{args.port}/healthz")
        print(f"\n  Ready.  Open http://localhost:{args.port}   (Ctrl+C to stop both services)\n", flush=True)
        while all(s.alive() for s in services):
            time.sleep(0.5)
        dead = next(s for s in services if not s.alive())
        print(f"\n{dead.name} stopped unexpectedly. Last output:\n" + "\n".join(dead.lines))
        return 1
    except LaunchError as e:
        print(f"\n{e}")
        return 1
    except KeyboardInterrupt:
        print("\nStopping...")
        return 0
    finally:
        for s in reversed(services):
            s.stop()


if __name__ == "__main__":
    sys.exit(main())
