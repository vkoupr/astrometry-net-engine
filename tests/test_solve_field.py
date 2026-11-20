# Copyright (c) 2010-2026 Vladimir Kouprianov (v.kouprianov@gmail.com)
# SPDX-License-Identifier: GPL-3.0-only
# Adapted 2026-11-15 from the original plate-solving regression tests.
"""Real-index solving, WCS accuracy, reuse, and lifecycle regression tests."""

from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path

import numpy as np
import pytest

from astrometry_net_engine import MatchObj, Solver, __version__


@pytest.mark.parametrize(
    "sip_order, lost, retry", [(0, False, False), (3, False, False), (3, True, False), (3, True, True)]
)
def test_solve_field(index_path: Path, sip_order: int, lost: bool, retry: bool) -> None:
    """Solve the reference field using index_path, sip_order, lost-position input, and optional retry."""
    data = json.loads((Path(__file__).parent / "data/field.json").read_text())
    sources = np.asarray(data["STAR_DATA"])
    with Solver(index_path) as solver:
        result = solver.solve(
            sources[:, :2],
            sources[:, 2],
            ra_hours=0 if lost else data["RA0"],
            dec_degs=0 if lost else data["DEC0"],
            radius=180 if lost and not retry else 1,
            min_scale=3.5,
            max_scale=4.5,
            sip_order=sip_order,
            retry_lost=retry,
        )
    wcs = result.wcs
    assert result.solved and not result.canceled
    assert wcs is not None and wcs.has_celestial
    assert len(result.attempts) == (2 if lost and retry else 1)
    assert result.attempts[0].use_radec  # Numeric zero coordinates are explicit coordinates too.
    assert result.attempts[-1].use_radec == (not (lost and retry))
    assert result.attempts[-1].numtries > 0
    assert result.attempts[-1].best_match_solves
    if not lost:
        predicted = np.asarray(wcs.all_world2pix(data["TEST_RADEC"], 0))
        error = np.linalg.norm(predicted - data["TEST_XY"], axis=1)
        assert error.max() < (4.0 if sip_order == 0 else 1.8)
    if sip_order >= 2:
        assert wcs.sip is not None


def test_reuse_and_independent_instances(index_path: Path) -> None:
    """Reuse two solvers loaded from index_path, including an option change and independent close."""
    data = json.loads((Path(__file__).parent / "data/field.json").read_text())
    sources = np.asarray(data["STAR_DATA"])
    with Solver(index_path) as first, Solver([index_path]) as second:
        for solver, center in [(first, True), (first, False), (second, True)]:
            solution = solver.solve(
                sources[:, :2],
                sources[:, 2],
                ra_hours=data["RA0"],
                dec_degs=data["DEC0"],
                radius=1,
                min_scale=3.5,
                max_scale=4.5,
                max_sources=40,
                crpix_center=center,
            )
            assert solution.solved
        first.close()
        first.close()
        assert second.solve(sources[:, :2], min_scale=3.5, max_scale=4.5, parity=None, max_sources=0).solved
        with pytest.raises(RuntimeError, match="closed"):
            first.solve(sources[:, :2])


@pytest.mark.parametrize(
    "coordinates, flux", [([], None), ([[1, 2]] * 4, None), ([[1, 2], [2, 3], [3, 4], [4, 5]], [1, 2])]
)
def test_invalid_arrays(index_path: Path, coordinates: list[list[float]], flux: list[float] | None) -> None:
    """Reject invalid coordinates or flux before using indexes from index_path."""
    with Solver(index_path) as solver, pytest.raises(ValueError):
        solver.solve(coordinates, flux)


def test_empty_indexes(tmp_path: Path) -> None:
    """Reject an empty tmp_path without retaining a partially constructed solver."""
    with pytest.raises(ValueError, match="No valid index"):
        Solver(tmp_path)


def test_installed_extension() -> None:
    """Check installed metadata and execute the imported binary through a resource-allocation operation."""
    from astrometry_net_engine import an_engine

    assert version("astrometry-net-engine") == __version__
    package_path = Path(an_engine.__file__).parent
    assert (package_path / "py.typed").is_file()
    assert (package_path / "an_engine.pyi").is_file()
    solver = an_engine.solver_new()
    assert solver is not None
    an_engine.solver_free(solver)
    distance = an_engine.healpix_distance_to_radec(0, 1, 0.0, 0.0)
    assert isinstance(distance, float) and 0 <= distance <= 180


