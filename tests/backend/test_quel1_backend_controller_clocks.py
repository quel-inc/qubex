# ruff: noqa: SLF001

"""Tests for clock interactions in Quel1BackendController."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from typing import Any, ClassVar, cast

import pytest

from qubex.backend.quel1.quel1_backend_controller import Quel1BackendController
from qubex.backend.quel1.quel1_runtime_context import NOT_CONNECTED_ERROR_MESSAGE

pytestmark = pytest.mark.usefixtures("stub_quel1_driver")


@dataclass
class _ClockmasterSetting:
    ipaddr: str


@dataclass
class _BoxSetting:
    ipaddr_sss: str


class _FakeSysDb:
    def __init__(self, *, include_clockmaster: bool = True) -> None:
        self._box_settings = {
            "A": _BoxSetting("10.0.1.1"),
            "B": _BoxSetting("10.0.1.2"),
        }
        self._clockmaster_setting = (
            _ClockmasterSetting("192.0.2.1") if include_clockmaster else None
        )

    def asdict(self) -> dict[str, Any]:
        """Return a minimal system config payload."""
        return {
            "box_settings": {
                name: {"ipaddr_sss": setting.ipaddr_sss}
                for name, setting in self._box_settings.items()
            }
        }


class _FakeQubeCalib:
    def __init__(self, *, include_clockmaster: bool = True) -> None:
        self.sysdb = _FakeSysDb(include_clockmaster=include_clockmaster)
        self.system_config_database = self.sysdb


class _FakeSequencerClient:
    calls: ClassVar[list[str]] = []
    readings: ClassVar[dict[str, tuple[bool, int, int]]] = {}

    def __init__(self, *, target_ipaddr: str) -> None:
        self._target_ipaddr = target_ipaddr

    def read_clock(self) -> tuple[bool, int, int]:
        """Return the preconfigured reading for the target."""
        self.calls.append(self._target_ipaddr)
        return self.readings[self._target_ipaddr]


class _FakeQuBEMasterClient:
    def __init__(self, master_ipaddr: str) -> None:
        self.master_ipaddr = master_ipaddr
        self.kick_calls: list[list[str]] = []
        self.reset_calls = 0

    def kick_clock_synch(self, target_addrs: list[str]) -> bool:
        """Record target addresses and report success."""
        self.kick_calls.append(list(target_addrs))
        return True

    def reset(self) -> bool:
        """Record reset requests and report success."""
        self.reset_calls += 1
        return True


class _FakeQuel1System:
    def __init__(self, clockmaster: _FakeQuBEMasterClient) -> None:
        self._clockmaster = clockmaster


class _FakeBox:
    def __init__(self, *, current: int, sysref: int) -> None:
        self.current = current
        self.sysref = sysref

    def get_current_timecounter(self) -> int:
        """Return the preconfigured current counter."""
        return self.current

    def get_latest_sysref_timecounter(self) -> int:
        """Return the preconfigured SYSREF counter."""
        return self.sysref


class _FakeClockMaster:
    def __init__(self) -> None:
        self.sync_calls: list[list[_FakeBox]] = []

    def sync_boxes(self, boxes: list[_FakeBox]) -> None:
        """Record the boxes synchronized together."""
        self.sync_calls.append(list(boxes))


class _FakeBoxPool:
    def __init__(
        self, *, boxes: dict[str, _FakeBox], clock_master: _FakeClockMaster | None
    ) -> None:
        self._boxes = {name: (box, object()) for name, box in boxes.items()}
        self._clock_master = clock_master


def _make_controller(*, include_clockmaster: bool = True) -> Quel1BackendController:
    controller = Quel1BackendController()
    fake_qubecalib = _FakeQubeCalib(include_clockmaster=include_clockmaster)
    cast(Any, controller)._qubecalib = fake_qubecalib
    cast(Any, controller)._runtime_context._qubecalib = fake_qubecalib
    return controller


def _connect(
    controller: Quel1BackendController,
    *,
    boxes: dict[str, _FakeBox],
    clock_master: _FakeClockMaster | None,
) -> None:
    controller._connection_manager.set_connected_state(
        boxpool=cast(Any, _FakeBoxPool(boxes=boxes, clock_master=clock_master)),
        quel1system=cast(Any, object()),
        cap_resource_map={},
        gen_resource_map={},
    )


def _override_driver_classes(
    controller: Quel1BackendController, **overrides: Any
) -> None:
    """Replace selected driver classes in one controller instance."""
    driver = replace(cast(Any, controller.driver), **overrides)
    cast(Any, controller)._runtime_context._driver = driver


def test_read_clocks_uses_sequencer_clients() -> None:
    """Given boxes, read_clocks returns sequencer client readings."""
    controller = _make_controller()
    _FakeSequencerClient.calls = []
    _FakeSequencerClient.readings = {
        "10.0.1.1": (True, 123, 456),
        "10.0.1.2": (True, 789, 1011),
    }
    _override_driver_classes(
        controller, package_name="qubecalib", SequencerClient=_FakeSequencerClient
    )

    result = controller.read_clocks(["A", "B"])

    assert result == [(True, 123, 456), (True, 789, 1011)]
    assert _FakeSequencerClient.calls == ["10.0.1.1", "10.0.1.2"]


def test_resync_clocks_kicks_clockmaster() -> None:
    """Given multiple boxes, resync_clocks kicks clockmaster and checks."""
    controller = _make_controller()
    _FakeSequencerClient.readings = {
        "10.0.1.1": (True, 123_456_789_000, 123_456_789_999),
        "10.0.1.2": (True, 123_456_789_000, 123_456_789_999),
    }
    master = _FakeQuBEMasterClient("192.0.2.1")

    _override_driver_classes(
        controller,
        package_name="qubecalib",
        SequencerClient=_FakeSequencerClient,
        QuBEMasterClient=lambda master_ipaddr: master,
    )

    assert controller.resync_clocks(["A", "B"]) is True
    assert master.master_ipaddr == "192.0.2.1"
    assert master.kick_calls == [["10.0.1.1", "10.0.1.2"]]


def test_resync_clocks_reuses_connected_quel1system_clockmaster() -> None:
    """Given connected system, resync_clocks reuses its clockmaster."""
    controller = _make_controller()
    _FakeSequencerClient.readings = {
        "10.0.1.1": (True, 123_456_789_000, 123_456_789_999),
        "10.0.1.2": (True, 123_456_789_000, 123_456_789_999),
    }
    master = _FakeQuBEMasterClient("192.0.2.1")
    controller._connection_manager.set_quel1system(
        cast(Any, _FakeQuel1System(clockmaster=master))
    )

    def _raise_if_instantiated(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("QuBEMasterClient must not be instantiated")

    _override_driver_classes(
        controller,
        package_name="qubecalib",
        SequencerClient=_FakeSequencerClient,
        QuBEMasterClient=_raise_if_instantiated,
    )

    assert controller.resync_clocks(["A", "B"]) is True
    assert master.kick_calls == [["10.0.1.1", "10.0.1.2"]]


def test_resync_clocks_raises_without_clockmaster() -> None:
    """Given no clockmaster, resync_clocks raises ValueError."""
    controller = _make_controller(include_clockmaster=False)
    _override_driver_classes(
        controller, package_name="qubecalib", QuBEMasterClient=_FakeQuBEMasterClient
    )

    with pytest.raises(ValueError, match="clock master is not found"):
        controller.resync_clocks(["A", "B"])


def test_reset_clockmaster_uses_master_client() -> None:
    """Given an IP address, reset_clockmaster delegates to QuBEMasterClient.reset."""
    controller = _make_controller()
    master = _FakeQuBEMasterClient("192.0.2.99")

    _override_driver_classes(
        controller,
        package_name="qubecalib",
        QuBEMasterClient=lambda master_ipaddr: master,
    )

    assert controller.reset_clockmaster("192.0.2.99") is True
    assert master.master_ipaddr == "192.0.2.99"
    assert master.reset_calls == 1


def test_reset_clockmaster_reuses_connected_quel1system_clockmaster() -> None:
    """Given connected system, reset_clockmaster reuses its clockmaster."""
    controller = _make_controller()
    master = _FakeQuBEMasterClient("192.0.2.1")
    controller._connection_manager.set_quel1system(
        cast(Any, _FakeQuel1System(clockmaster=master))
    )

    def _raise_if_instantiated(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("QuBEMasterClient must not be instantiated")

    _override_driver_classes(
        controller,
        package_name="qubecalib",
        QuBEMasterClient=_raise_if_instantiated,
    )

    assert controller.reset_clockmaster("192.0.2.1") is True
    assert master.reset_calls == 1


def test_reset_clockmaster_returns_false_when_reset_is_unsupported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Given qxdriver, reset_clockmaster warns and returns False without a client."""
    controller = _make_controller()

    with caplog.at_level(logging.WARNING):
        assert controller.reset_clockmaster("192.0.2.99") is False

    assert "Clock master reset is not supported" in caplog.text


