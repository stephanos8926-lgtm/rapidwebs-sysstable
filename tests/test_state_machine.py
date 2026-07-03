"""Tests for the Pressure State Machine."""

from sysstable.state_machine import PressureStateMachine, PressureState


def test_initial_state_is_normal():
    sm = PressureStateMachine({})
    assert sm.get_state() == PressureState.NORMAL


def test_critical_detected_on_first_low_ram():
    sm = PressureStateMachine({})
    sm.update(50.0, critical_threshold_mb=128.0)
    assert sm.get_state() == PressureState.CRITICAL_DETECTED


def test_confirming_after_5_intervals():
    sm = PressureStateMachine({"memory_pressure": {"confirmation_intervals": 3}})
    for _ in range(3):
        sm.update(50.0, critical_threshold_mb=128.0)
    assert sm.get_state() == PressureState.CONFIRMING


def test_countdown_after_confirming():
    sm = PressureStateMachine({"memory_pressure": {"confirmation_intervals": 3, "countdown_seconds": 90}})
    for _ in range(4):
        sm.update(50.0, critical_threshold_mb=128.0)
    assert sm.get_state() == PressureState.COUNTDOWN


def test_recovery_when_ram_improves():
    sm = PressureStateMachine({"memory_pressure": {"confirmation_intervals": 3}})
    for _ in range(2):
        sm.update(50.0, critical_threshold_mb=128.0)
    assert sm.get_state() == PressureState.CRITICAL_DETECTED
    sm.update(500.0, critical_threshold_mb=128.0)
    assert sm.get_state() == PressureState.NORMAL


def test_resolve_on_success_goes_recovered():
    sm = PressureStateMachine({})
    sm.on_resolution_complete(success=True)
    assert sm.get_state() == PressureState.RECOVERED


def test_resolve_on_failure_retries_then_manual():
    sm = PressureStateMachine({
        "memory_pressure": {},
        "resolution": {"max_resolution_cycles": 3},
    })
    for _ in range(3):
        assert sm.get_state() != PressureState.MANUAL_INTERVENTION
        sm.on_resolution_complete(success=False)
    assert sm.get_state() == PressureState.MANUAL_INTERVENTION


def test_manual_intervention_stays_forever():
    sm = PressureStateMachine({"resolution": {"max_resolution_cycles": 1}})
    sm.on_resolution_complete(success=False)
    assert sm.get_state() == PressureState.MANUAL_INTERVENTION
    sm.update(50.0, critical_threshold_mb=128.0)
    assert sm.get_state() == PressureState.MANUAL_INTERVENTION


def test_full_lifecycle():
    sm = PressureStateMachine({
        "memory_pressure": {"confirmation_intervals": 3, "countdown_seconds": 0.01},
        "resolution": {"max_resolution_cycles": 3},
    })
    import time

    # Step through the whole lifecycle
    for _ in range(3):
        sm.update(50.0, critical_threshold_mb=128.0)
    assert sm.get_state() == PressureState.CONFIRMING

    sm.update(50.0, critical_threshold_mb=128.0)
    assert sm.get_state() == PressureState.COUNTDOWN

    time.sleep(0.02)
    sm.update(50.0, critical_threshold_mb=128.0)
    assert sm.get_state() == PressureState.RESOLVING
    assert sm.should_fire_resolution() is True

    sm.on_resolution_complete(success=True)
    assert sm.get_state() == PressureState.RECOVERED


def test_cancel_resolution():
    sm = PressureStateMachine({"memory_pressure": {"confirmation_intervals": 3}})
    for _ in range(4):
        sm.update(50.0, critical_threshold_mb=128.0)
    assert sm.get_state() == PressureState.COUNTDOWN
    sm.cancel_resolution()
    assert sm.get_state() == PressureState.NORMAL