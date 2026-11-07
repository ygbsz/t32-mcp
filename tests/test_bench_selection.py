"""Hardware test discovery must never take over a busy probe."""
from unittest.mock import Mock

import pytest

import test_live as live
from t32_mcp.powerview import Device


@pytest.mark.parametrize("busy, expected", [
    ([], None),
    ([True], None),
    ([False, True], None),
    ([False, False], None),
    ([False], "bench-0"),
])
def test_bench_scan_only_accepts_one_free_probe(tmp_path, monkeypatch, busy, expected):
    install = Mock(spec=live.Install)
    install.executable.return_value.is_file.return_value = True
    monkeypatch.setattr(live.Install, "locate", lambda: install)
    monkeypatch.setattr(live, "running_instances", lambda: [])
    instance = Mock(spec=live.Instance)
    monkeypatch.setattr(live.Instance, "launch", Mock(return_value=instance))
    devices = [Device(i, f"bench-{i}", "Mock PowerDebug", "USB", held) for i, held in enumerate(busy)]
    monkeypatch.setattr(live, "scan", lambda conn: devices)
    reset = Mock(side_effect=AssertionError("bench discovery attempted a probe reset"))
    monkeypatch.setattr(live.powerview, "reset_device", reset)

    assert live.free_bench_serial(tmp_path) == expected
    reset.assert_not_called()
    instance.quit.assert_called_once_with(instance.connect.return_value)
