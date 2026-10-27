# ruff: noqa: SLF001

"""Tests for connect mode switching in Quel1BackendController."""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any, cast

import pytest

from qubex.backend.quel1.managers import connection_manager
from qubex.backend.quel1.quel1_backend_constants import (
    DEFAULT_BACKGROUND_NOISE_THRESHOLD_AT_RECONNECT,
)
from qubex.backend.quel1.quel1_backend_controller import Quel1BackendController

pytestmark = pytest.mark.usefixtures("stub_quel1_driver")


@dataclass
class _ClockmasterSetting:
    ipaddr: str


@dataclass
class _BoxSetting:
    ipaddr_wss: str
    ipaddr_sss: str
    ipaddr_css: str
    boxtype: str


class _FakeBox:
    def __init__(self, name: str) -> None:
        self.name = name
        self.reconnect_count = 0
        self.reconnect_calls: list[dict[str, Any]] = []

    def reconnect(self, **kwargs: Any) -> None:
        """Increment reconnect count and record keyword arguments."""
        self.reconnect_calls.append(kwargs)
        self.reconnect_count += 1

    def dump_box(self) -> dict[str, str]:
        """Return a minimal box dump payload."""
        return {"box": self.name}


class _FakeClockMaster:
    def __init__(self, ipaddr: str) -> None:
        self.ipaddr = ipaddr
        self.terminate_calls = 0

    def terminate(self) -> None:
        """Record one terminate request."""
        self.terminate_calls += 1


class _FakeBoxPool:
    def __init__(self) -> None:
        self._boxes: dict[str, tuple[_FakeBox, object]] = {}
        self._linkstatus: dict[str, bool] = {}
        self._clock_master: _FakeClockMaster | None = None
        self.clockmaster_ip: str | None = None

    def create_clock_master(self, *, ipaddr: str) -> None:
        """Store clockmaster IP."""
        self.clockmaster_ip = ipaddr

    def set_clock_master(self, master: _FakeClockMaster) -> None:
        """Store the clock master created by the caller."""
        self._clock_master = master

    def create(
        self,
        box_name: str,
        *,
        ipaddr_wss: str,
        ipaddr_sss: str,
        ipaddr_css: str,
        boxtype: str,
    ) -> _FakeBox:
        """Create and register a fake box."""
        _ = (ipaddr_wss, ipaddr_sss, ipaddr_css, boxtype)
        box = _FakeBox(box_name)
        self._boxes[box_name] = (box, object())
        return box


class _FakeSysDb:
    def __init__(self) -> None:
        self._clockmaster_setting = _ClockmasterSetting("192.0.2.1")
        self._box_settings = {
            "A": _BoxSetting("10.0.0.1", "10.0.1.1", "10.0.2.1", "type-a"),
            "B": _BoxSetting("10.0.0.2", "10.0.1.2", "10.0.2.2", "type-b"),
        }
        self.create_quel1system_calls: list[tuple[str, ...]] = []
        self.create_box_calls: list[tuple[str, bool]] = []
        self.created_boxes: list[_FakeBox] = []

    def create_box(self, box_name: str, reconnect: bool = True) -> _FakeBox:
        """Create a fake box and record call arguments."""
        self.create_box_calls.append((box_name, reconnect))
        box = _FakeBox(box_name)
        self.created_boxes.append(box)
        return box

    def create_quel1system(self, *box_names: str) -> str:
        """Record and return a fake system marker."""
        self.create_quel1system_calls.append(tuple(box_names))
        return "legacy-system"


class _FakeQubeCalib:
    def __init__(self) -> None:
        self.sysdb = _FakeSysDb()
        self.system_config_database = self.sysdb


def _make_controller() -> Quel1BackendController:
    controller = Quel1BackendController()
    fake_qubecalib = _FakeQubeCalib()
    cast(Any, controller)._qubecalib = fake_qubecalib
    cast(Any, controller)._runtime_context._qubecalib = fake_qubecalib
    return controller


