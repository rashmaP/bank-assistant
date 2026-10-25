"""Tests for run_dev.py (stdlib only, so these run anywhere, including on Windows)."""
import socket
import sys

import pytest

import run_dev
from run_dev import LaunchError, Service, check_key, is_placeholder, parse_env, port_free, wait_healthy

REAL_ANTHROPIC = "sk-ant-api03-" + "a1B2" * 15


def test_parse_env_handles_comments_quotes_export_and_inline_comments():
    env = parse_env('''
# a comment
LLM_PROVIDER=openai   # inline comment
OPENAI_API_KEY="gsk_abc#123"
export A=1
EMPTY=
SPACED = value with spaces
not a line
''')
    assert env == {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "gsk_abc#123", "A": "1", "EMPTY": "", "SPACED": "value with spaces"}


def test_real_environment_overrides_dotenv_and_empty_values_are_ignored(tmp_path):
    f = tmp_path / ".env"
    f.write_text("A=from_file\nB=from_file\n")
    env = run_dev.build_env(f, {"A": "from_shell", "B": ""})
    assert env["A"] == "from_shell" and env["B"] == "from_file"


def test_placeholder_detection():
    for v in ("", "sk-ant-...", "sk-ant-xxxxxxxx", "your-key-here", "<key>", "paste-it"):
        assert is_placeholder(v)
    assert not is_placeholder(REAL_ANTHROPIC)


def test_check_key_anthropic():
    assert check_key({"ANTHROPIC_API_KEY": REAL_ANTHROPIC}) == []
    assert "not set" in check_key({})[0]
    assert "placeholder" in check_key({"ANTHROPIC_API_KEY": "sk-ant-xxx"})[0]
    assert "placeholder" in check_key({"ANTHROPIC_API_KEY": "not-an-anthropic-key-0123456789"})[0]


def test_check_key_openai_and_bad_provider():
    ok = {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "gsk_" + "Zq9" * 12}
    assert check_key(ok) == []
    assert "not set" in check_key({"LLM_PROVIDER": "openai"})[0]
    assert "placeholder" in check_key({"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "gsk_..."})[0]
    assert "must be" in check_key({"LLM_PROVIDER": "gemini"})[0]


def test_python_version_check():
    assert run_dev.check_python((3, 12)) == []
    assert "3.11+" in run_dev.check_python((3, 9))[0]


def test_port_free_detects_a_busy_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    busy = s.getsockname()[1]
    try:
        assert not port_free(busy)
    finally:
        s.close()
    assert port_free(busy)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


SERVER = (
    "import http.server, sys\n"
    "class H(http.server.BaseHTTPRequestHandler):\n"
    "    def do_GET(self):\n"
    "        self.send_response(200); self.end_headers(); self.wfile.write(b'ok')\n"
    "    def log_message(self, *a): pass\n"
    "http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])), H).serve_forever()\n"
)


def test_service_starts_becomes_healthy_and_stops_cleanly():
    import os
    port = _free_port()
    svc = Service("fake", [sys.executable, "-c", SERVER, str(port)], dict(os.environ), echo=False)
    try:
        svc.start()
        wait_healthy(svc, f"http://127.0.0.1:{port}/healthz", timeout=15)
        assert svc.alive()
    finally:
        svc.stop()
    assert not svc.alive()


def test_crash_during_startup_is_reported_with_its_output():
    import os
    svc = Service("crashy", [sys.executable, "-c", "print('boom: bad config'); raise SystemExit(3)"], dict(os.environ), echo=False)
    svc.start()
    with pytest.raises(LaunchError) as e:
        wait_healthy(svc, f"http://127.0.0.1:{_free_port()}/healthz", timeout=15)
    assert "crashy exited during startup" in str(e.value) and "boom: bad config" in str(e.value)


def test_check_mode_reports_problems_and_exits_2(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(run_dev, "build_env", lambda *a, **k: {"LLM_PROVIDER": "openai"})
    monkeypatch.setattr(run_dev, "check_deps", lambda provider: [])
    assert run_dev.main(["--check"]) == 2
    assert "OPENAI_API_KEY is not set" in capsys.readouterr().out


def test_check_mode_passes_with_valid_setup(monkeypatch, capsys):
    monkeypatch.setattr(run_dev, "build_env", lambda *a, **k: {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "gsk_" + "Zq9" * 12})
    monkeypatch.setattr(run_dev, "check_deps", lambda provider: [])
    monkeypatch.setattr(run_dev, "port_free", lambda p, host="127.0.0.1": True)
    assert run_dev.main(["--check"]) == 0
    assert "All checks passed" in capsys.readouterr().out
