# Copyright (c) 2026 Vladimir Kouprianov (v.kouprianov@gmail.com)
# SPDX-License-Identifier: GPL-3.0-only
"""Real native timer invocation, cancellation, exception, and concurrent access checks."""

from __future__ import annotations

import gc
import importlib.util
import json
import subprocess
import sys
import sysconfig
import time
import weakref
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import distribution
from pathlib import Path
from threading import Lock

import numpy as np
import pytest

from astrometry_net_engine import Solver, an_engine


class _CallbackSolver(Solver):
    """Declare ordinary application attributes used by the typed callback tests."""

    last_check: float | None = None
    cancel_requested: bool = False
    callback_error: BaseException | None = None


def test_timer_cancel_and_reuse(index_path: Path) -> None:
    """Cancel a deliberately unmatched field with indexes in index_path, then reuse the solver."""
    calls = []

    def cancel(current: Solver) -> int:
        """Record and update the actual current solver, then obey its cancellation flag."""
        assert current is solver
        assert isinstance(current, _CallbackSolver)
        current.last_check = time.monotonic()
        calls.append(current)
        return 0 if current.cancel_requested else 1

    # A large random field exercises the actual native timer, rather than a simulated Python callback.
    coordinates = np.random.default_rng(12).uniform(0, 4000, (1000, 2))
    solver = _CallbackSolver(index_path, timer_callback=cancel)
    with solver:
        solver.cancel_requested = True
        result = solver.solve(coordinates, min_scale=3.5, max_scale=4.5, ra_hours=0, dec_degs=0, radius=1)
        assert not result.solved and result.wcs is None and result.canceled
        assert len(result.attempts) == 1
        assert result.attempts[0].numtries > 0
        assert result.attempts[0].timeused > 0
        assert calls and all(current is solver for current in calls)
        assert solver.last_check is not None
        assert solver.cancel_requested is True
        assert "last_check" in vars(solver)
        solver.timer_callback = None
        data = json.loads((Path(__file__).parent / "data/field.json").read_text())
        assert solver.solve(np.asarray(data["STAR_DATA"])[:, :2], min_scale=3.5, max_scale=4.5).solved


@pytest.mark.parametrize("error_type", [ValueError, KeyboardInterrupt])
def test_callback_exception(index_path: Path, error_type: type[BaseException]) -> None:
    """Propagate error_type raised in a native callback using indexes from index_path."""
    expected = error_type("callback sentinel")

    def fail(current: Solver) -> int:
        """Store exception information on current before raising the exact exception instance."""
        assert current is solver
        assert isinstance(current, _CallbackSolver)
        current.callback_error = expected
        current.last_check = time.monotonic()
        raise expected

    solver = _CallbackSolver(index_path, timer_callback=fail)
    with solver:
        with pytest.raises(error_type) as caught:
            solver.solve(np.random.default_rng(12).uniform(0, 4000, (1000, 2)), min_scale=3.5, max_scale=4.5)
        assert caught.value is expected
        assert solver.callback_error is expected
        assert solver.last_check is not None
        frames = []
        traceback = caught.value.__traceback__
        while traceback is not None:
            frames.append(traceback.tb_frame.f_code.co_name)
            traceback = traceback.tb_next
        assert "fail" in frames
        solver.timer_callback = None
        data = json.loads((Path(__file__).parent / "data/field.json").read_text())
        assert solver.solve(np.asarray(data["STAR_DATA"])[:, :2], min_scale=3.5, max_scale=4.5).solved


def test_callback_delay_and_reentry(index_path: Path) -> None:
    """Continue once, reject callback reentry, then cancel using indexes from index_path."""
    calls = []
    with Solver(index_path) as solver:

        def callback(current: Solver) -> int:
            """Exercise rescheduling and reject operations on current from inside its callback."""
            assert current is solver
            calls.append(time.monotonic())
            with pytest.raises(RuntimeError, match="callback"):
                current.close()
            with pytest.raises(RuntimeError, match="callback"):
                current.solve(np.array([[1, 1], [2, 4], [3, 2], [4, 3]]))
            return 1 if len(calls) == 1 else 0

        solver.timer_callback = callback
        assert solver.solve(
            np.random.default_rng(12).uniform(0, 4000, (1000, 2)), min_scale=3.5, max_scale=4.5
        ).canceled
    assert len(calls) == 2
    assert calls[1] - calls[0] >= 0.9