@pytest.mark.parametrize("case, retry", [("random", False), ("scale", False), ("scale", True), ("position", False)])
def test_solve_field_no_match(index_path: Path, case: str, retry: bool) -> None:
    """Search eligible indexes in index_path for nonexistent or incorrectly constrained fields, with retry."""
    data = json.loads((Path(__file__).parent / "data/field.json").read_text())
    sources = np.asarray(data["STAR_DATA"])
    coordinates = np.random.default_rng(20261118).uniform(1, 3000, (50, 2)) if case == "random" else sources[:, :2]
    with Solver(index_path) as solver:
        result = solver.solve(
            coordinates,
            ra_hours=(data["RA0"] + 6) % 24 if case == "position" else data["RA0"],
            dec_degs=data["DEC0"],
            radius=1,
            min_scale=1.5 if case == "scale" else 3.5,
            max_scale=2.0 if case == "scale" else 4.5,
            retry_lost=retry,
            maximum_quads=10000,
        )
    assert not result.solved and result.wcs is None and not result.canceled
    assert len(result.attempts) == (2 if retry else 1)
    for state in result.attempts:
        assert any("4110" in name for name in state.index_names)
        assert state.numtries > 0 and state.last_examined_object >= 4
        assert state.timeused > 0 and not state.best_match_solves
        assert (state.best_match is not None) == state.have_best_match
    if case == "position":
        # The all-sky 4110 index survives preselection; native RA/Dec filtering rejects actual quad matches.
        assert result.attempts[0].use_radec
        assert result.attempts[0].num_radec_skipped > 0
    if case == "scale":
        assert all(state.funits_upper == 2.0 for state in result.attempts)


def test_result_lifetime_and_settings(index_path: Path) -> None:
    """Keep immutable metadata and owned WCS data valid after reuse, concurrent work, and close using index_path."""
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import FrozenInstanceError, asdict

    data = json.loads((Path(__file__).parent / "data/field.json").read_text())
    coordinates = np.asarray(data["STAR_DATA"])[:, :2]
    with Solver(index_path) as solver:
        result = solver.solve(coordinates, min_scale=3.5, max_scale=4.5)
        assert result.wcs is not None
        world = np.asarray(result.wcs.all_pix2world([[1000, 1000]], 1))
        match = result.attempts[0].best_match
        assert isinstance(match, MatchObj)
        saved = asdict(result.attempts[0])
        assert result.settings.source_count == len(coordinates) and not result.settings.has_flux
        assert result.settings.width is None and result.settings.height is None
        assert result.settings.ra_hours is None and result.settings.dec_degs is None
        assert result.settings.parity is None and not result.attempts[0].use_radec
        with pytest.raises(FrozenInstanceError):
            result.settings.radius = 1  # pyright: ignore[reportAttributeAccessIssue]
        with pytest.raises(FrozenInstanceError):
            result.attempts[0].numtries = 0  # pyright: ignore[reportAttributeAccessIssue]
        with pytest.raises(FrozenInstanceError):
            match.nmatch = 0  # pyright: ignore[reportAttributeAccessIssue]
        assert solver.solve(coordinates, min_scale=3.5, max_scale=4.5, sip_order=0).solved
        with Solver(index_path) as other, ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(current.solve, coordinates, min_scale=3.5, max_scale=4.5) for current in (solver, other)
            ]
            assert all(future.result(timeout=120).solved for future in futures)
    del solver
    assert asdict(result.attempts[0]) == saved
    np.testing.assert_array_equal(result.wcs.all_pix2world([[1000, 1000]], 1), world)
    assert result.attempts[0].best_index_name is not None
    assert 4 <= match.nmatch <= len(coordinates)
    assert match.dimquads == 4 and match.indexid == 4110
    # Metadata consists of owned scalar/tuple values and the frozen match snapshot.
    assert all(
        value is None or isinstance(value, (int, float, bool, str, tuple, MatchObj))
        for value in vars(result.attempts[0]).values()
    )
    assert all(isinstance(value, (int, float, bool)) for value in vars(match).values())


