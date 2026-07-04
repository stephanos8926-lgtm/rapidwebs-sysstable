"""Tests for the Resolution Executor."""

from unittest.mock import MagicMock, patch

from sysstable.resolver import MemoryPressureResolver


def _make_entry(pid=1001, name="test", score=0.9, mem_mb=200.0, cpu=50.0, reason="high_mem"):
    from sysstable.process_watch import KillListEntryScore

    return KillListEntryScore(
        pid=pid,
        name=name,
        cmdline=f"/usr/bin/{name}",
        score=score,
        memory_mb=mem_mb,
        cpu_percent=cpu,
        reason=reason,
    )


@patch("sysstable.resolver.psutil.Process")
def test_kill_process_terminates(mock_proc_class):
    resolver = MemoryPressureResolver({}, object())
    mock_proc = MagicMock()
    mock_proc.is_running.return_value = True
    mock_proc.name.return_value = "firefox"
    mock_proc.cmdline.return_value = ["/usr/bin/firefox"]
    mock_proc_class.return_value = mock_proc

    entry = _make_entry(1001, "firefox", mem_mb=500.0)
    details = []
    result = resolver._kill_process(entry, 15, details)
    assert result is True
    mock_proc.send_signal.assert_called()
    assert len(details) == 1
    assert details[0]["action"] == "kill"
    assert details[0]["pid"] == 1001


@patch("sysstable.resolver.psutil.Process")
def test_skip_already_dead(mock_proc_class):
    resolver = MemoryPressureResolver({}, object())
    mock_proc = MagicMock()
    mock_proc.is_running.return_value = False
    mock_proc_class.return_value = mock_proc

    entry = _make_entry(9999, "gone")
    details = []
    result = resolver._kill_process(entry, 15, details)
    assert result is True  # already dead = success
    mock_proc.send_signal.assert_not_called()


@patch("sysstable.resolver.psutil.Process")
def test_pause_process(mock_proc_class):
    resolver = MemoryPressureResolver({}, object())
    mock_proc = MagicMock()
    mock_proc.is_running.return_value = True
    mock_proc_class.return_value = mock_proc

    entry = _make_entry(2001, "bash")
    details = []
    result = resolver._pause_process(entry, details)
    assert result is True
    mock_proc.send_signal.assert_called_once()


def test_re_entrance_guard():
    resolver = MemoryPressureResolver({}, object())
    resolver._resolving = True
    result = resolver.resolve([], 500.0)
    assert result.success is False
    assert "REJECTED" in result.action_summary


def test_empty_kill_list():
    resolver = MemoryPressureResolver({}, object())
    result = resolver.resolve([], 500.0)
    assert result.success is False
    assert "EMPTY" in result.action_summary


@patch("sysstable.resolver.psutil.virtual_memory")
@patch("sysstable.resolver.MemoryPressureResolver._kill_process")
@patch("sysstable.resolver.MemoryPressureResolver._pause_process")
def test_full_resolve_cycle(mock_pause, mock_kill, mock_ram):
    mock_ram.return_value.available = 300 * 1024 * 1024  # 300MB after
    mock_kill.return_value = True
    mock_pause.return_value = True

    resolver = MemoryPressureResolver({}, object())
    entry = _make_entry(1001, "firefox", mem_mb=500.0)
    entry2 = _make_entry(1002, "chrome", mem_mb=300.0)

    result = resolver.resolve([entry, entry2], 200.0)
    assert mock_kill.called
    assert result.kill_count == 1


@patch("sysstable.resolver.psutil.Process")
def test_kill_permission_error(mock_proc_class):
    resolver = MemoryPressureResolver({}, object())
    import psutil

    mock_proc = MagicMock()
    mock_proc.is_running.return_value = True
    mock_proc.name.return_value = "protected"
    mock_proc.cmdline.return_value = ["/usr/bin/protected"]
    mock_proc.send_signal.side_effect = psutil.AccessDenied()
    mock_proc_class.return_value = mock_proc

    entry = _make_entry(3001, "protected")
    details = []
    result = resolver._kill_process(entry, 15, details)
    assert result is False


@patch("sysstable.resolver.MemoryPressureResolver._kill_process")
@patch("sysstable.resolver.MemoryPressureResolver._pause_process")
@patch("sysstable.resolver.psutil.virtual_memory")
def test_insufficient_freed_memory_triggers_fail(mock_ram, mock_pause, mock_kill):
    # Only freed 30MB (below 64MB min)
    mock_ram.return_value.available = 230 * 1024 * 1024
    mock_kill.return_value = True
    mock_pause.return_value = True

    resolver = MemoryPressureResolver({}, object())
    entry = _make_entry(1001, "test", mem_mb=100.0)
    result = resolver.resolve([entry], 200.0)
    assert result.success is False
    assert "INSUFFICIENT" in result.action_summary
