"""Test the hosting entry point without launching real services."""
from unittest.mock import Mock, patch

import pytest

import host


@pytest.mark.parametrize("value", ["", "bad", "0", "65536"])
def test_invalid_port(monkeypatch, value):
    monkeypatch.setenv("SERVER_PORT", value)
    with pytest.raises(ValueError):
        host.api_port()


def test_host_port(monkeypatch):
    monkeypatch.setenv("SERVER_PORT", "12345")
    assert host.api_port() == 12345


def test_child_exit_stops_other_service(monkeypatch):
    monkeypatch.setenv("SERVER_PORT", "12345")
    first, second = Mock(), Mock()
    first.poll.return_value = 3
    second.poll.return_value = None
    with patch("dashboard.api.create_app"), patch("host.signal.signal"), patch(
        "host.subprocess.Popen", side_effect=[first, second]
    ) as launch:
        assert host.main() == 3
    args = launch.call_args_list[0].args[0]
    assert args[args.index("--port") + 1] == "12345"
    assert "0.0.0.0" in args
    assert launch.call_args_list[1].args[0][-1] == "bot.main"
    second.terminate.assert_called_once()
    second.wait.assert_called_once()
