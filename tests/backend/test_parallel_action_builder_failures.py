"""Tests for failures of QubexMultiAction across boxes."""

from __future__ import annotations

import logging
from concurrent.futures import CancelledError, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast

import pytest

from qubex.backend.quel1.compat.parallel_action_builder import (
    ClockHealthCheckOptions,
    QubexMultiAction,
    _load_shot_bundle,
)


class _Window:
    """A shot that has ended with `error`, or runs until it is cancelled."""

    def __init__(self, error: Exception | None = None, *, running: bool) -> None:
        self._error: BaseException | None = error
        self._running = running

    def done(self) -> bool:
        """Return whether the shot has ended."""
        return not self._running

    def exception(self, timeout: float | None = None) -> BaseException | None:
        """Return the failure of the shot, if any."""
        _ = timeout
        if self._running:
            raise FutureTimeoutError
        return self._error

    def result(self, timeout: float | None = None) -> dict[Any, Any]:
        """Return the result of the shot, or raise its failure."""
        _ = timeout
        if self._running:
            raise FutureTimeoutError
        if self._error is not None:
            raise self._error
        return {}

    def cancel(self, timeout: float | None = None) -> bool:
        """Cancel the shot if it is running."""
        _ = timeout
        if not self._running:
            return False
        self._running = False
        self._error = CancelledError()
        return True


@dataclass
class _ShotBox:
    """Starts running shots; fails the start named, or ends the shot named with a failure."""

    fail_start: str | None = None
    fail_shot: str | None = None
    windows: list[_Window] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.failure = RuntimeError(f"{self.fail_start or self.fail_shot} failed")

    def start(self, kind: str) -> _Window:
        """Start one shot of `kind`."""
        if kind == self.fail_start:
            raise self.failure
        window = (
            _Window(self.failure, running=False)
            if kind == self.fail_shot
            else _Window(running=True)
        )
        self.windows.append(window)
        return window

    def get_current_timecounter(self) -> int:
        """Return a fixed time counter."""
        return 100

    def get_latest_sysref_timecounter(self) -> int:
        """Return a fixed SYSREF time counter."""
        return 0

    def start_wavegen(
        self,
        channels: set[tuple[str, int]],
        timecounter: int | None = None,
    ) -> _Window:
        """Start a timed emission."""
        _ = channels, timecounter
        return self.start("wavegen")


@dataclass
class _ShotAction:
    box: _ShotBox
    triggered: bool = False
    _cprms: dict[str, object] = field(default_factory=lambda: {"P0": object()})
    _wseqs: list[SimpleNamespace] = field(
        default_factory=lambda: [SimpleNamespace(port="P0", channel=0)]
    )

    @property
    def _triggers(self) -> dict[str, object]:
        return {"P0": object()} if self.triggered else {}

    def capture_start(
        self, *, timecounter: int | None = None
    ) -> dict[str, _Window | tuple[_Window, _Window]]:
        """Start a capture, by the trigger of an AWG if triggered."""
        _ = timecounter
        if self.triggered:
            return {
                "__triggered__": (
                    self.box.start("trigger_capture"),
                    self.box.start("trigger_awg"),
                )
            }
        return {"P0": self.box.start("capture")}

    def capture_stop(
        self,
        futures: dict[str, Any],
    ) -> tuple[dict[str, str], dict[tuple[str, int], str]]:
        """Read the captures, which have ended."""
        for value in futures.values():
            for window in value if isinstance(value, tuple) else (value,):
                window.result()
        return {"P0": "ok"}, {}


def _multi_action(actions: dict[str, _ShotAction]) -> QubexMultiAction:
    return QubexMultiAction(
        _system=cast(
            Any,
            SimpleNamespace(
                box={name: action.box for name, action in actions.items()},
                timing_shift=dict.fromkeys(actions, 0),
                displacement=0,
            ),
        ),
        _actions=cast(Any, MappingProxyType(actions)),
        _estimated_timediff=MappingProxyType(dict.fromkeys(actions, 0)),
        _reference_box_name=next(iter(actions)),
        _ref_sysref_time_offset=0,
        _clock_options=ClockHealthCheckOptions(),
        _logger=logging.getLogger(__name__),
        _arm_triggered_boxes_at_capture_start=True,
        _shot_bundle=_load_shot_bundle("qxdriver_quel1"),
    )


@pytest.mark.parametrize(
    ("failing", "fail_start", "fail_shot"),
    [
        (0, "capture", None),
        (1, "trigger_capture", None),
        (2, "wavegen", None),
        (2, None, "capture"),
        (1, None, "trigger_capture"),
        (2, None, "wavegen"),
    ],
    ids=[
        "capture_start_first_box",
        "triggered_start",
        "emission_start",
        "capture_wait",
        "triggered_wait",
        "emission_wait",
    ],
)
def test_action_failure_leaves_no_shot_running(
    failing: int, fail_start: str | None, fail_shot: str | None
) -> None:
    """Given a failure on one box, the shots still running on the others are cancelled and the failure is raised."""
    boxes = [
        _ShotBox(fail_start, fail_shot) if i == failing else _ShotBox()
        for i in range(3)
    ]
    multi_action = _multi_action(
        {
            "B0": _ShotAction(boxes[0]),
            "B1": _ShotAction(boxes[1], triggered=True),
            "B2": _ShotAction(boxes[2]),
        }
    )

    with pytest.raises(RuntimeError) as exc_info:
        multi_action.action()

    assert exc_info.value is boxes[failing].failure
    windows = [window for box in boxes for window in box.windows]
    assert windows
    assert all(window.done() for window in windows)