def test_native_options_and_retry(index_path: Path) -> None:
    """Inspect effective native options on both attempts and verify defaults on reuse with index_path."""
    from astrometry_net_engine import an_engine

    data = json.loads((Path(__file__).parent / "data/field.json").read_text())
    sources = np.asarray(data["STAR_DATA"])
    with Solver(index_path) as solver:
        result = solver.solve(
            sources[:, :2],
            sources[:, 2],
            ra_hours=2,
            dec_degs=10,
            radius=1,
            min_scale=3.5,
            max_scale=4.5,
            parity=True,
            sip_order=2,
            sip_inverse_order=3,
            deduplicate=False,
            uniformize_index=False,
            positional_noise_pixels=1.25,
            distractor_ratio=0.3,
            code_tolerance_l2_distance=0.015,
            distance_from_quad_bonus=False,
            maximum_quads=20000,
            maximum_matches=5000,
            minimum_quad_size_pixels=200,
            maximum_quad_size_pixels=3000,
            logodds_threshold=30,
            logodds_bail_threshold=-100,
            logodds_stoplooking=1000,
            logodds_to_tune=30,
        )
        assert result.solved and len(result.attempts) == 2
        assert result.settings.radius == 1 and result.settings.parity is True
        first, last = result.attempts
        assert first.best_match is None and not first.have_best_match
        assert isinstance(last.best_match, MatchObj)
        assert first.use_radec and not last.use_radec
        # Clearing the native constraint leaves its now-inactive numeric fields unchanged.
        assert first.r2 == last.r2 and first.centerxyz == last.centerxyz
        assert first.parity == an_engine.PARITY_FLIP and last.parity == an_engine.PARITY_BOTH
        for state in result.attempts:
            assert not state.verify_dedup and not state.verify_uniformize
            assert state.verify_pix == 1.25 and state.distractor_ratio == 0.3 and state.codetol == 0.015
            assert not state.distance_from_quad_bonus
            assert state.do_tweak and state.tweak_aborder == 2 and state.tweak_abporder == 3
            assert state.maxquads == 20000 and state.maxmatches == 5000
            assert state.quadsize_min == 200 and state.quadsize_max == 3000
            assert state.logratio_tokeep == 30 and state.logratio_bail_threshold == -100
            assert state.logratio_stoplooking == 1000 and state.logratio_totune == 30
        # Ignore the inverse-order request in linear-only mode, and clear all previously customized settings.
        linear = solver.solve(
            sources[:, :2], min_scale=3.5, max_scale=4.5, sip_order=0, sip_inverse_order=7, logodds_to_tune=1
        )
        assert linear.solved and linear.wcs is not None and linear.wcs.sip is None
        state = linear.attempts[0]
        assert not state.do_tweak and state.tweak_aborder == state.tweak_abporder == 3
        assert state.logratio_totune == 1e30 and linear.settings.logodds_to_tune == 1
        assert linear.settings.sip_inverse_order == 7
        default = solver.solve(sources[:, :2], min_scale=3.5, max_scale=4.5)
        repeated = solver.solve(sources[:, :2], min_scale=3.5, max_scale=4.5)
        state = default.attempts[0]
        assert default.solved and repeated.solved
        assert state.verify_dedup and state.verify_uniformize and state.distance_from_quad_bonus
        assert state.verify_pix == 1 and state.codetol == 0.01 and state.distractor_ratio == 0.25
        assert state.maxquads == state.maxmatches == state.quadsize_max == 0
        assert state.tweak_aborder == 3 and state.tweak_abporder == 4
        assert state.logratio_tokeep == pytest.approx(np.log(1e12))
        assert state.logratio_bail_threshold == pytest.approx(np.log(1e-100))
        assert state.logratio_stoplooking == 1e30 and state.logratio_totune == 14.0
        assert default.settings.logodds_to_tune == 14.0
        disabled = solver.solve(sources[:, :2], min_scale=3.5, max_scale=4.5, logodds_to_tune=None)
        assert disabled.solved and disabled.settings.logodds_to_tune is None
        assert disabled.attempts[0].logratio_totune == 1e30
        assert state.num_meanx_skipped == repeated.attempts[0].num_meanx_skipped
        assert state.numtries == repeated.attempts[0].numtries