def _override_driver_classes(
    controller: Quel1BackendController, **overrides: Any
) -> None:
    """Replace selected driver classes in one controller instance."""
    driver = replace(cast(Any, controller.driver), **overrides)
    cast(Any, controller)._runtime_context._driver = driver


@pytest.fixture(autouse=True)
def clock_masters(monkeypatch: pytest.MonkeyPatch) -> list[_FakeClockMaster]:
    """Record the clock masters of quel_ic_config created by the connection."""
    created: list[_FakeClockMaster] = []

    def _create(ipaddr: str) -> _FakeClockMaster:
        master = _FakeClockMaster(ipaddr)
        created.append(master)
        return master

    monkeypatch.setattr(connection_manager, "create_quel_clock_master", _create)
    return created


def test_connect_uses_boxpool_by_default(monkeypatch) -> None:
    """Given default mode, connect builds a boxpool and system."""
    controller = _make_controller()
    monkeypatch.setattr(
        controller._connection_manager, "_create_resource_map", lambda _type: {}
    )
    fake_quel1_system = object()

    _override_driver_classes(
        controller, BoxPool=_FakeBoxPool, SequencerClient=lambda _ipaddr: object()
    )

    def _fake_create_from_boxpool(box_names: list[str]):
        assert box_names == ["A", "B"]
        return fake_quel1_system

    monkeypatch.setattr(
        controller._connection_manager,
        "_create_quel1system_from_boxpool",
        _fake_create_from_boxpool,
    )

    controller.connect(["A", "B"])

    qubecalib = cast(_FakeQubeCalib, controller.qubecalib)
    assert qubecalib.sysdb.create_quel1system_calls == []
    assert isinstance(controller._connection_manager.boxpool, _FakeBoxPool)
    assert controller._connection_manager.quel1system is fake_quel1_system


def test_connect_parallel_mode_bypasses_legacy_create_boxpool(monkeypatch) -> None:
    """Given parallel mode, connect builds a boxpool and system."""
    controller = _make_controller()
    monkeypatch.setattr(
        controller._connection_manager, "_create_resource_map", lambda _type: {}
    )
    fake_quel1_system = object()

    _override_driver_classes(
        controller, BoxPool=_FakeBoxPool, SequencerClient=lambda _ipaddr: object()
    )

    def _fake_create_from_boxpool(box_names: list[str]):
        assert box_names == ["A", "B"]
        return fake_quel1_system

    monkeypatch.setattr(
        controller._connection_manager,
        "_create_quel1system_from_boxpool",
        _fake_create_from_boxpool,
    )

    controller.connect(["A", "B"], parallel=True)

    qubecalib = cast(_FakeQubeCalib, controller.qubecalib)
    assert qubecalib.sysdb.create_quel1system_calls == []
    assert isinstance(controller._connection_manager.boxpool, _FakeBoxPool)
    assert controller._connection_manager.quel1system is fake_quel1_system


def test_create_boxpool_reconnects_all_boxes(monkeypatch) -> None:
    """Given valid boxes, pool creation reconnects each box once."""
    controller = _make_controller()
    _override_driver_classes(
        controller,
        package_name="qubecalib",
        BoxPool=_FakeBoxPool,
        SequencerClient=lambda _ipaddr: object(),
    )

    boxpool = cast(
        _FakeBoxPool,
        controller._connection_manager.create_boxpool(["A", "B"]),
    )

    assert isinstance(boxpool, _FakeBoxPool)
    assert boxpool.clockmaster_ip == "192.0.2.1"
    assert set(boxpool._boxes) == {"A", "B"}
    assert boxpool._boxes["A"][0].reconnect_count == 1
    assert boxpool._boxes["B"][0].reconnect_count == 1
    assert boxpool._boxes["A"][0].reconnect_calls == [
        {"background_noise_threshold": DEFAULT_BACKGROUND_NOISE_THRESHOLD_AT_RECONNECT}
    ]
    assert boxpool._boxes["B"][0].reconnect_calls == [
        {"background_noise_threshold": DEFAULT_BACKGROUND_NOISE_THRESHOLD_AT_RECONNECT}
    ]