def test_invalid_callback_result(index_path: Path) -> None:
    """Reject a negative timer delay returned during native solving with index_path."""
    with Solver(index_path, timer_callback=lambda solver: -1) as solver, pytest.raises(ValueError, match="callback"):
        solver.solve(np.random.default_rng(12).uniform(0, 4000, (1000, 2)), min_scale=3.5, max_scale=4.5)


def test_callback_reference_lifetime(index_path: Path) -> None:
    """Keep a callback alive through a native search with index_path and release it when unconfigured."""

    class Cancel:
        """A weak-referenceable application callable with no shared callback state."""

        solver_id: int | None = None

        def __call__(self, current: Solver) -> int:
            """Remember the actual current solver's identity and cancel its search."""
            self.solver_id = id(current)
            return 0

    callback = Cancel()
    reference = weakref.ref(callback)
    with Solver(index_path, timer_callback=callback) as solver:
        assert solver.solve(
            np.random.default_rng(12).uniform(0, 4000, (1000, 2)), min_scale=3.5, max_scale=4.5
        ).canceled
        assert callback.solver_id == id(solver)
        solver.timer_callback = None
        del callback
        gc.collect()
        assert reference() is None


def test_native_serialization(index_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify actual solver calls serialize across reused and independent instances from index_path."""
    original = an_engine.solver_run_with_callback
    counter_lock = Lock()
    counts = [0, 0]

    def tracked(native: an_engine.solver_t, callback: Callable[[], int] | None) -> int:
        """Track overlapping native calls and execute original solver with native and callback."""
        with counter_lock:
            counts[0] += 1
            counts[1] = max(counts)
        try:
            time.sleep(0.02)
            return original(native, callback)
        finally:
            with counter_lock:
                counts[0] -= 1

    data = json.loads((Path(__file__).parent / "data/field.json").read_text())
    coordinates = np.asarray(data["STAR_DATA"])[:, :2]
    monkeypatch.setattr(an_engine, "solver_run_with_callback", tracked)
    with Solver(index_path) as first, Solver(index_path) as second:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(solver.solve, coordinates, min_scale=3.5, max_scale=4.5)
                for solver in [first, first, second, second]
            ]
            assert all(future.result(timeout=120).solved for future in futures)
    assert counts == [0, 1]


def test_free_threaded_import() -> None:
    """Check GIL state, the native extension suffix, and the binary's free-threading declaration."""
    if sysconfig.get_config_var("Py_GIL_DISABLED"):
        assert not getattr(sys, "_is_gil_enabled", lambda: True)()
        native_spec = importlib.util.find_spec("astrometry_net_engine._an_engine")
        assert native_spec is not None and native_spec.origin is not None
        assert native_spec.origin.endswith(str(sysconfig.get_config_var("EXT_SUFFIX")))

        # Bypass Python package imports to distinguish this binary from dependency GIL declarations.
        subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                "import importlib.util, sys; "
                "spec = importlib.util.spec_from_file_location('_an_engine', sys.argv[1]); "
                "module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
                "assert not sys._is_gil_enabled()",
                native_spec.origin,
            ],
            check=True,
        )


def test_free_threaded_wheel_metadata() -> None:
    """Validate installed free-threaded wheel tags, skipping source-tree imports."""
    source_package = Path(__file__).resolve().parents[1] / "astrometry_net_engine"
    if Path(an_engine.__file__).resolve().parent == source_package.resolve():
        pytest.skip("Wheel metadata is not checked for source-tree imports")
    if not sysconfig.get_config_var("Py_GIL_DISABLED"):
        pytest.skip("Requires free-threaded CPython")
    python_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    wheel_metadata = distribution("astrometry-net-engine").read_text("WHEEL")
    assert wheel_metadata is not None
    assert f"Tag: {python_tag}-{python_tag}t-" in wheel_metadata