@pytest.mark.parametrize(
    "option, value",
    [
        ("positional_noise_pixels", -1),
        ("distractor_ratio", 0),
        ("distractor_ratio", 1.1),
        ("code_tolerance_l2_distance", -0.1),
        ("maximum_quads", -1),
        ("maximum_matches", 1.5),
        ("sip_inverse_order", 10),
        ("minimum_quad_size_pixels", -1),
        ("minimum_quad_size_fraction", -1),
        ("minimum_quad_size_fraction", float("nan")),
        ("minimum_quad_size_fraction", float("inf")),
        ("max_sources", ""),
        ("parity", ""),
        ("maximum_quad_size_pixels", -1),
        ("logodds_threshold", float("nan")),
        ("uniformize_index", 1),
        ("ra_hours", float("nan")),
        ("dec_degs", float("inf")),
        ("dec_degs", 91),
    ],
)
def test_invalid_solver_options(index_path: Path, option: str, value: float | str) -> None:
    """Reject an invalid option/value before passing it to native code loaded from index_path."""
    # String placeholders must fail validation, rather than an unrelated index-selection check.
    with (
        Solver(index_path) as solver,
        pytest.raises((ValueError, TypeError), match=option if isinstance(value, str) else None),
    ):
        solver.solve([[1, 1], [2, 4], [3, 2], [4, 3]], **{option: value})  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("width, height, minimum_pixels", [(3000, 3100, None), (None, None, None), (3000, 3100, 200)])
def test_positional_options_and_quad_fraction(
    index_path: Path, width: int | None, height: int | None, minimum_pixels: float | None
) -> None:
    """Pass every option positionally with index_path and check quad fractions for width/height and pixel overrides."""
    import inspect

    assert all(
        parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        for parameter in inspect.signature(Solver.solve).parameters.values()
    )
    data = json.loads((Path(__file__).parent / "data/field.json").read_text())
    sources = np.asarray(data["STAR_DATA"])
    with Solver(index_path) as solver:
        result = solver.solve(
            sources[:, :2],
            sources[:, 2],
            width,
            height,
            data["RA0"],
            data["DEC0"],
            1,
            3.5,
            4.5,
            None,
            False,
            3,
            4,
            True,
            40,
            True,
            True,
            1.0,
            0.25,
            0.01,
            True,
            20000,
            5000,
            minimum_pixels,
            0.2,
            0.0,
            float(np.log(1e12)),
            float(np.log(1e-100)),
            None,
            14.0,
        )
    assert result.solved
    assert result.settings.minimum_quad_size_fraction == 0.2
    assert result.settings.minimum_quad_size_pixels == minimum_pixels
    image_width = width if width is not None else np.ptp(sources[:, 0]) + 1
    image_height = height if height is not None else np.ptp(sources[:, 1]) + 1
    expected_minimum = 0.2 * min(image_width, image_height) if minimum_pixels is None else minimum_pixels
    assert result.attempts[0].quadsize_min == pytest.approx(expected_minimum)
    assert result.attempts[0].logratio_totune == 14.0


@pytest.mark.parametrize(
    "reflect_x, parity, solved", [(False, True, True), (False, False, False), (True, False, True), (True, True, False)]
)
def test_native_parity(index_path: Path, reflect_x: bool, parity: bool, solved: bool) -> None:
    """Check native parity for the reference field in index_path, with optional reflect_x and expected solved flag."""
    from astrometry_net_engine import an_engine

    data = json.loads((Path(__file__).parent / "data/field.json").read_text())
    coordinates = np.asarray(data["STAR_DATA"])[:, :2].copy()
    if reflect_x:
        coordinates[:, 0] = coordinates[:, 0].min() + coordinates[:, 0].max() - coordinates[:, 0]
    with Solver(index_path) as solver:
        result = solver.solve(
            coordinates,
            min_scale=3.5,
            max_scale=4.5,
            parity=parity,
            retry_lost=False,
            sip_order=0,
            maximum_quads=10000,
        )
    assert result.solved is solved and len(result.attempts) == 1
    assert result.settings.parity is parity
    state = result.attempts[0]
    assert state.parity == (an_engine.PARITY_FLIP if parity else an_engine.PARITY_NORMAL)
    assert not state.use_radec and state.numtries > 0
    if solved:
        assert state.best_match is not None and state.best_match.parity is parity
        assert result.wcs is not None
        assert (np.linalg.det(result.wcs.wcs.cd) > 0) == parity