def test_create_boxpool_sets_quel_clock_master_of_configured_ip(
    clock_masters: list[_FakeClockMaster],
) -> None:
    """Given qxdriver, the pool holds the clock master of the configured IP."""
    controller = _make_controller()
    _override_driver_classes(
        controller,
        BoxPool=_FakeBoxPool,
        SequencerClient=lambda _ipaddr: object(),
    )

    boxpool = cast(
        _FakeBoxPool,
        controller._connection_manager.create_boxpool(["A", "B"]),
    )

    assert [master.ipaddr for master in clock_masters] == ["192.0.2.1"]
    assert boxpool._clock_master is clock_masters[0]
    assert clock_masters[0].terminate_calls == 0


def _fail_reconnect(self: _FakeBox, **kwargs: Any) -> None:
    raise RuntimeError("reconnect failed")


@pytest.mark.parametrize(
    ("box_names", "fail_reconnect", "error", "terminate_calls"),
    [
        (["A", "Z"], False, ValueError, []),
        (["A", "B"], True, RuntimeError, [1]),
    ],
    ids=["unknown-box", "reconnect-failure"],
)
def test_create_boxpool_leaves_no_clock_master_held_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    clock_masters: list[_FakeClockMaster],
    box_names: list[str],
    fail_reconnect: bool,
    error: type[Exception],
    terminate_calls: list[int],
) -> None:
    """Given a failed pool creation, no clock master keeps its lock."""
    controller = _make_controller()
    _override_driver_classes(
        controller,
        BoxPool=_FakeBoxPool,
        SequencerClient=lambda _ipaddr: object(),
    )
    if fail_reconnect:
        monkeypatch.setattr(_FakeBox, "reconnect", _fail_reconnect)

    with pytest.raises(error):
        controller._connection_manager.create_boxpool(box_names)

    assert [master.terminate_calls for master in clock_masters] == terminate_calls


def test_connect_terminates_clock_master_when_system_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
    clock_masters: list[_FakeClockMaster],
) -> None:
    """Given a failure after the pool is created, connect releases the clock master."""
    controller = _make_controller()
    _override_driver_classes(
        controller, BoxPool=_FakeBoxPool, SequencerClient=lambda _ipaddr: object()
    )

    def _raise(box_names: list[str]) -> Any:
        raise RuntimeError("system creation failed")

    monkeypatch.setattr(
        controller._connection_manager, "_create_quel1system_from_boxpool", _raise
    )

    with pytest.raises(RuntimeError, match="system creation failed"):
        controller.connect(["A", "B"])

    assert [master.terminate_calls for master in clock_masters] == [1]
    assert not controller._connection_manager.is_connected


def test_create_quel1system_uses_clock_master_of_boxpool(
    clock_masters: list[_FakeClockMaster],
) -> None:
    """Given qxdriver, the system takes the pooled clock master without a client."""
    controller = _make_controller()
    create_calls: list[dict[str, Any]] = []

    def _create_system(**kwargs: Any) -> object:
        create_calls.append(kwargs)
        return object()

    _override_driver_classes(
        controller,
        BoxPool=_FakeBoxPool,
        SequencerClient=lambda _ipaddr: object(),
        NamedBox=lambda *, name, box: (name, box),
        Quel1System=SimpleNamespace(create=_create_system),
    )
    boxpool = cast(
        _FakeBoxPool, controller._connection_manager.create_boxpool(["A", "B"])
    )
    controller._connection_manager.set_boxpool(cast(Any, boxpool))

    controller._connection_manager._create_quel1system_from_boxpool(["A", "B"])

    assert len(create_calls) == 1
    assert create_calls[0]["clockmaster"] is clock_masters[0]
    assert create_calls[0]["boxes"] == [
        ("A", boxpool._boxes["A"][0]),
        ("B", boxpool._boxes["B"][0]),
    ]