def test_read_clocks_reads_connected_boxes() -> None:
    """Given qxdriver, read_clocks reads the counters of the connected boxes."""
    controller = _make_controller()
    _connect(
        controller,
        boxes={
            "A": _FakeBox(current=123, sysref=456),
            "B": _FakeBox(current=789, sysref=1011),
        },
        clock_master=_FakeClockMaster(),
    )

    assert controller.read_clocks(["A", "B"]) == [(True, 123, 456), (True, 789, 1011)]


def test_read_clocks_raises_for_box_out_of_connection() -> None:
    """Given qxdriver, read_clocks rejects a box that the connection does not hold."""
    controller = _make_controller()
    _connect(
        controller,
        boxes={"A": _FakeBox(current=123, sysref=456)},
        clock_master=_FakeClockMaster(),
    )

    with pytest.raises(ValueError, match=r"box\(B\) is not connected"):
        controller.read_clocks(["A", "B"])


def test_read_clocks_raises_when_disconnected() -> None:
    """Given qxdriver and no connection, read_clocks raises the not-connected error."""
    controller = _make_controller()

    with pytest.raises(ValueError, match=re.escape(NOT_CONNECTED_ERROR_MESSAGE)):
        controller.read_clocks(["A", "B"])


def test_resync_clocks_syncs_connected_boxes_together() -> None:
    """Given qxdriver, resync_clocks syncs the connected boxes in one call and checks."""
    controller = _make_controller()
    boxes = {
        "A": _FakeBox(current=123_456_789_000, sysref=123_456_789_999),
        "B": _FakeBox(current=123_456_789_000, sysref=123_456_789_999),
    }
    master = _FakeClockMaster()
    _connect(controller, boxes=boxes, clock_master=master)

    assert controller.resync_clocks(["A", "B"]) is True
    assert master.sync_calls == [[boxes["A"], boxes["B"]]]


def test_resync_clocks_raises_without_quel_clockmaster() -> None:
    """Given qxdriver and a connection without clock master, resync_clocks raises."""
    controller = _make_controller(include_clockmaster=False)
    _connect(
        controller,
        boxes={
            "A": _FakeBox(current=1, sysref=1),
            "B": _FakeBox(current=1, sysref=1),
        },
        clock_master=None,
    )

    with pytest.raises(ValueError, match="clock master is not found"):
        controller.resync_clocks(["A", "B"])