@pytest.mark.parametrize("ra_hours, dec_degs", [(None, None), (None, 80.0), (2.0, None)])
def test_missing_coordinate_clears_position(
    index_path: Path, monkeypatch: pytest.MonkeyPatch, ra_hours: float | None, dec_degs: float | None
) -> None:
    """Disable position for missing ra_hours/dec_degs, checking native clear calls and solver reuse with index_path."""
    from astrometry_net_engine import an_engine

    original_clear = an_engine.solver_clear_radec
    clears: list[bool] = []

    def clear(native: an_engine.solver_t) -> None:
        """Call the real clear function on native and record its resulting constraint flag."""
        original_clear(native)
        clears.append(bool(native.use_radec))

    monkeypatch.setattr(an_engine, "solver_clear_radec", clear)
    data = json.loads((Path(__file__).parent / "data/field.json").read_text())
    coordinates = np.asarray(data["STAR_DATA"])[:, :2]
    with Solver(index_path) as solver:
        localized = solver.solve(
            coordinates,
            ra_hours=data["RA0"],
            dec_degs=data["DEC0"],
            radius=1,
            min_scale=3.5,
            max_scale=4.5,
            retry_lost=False,
        )
        assert localized.solved and localized.attempts[0].use_radec
        assert clears == []
        result = solver.solve(
            coordinates,
            ra_hours=ra_hours,
            dec_degs=dec_degs,
            radius=0,
            min_scale=3.5,
            max_scale=4.5,
        )
        assert result.solved and len(result.attempts) == 1
        assert result.settings.ra_hours == ra_hours and result.settings.dec_degs == dec_degs
        assert result.settings.radius == 0
        assert clears == [False] and not result.attempts[0].use_radec
        # A later solve can re-enable the constraint without changing previous snapshots.
        restored = solver.solve(
            coordinates,
            ra_hours=data["RA0"],
            dec_degs=data["DEC0"],
            radius=1,
            min_scale=3.5,
            max_scale=4.5,
            retry_lost=False,
        )
        assert restored.solved and restored.attempts[0].use_radec
        assert not result.attempts[0].use_radec


@pytest.mark.parametrize(
    "ra_hours, dec_degs, radius", [(None, None, 1.0), (None, None, 180.0), (None, 80.0, 0.0), (2.0, None, 0.0)]
)
def test_unrestricted_no_match_is_not_retried(
    index_path: Path, ra_hours: float | None, dec_degs: float | None, radius: float
) -> None:
    """Search indexes in index_path once when ra_hours/dec_degs disable position, regardless of radius."""
    coordinates = np.random.default_rng(20261118).uniform(1, 3000, (50, 2))
    with Solver(index_path) as solver:
        result = solver.solve(
            coordinates,
            ra_hours=ra_hours,
            dec_degs=dec_degs,
            radius=radius,
            min_scale=3.5,
            max_scale=4.5,
            maximum_quads=1000,
        )
    assert not result.solved and not result.canceled and len(result.attempts) == 1
    assert result.settings.retry_lost and result.settings.parity is None
    assert result.settings.ra_hours == ra_hours and result.settings.dec_degs == dec_degs
    assert result.settings.radius == radius
    state = result.attempts[0]
    assert state.numtries > 0 and not state.use_radec and state.num_radec_skipped == 0


def test_parity_retry_without_position(index_path: Path) -> None:
    """Relax only parity on a retry with indexes from index_path and an intentionally unspecified position."""
    from astrometry_net_engine import an_engine

    data = json.loads((Path(__file__).parent / "data/field.json").read_text())
    with Solver(index_path) as solver:
        result = solver.solve(
            np.asarray(data["STAR_DATA"])[:, :2],
            radius=0,
            min_scale=3.5,
            max_scale=4.5,
            parity=False,
            maximum_quads=10000,
        )
    assert result.solved and len(result.attempts) == 2
    assert result.settings.ra_hours is None and result.settings.dec_degs is None
    assert result.settings.parity is False and result.settings.radius == 0
    first, last = result.attempts
    assert not first.use_radec and not last.use_radec
    assert first.parity == an_engine.PARITY_NORMAL and last.parity == an_engine.PARITY_BOTH
    assert last.best_match is not None and last.best_match.parity is True