def test_create_boxpool_raises_for_unknown_box(monkeypatch) -> None:
    """Given an unknown box, pool creation raises ValueError."""
    controller = _make_controller()
    _override_driver_classes(controller, BoxPool=_FakeBoxPool)

    with pytest.raises(ValueError, match=r"box\(Z\) is not defined") as exc:
        controller._connection_manager.create_boxpool(["A", "Z"])
    assert exc.value.args
    if exc.value.args:
        assert "box(Z) is not defined" in str(exc)


def test_get_box_returns_existing_box_without_reconnect(monkeypatch) -> None:
    """Given pooled box, get_box returns it without reconnecting."""
    controller = _make_controller()
    _override_driver_classes(
        controller,
        BoxPool=_FakeBoxPool,
        SequencerClient=lambda _ipaddr: object(),
    )
    boxpool = cast(_FakeBoxPool, controller._connection_manager.create_boxpool(["A"]))
    controller._connection_manager.set_boxpool(cast(Any, boxpool))
    monkeypatch.setattr(
        controller._runtime_context, "validate_box_availability", lambda _: None
    )

    reconnect_count_before = boxpool._boxes["A"][0].reconnect_count
    box = controller.get_box("A")

    assert box is boxpool._boxes["A"][0]
    assert boxpool._boxes["A"][0].reconnect_count == reconnect_count_before


def test_get_box_creates_box_without_reconnect(monkeypatch) -> None:
    """Given missing pooled box, get_box creates box with reconnect disabled then reconnects explicitly."""
    controller = _make_controller()
    controller._connection_manager.set_boxpool(cast(Any, _FakeBoxPool()))
    monkeypatch.setattr(
        controller._runtime_context, "validate_box_availability", lambda _: None
    )

    box = controller.get_box("B")

    qubecalib = cast(_FakeQubeCalib, controller.qubecalib)
    assert cast(Any, box).name == "B"
    assert qubecalib.sysdb.create_box_calls[-1] == ("B", False)
    assert qubecalib.sysdb.created_boxes[-1].reconnect_calls == [
        {"background_noise_threshold": DEFAULT_BACKGROUND_NOISE_THRESHOLD_AT_RECONNECT}
    ]


def test_dump_box_creates_box_without_reconnect_when_disconnected(monkeypatch) -> None:
    """Given disconnected runtime, dump_box creates box with reconnect disabled then reconnects explicitly."""
    controller = _make_controller()
    monkeypatch.setattr(
        controller._runtime_context, "validate_box_availability", lambda _: None
    )

    dumped = controller.dump_box("A")

    qubecalib = cast(_FakeQubeCalib, controller.qubecalib)
    assert dumped == {"box": "A"}
    assert qubecalib.sysdb.create_box_calls[-1] == ("A", False)
    assert qubecalib.sysdb.created_boxes[-1].reconnect_calls == [
        {"background_noise_threshold": DEFAULT_BACKGROUND_NOISE_THRESHOLD_AT_RECONNECT}
    ]


def test_connect_skips_reconnect_when_already_connected(monkeypatch) -> None:
    """Given existing connection, connect skips creating a new boxpool."""
    controller = _make_controller()
    existing_system = object()
    existing_boxpool = _FakeBoxPool()
    existing_boxpool._boxes["A"] = (_FakeBox("A"), object())
    controller._connection_manager.set_connected_state(
        boxpool=cast(Any, existing_boxpool),
        quel1system=cast(Any, existing_system),
        cap_resource_map={},
        gen_resource_map={},
    )

    def _raise_if_called(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("connect path should be skipped when already connected")

    monkeypatch.setattr(
        controller._connection_manager, "create_boxpool", _raise_if_called
    )
    monkeypatch.setattr(
        controller._connection_manager,
        "_create_quel1system_from_boxpool",
        _raise_if_called,
    )
    monkeypatch.setattr(
        controller._connection_manager, "_create_resource_map", _raise_if_called
    )

    controller.connect(["A"])

    assert controller._connection_manager.quel1system is existing_system