@pytest.mark.parametrize("max_sources", [None, 0, 40])
def test_bright_sources_beyond_native_limit(index_path: Path, max_sources: int | None) -> None:
    """Keep bright reference stars after 1000 faint inputs searchable with index_path and any max_sources mode."""
    data = json.loads((Path(__file__).parent / "data/field.json").read_text())
    stars = np.asarray(data["STAR_DATA"])
    faint_xy = np.random.default_rng(20261119).uniform(1, 3000, (1000, 2))
    coordinates = np.vstack((faint_xy, stars[:, :2]))
    fluxes = np.concatenate((np.full(1000, stars[:, 2].min() - 1), stars[:, 2]))
    original_xy, original_flux = coordinates.copy(), fluxes.copy()
    with Solver(index_path) as solver:
        result = solver.solve(
            coordinates,
            fluxes,
            ra_hours=data["RA0"],
            dec_degs=data["DEC0"],
            radius=1,
            min_scale=3.5,
            max_scale=4.5,
            max_sources=max_sources,
            retry_lost=False,
            sip_order=0,
            maximum_quads=20000,
        )
    assert result.solved and result.wcs is not None
    assert result.settings.source_count == len(coordinates) and result.settings.has_flux
    assert result.settings.max_sources == (max_sources or None)
    assert result.attempts[0].last_examined_object < len(stars)
    match = result.attempts[0].best_match
    assert match is not None and match.nfield == (max_sources or len(coordinates))
    predicted = np.asarray(result.wcs.all_world2pix(data["TEST_RADEC"], 0))
    assert np.linalg.norm(predicted - data["TEST_XY"], axis=1).max() < 4.0
    np.testing.assert_array_equal(coordinates, original_xy)
    np.testing.assert_array_equal(fluxes, original_flux)


def test_faint_sources_removed_before_search(index_path: Path) -> None:
    """Use index_path to solve after sorting and removing seeded random faint leading distractors."""
    data = json.loads((Path(__file__).parent / "data/field.json").read_text())
    sources = np.asarray(data["STAR_DATA"])
    min_flux = sources[:, 2].min()
    rng = np.random.default_rng(20261119)
    distractors = np.column_stack(
        (rng.uniform(1, 3000, (len(sources), 2)), rng.uniform(min_flux - 2000, min_flux - 1000, len(sources)))
    )
    assert len(distractors) == len(sources)
    assert distractors[:, 2].max() < sources[:, 2].min()
    # Every retained source must come from the real field, despite the faint sources appearing first.
    combined = np.vstack((distractors, sources))
    coordinates, fluxes = combined[:, :2].copy(), combined[:, 2].copy()
    with Solver(index_path) as solver:
        result = solver.solve(
            coordinates,
            fluxes,
            ra_hours=data["RA0"],
            dec_degs=data["DEC0"],
            radius=1,
            min_scale=3.5,
            max_scale=4.5,
            max_sources=len(sources),
            retry_lost=False,
        )
    assert result.solved and not result.canceled
    assert result.wcs is not None and result.wcs.has_celestial and result.wcs.sip is not None
    assert result.settings.source_count == len(combined) and result.settings.has_flux
    assert result.settings.max_sources == len(sources)
    assert len(result.attempts) == 1
    state = result.attempts[0]
    assert state.numtries > 0 and state.endobj == len(sources)
    assert state.last_examined_object < len(sources)
    assert state.best_match is not None and state.best_match.nfield == len(sources)
    predicted = np.asarray(result.wcs.all_world2pix(data["TEST_RADEC"], 0))
    assert np.linalg.norm(predicted - data["TEST_XY"], axis=1).max() < 1.8
    np.testing.assert_array_equal(coordinates, combined[:, :2])
    np.testing.assert_array_equal(fluxes, combined[:, 2])
