# Copyright (c) 2010-2026 Vladimir Kouprianov (v.kouprianov@gmail.com)
# SPDX-License-Identifier: GPL-3.0-only
# Adapted 2026-11-15 for the standalone astrometry-net-engine package.
"""Solve astrometric fields from pre-extracted XY source coordinates."""

from __future__ import annotations

import ctypes
import logging
import math
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import RLock, local
from types import TracebackType
from typing import SupportsInt

import numpy as np
from astropy.wcs import WCS, Sip
from numpy.typing import ArrayLike, NDArray

from . import an_engine
from .__version__ import __version__

__all__ = ["MatchObj", "SolveResult", "SolveSettings", "Solver", "SolverState", "__version__"]

# Astrometry.net has process-global logging, error, and FITS state. Guard all native resource operations too.
_engine_lock = RLock()
_active = local()
_logger = logging.getLogger(__name__)


def _format_angle(value: float, signed: bool) -> str:
    """Format an angle value as colon-separated sexagesimal, optionally with a sign."""
    sign = ("-" if value < 0 else "+") if signed else ""
    seconds = abs(value) * 3600
    units, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{sign}{int(units):02d}:{int(minutes):02d}:{seconds:05.2f}"


def _array_from_swig(data: SupportsInt, shape: tuple[int, ...]) -> NDArray[np.float64]:
    """Copy native double data into an owned NumPy array of the requested shape."""
    array = np.empty(shape, dtype=np.float64)
    ctypes.memmove(array.ctypes.data, int(data), array.nbytes)
    return array


@dataclass(frozen=True)
class SolveSettings:
    """Normalized requested options, source count, and flux presence; no input arrays are retained.

    None records inferred dimensions, unspecified coordinates, the default inverse SIP order, or disabled log-odds
    thresholds.
    Units and defaults are those of Solver.solve(). These values are unchanged by a lost-in-space retry.
    """

    source_count: int
    has_flux: bool
    width: int | None
    height: int | None
    ra_hours: float | None
    dec_degs: float | None
    radius: float
    min_scale: float
    max_scale: float
    parity: bool | None
    sip_order: int
    crpix_center: bool
    max_sources: int | None
    retry_lost: bool
    deduplicate: bool
    uniformize_index: bool
    positional_noise_pixels: float
    distractor_ratio: float
    code_tolerance_l2_distance: float
    distance_from_quad_bonus: bool
    sip_inverse_order: int | None
    maximum_quads: int
    maximum_matches: int
    minimum_quad_size_pixels: float | None
    minimum_quad_size_fraction: float
    maximum_quad_size_pixels: float
    logodds_threshold: float
    logodds_bail_threshold: float
    logodds_stoplooking: float | None
    logodds_to_tune: float | None


@dataclass(frozen=True)
class MatchObj:
    """Owned scalar match metadata using the field names and units from matchobj.h.

    code_err is squared quad-code distance; scale and index_jitter are arcseconds per pixel and arcseconds,
    respectively. radius_deg is in degrees, and logodds/worstlogodds are natural logarithms of odds.
    Counters describe the search when the match was found. No native structures or buffers are retained.
    """

    code_err: float
    radius_deg: float
    scale: float
    logodds: float
    worstlogodds: float
    index_jitter: float
    quadno: int
    dimquads: int
    quad_npeers: int
    nmatch: int
    ndistractor: int
    nconflict: int
    nfield: int
    nindex: int
    nbest: int
    nagree: int
    indexid: int
    healpix: int
    hpnside: int
    quads_tried: int
    quads_matched: int
    quads_scaleok: int
    objs_tried: int
    nverified: int
    parity: bool
    wcs_valid: bool


@dataclass(frozen=True)
class SolverState:
    """Owned native values for one completed attempt, captured before field cleanup.

    Field names follow solver.h; best_match is an independent MatchObj snapshot, or None without a match.
    When use_radec is true, centerxyz is a unit vector and r2 a squared chord distance, not angular coordinates.
    Otherwise those native fields are inactive and may retain reset or earlier-attempt values.
    Counters and CPU timeused (seconds) start afresh on every attempt. index_names lists searched indexes.
    canceled distinguishes a timer cancellation from an ordinary unsuccessful search.
    """

    pixel_xscale: float
    funits_lower: float
    funits_upper: float
    logratio_toprint: float
    logratio_tokeep: float
    logratio_totune: float
    verify_pix: float
    distractor_ratio: float
    codetol: float
    quadsize_min: float
    quadsize_max: float
    r2: float
    logratio_bail_threshold: float
    logratio_stoplooking: float
    minminAB2: float
    maxmaxAB2: float
    field_minx: float
    field_maxx: float
    field_miny: float
    field_maxy: float
    field_diag: float
    timeused: float
    best_logodds: float
    tweak_aborder: int
    tweak_abporder: int
    startobj: int
    endobj: int
    parity: int
    maxquads: int
    maxmatches: int
    numtries: int
    nummatches: int
    numscaleok: int
    last_examined_object: int
    num_cxdx_skipped: int
    num_meanx_skipped: int
    num_radec_skipped: int
    num_abscale_skipped: int
    num_verified: int
    distance_from_quad_bonus: bool
    verify_uniformize: bool
    verify_dedup: bool
    do_tweak: bool
    use_radec: bool
    set_crpix: bool
    set_crpix_center: bool
    quit_now: bool
    best_match_solves: bool
    have_best_match: bool
    centerxyz: tuple[float, float, float]
    crpix: tuple[float, float]
    index_names: tuple[str, ...]
    canceled: bool
    best_index_name: str | None
    best_match: MatchObj | None


@dataclass(frozen=True)
class SolveResult:
    """An independent WCS, original settings, and chronological native attempt snapshots.

    Metadata is immutable; the owned Astropy WCS remains editable by its recipient.
    """

    wcs: WCS | None
    settings: SolveSettings
    attempts: tuple[SolverState, ...]

    @property
    def solved(self) -> bool:
        """Whether an attempt produced an accepted WCS."""
        return self.wcs is not None

    @property
    def canceled(self) -> bool:
        """Whether the final attempt was canceled by the timer callback."""
        return bool(self.attempts and self.attempts[-1].canceled)


class Solver:
    """Reusable Astrometry.net solver owning its loaded indexes and native resources.

    Args:
        index_path: Index directory, OS-path-separated directories, or a sequence of directories.
        timer_callback: Optional callable receiving this Solver and returning seconds until its next invocation;
            zero cancels. The callback may read and write ordinary Python attributes on the instance.
            Exceptions propagate to the caller of solve after native cleanup. Callbacks run periodically during
            search, so a quick solution may finish before the first callback.
    """

    _solver: an_engine.solver_t | None
    _indexes: list[an_engine.index_t]
    index_dirs: list[str]
    timer_callback: Callable[[Solver], int] | None

    def __init__(
        self,
        index_path: str | os.PathLike[str] | Sequence[str | os.PathLike[str]],
        timer_callback: Callable[[Solver], int] | None = None,
    ) -> None:
        """Load indexes from index_path and retain the optional timer_callback for each solve."""
        self._solver = None
        self._indexes = []
        if timer_callback is not None and not callable(timer_callback):
            raise TypeError("timer_callback must be callable or None")
        self.timer_callback = timer_callback
        paths = os.fspath(index_path).split(os.pathsep) if isinstance(index_path, (str, os.PathLike)) else index_path
        self.index_dirs = [str(Path(path).expanduser().resolve()) for path in paths]
        if getattr(_active, "running", False):
            raise RuntimeError("Solver operations cannot be called from a timer callback")
        with _engine_lock:
            try:
                self._solver = an_engine.solver_new()
                if self._solver is None:
                    raise MemoryError("Unable to allocate solver")
                for directory in self.index_dirs:
                    _logger.info("Loading Astrometry.net indexes from %s", directory)
                    if not Path(directory).is_dir():
                        continue
                    for filename in sorted(Path(directory).iterdir()):
                        if not filename.is_file():
                            continue
                        index = an_engine.index_load(str(filename), 0, None)
                        if index is not None:
                            self._indexes.append(index)
                if not self._indexes:
                    raise ValueError(f"No valid index files found in {', '.join(self.index_dirs)}")
                _logger.info("Loaded %d index files", len(self._indexes))
            except BaseException:
                self.close()
                raise

    def close(self) -> None:
        """Release the solver and its indexes; repeated calls are harmless."""
        if getattr(_active, "running", False):
            raise RuntimeError("Solver operations cannot be called from a timer callback")
        with _engine_lock:
            if self._solver is not None:
                an_engine.solver_free(self._solver)
                self._solver = None
            for index in self._indexes:
                an_engine.index_free(index)
            self._indexes.clear()

    def __enter__(self) -> Solver:
        """Return this solver for use in a context manager."""
        with _engine_lock:
            if self._solver is None:
                raise RuntimeError("Solver is closed")
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        """Close resources when leaving the context, regardless of exc_type, exc_value, or traceback."""
        self.close()

    def __del__(self) -> None:
        """Release remaining native resources if explicit close was omitted."""
        if getattr(self, "_solver", None) is not None:
            self.close()

    def solve(
        self,
        xy: ArrayLike,
        flux: ArrayLike | None = None,
        width: int | None = None,
        height: int | None = None,
        ra_hours: float | None = None,
        dec_degs: float | None = None,
        radius: float = 180.0,
        min_scale: float = 0.1,
        max_scale: float = 10.0,
        parity: bool | None = None,
        retry_lost: bool = True,
        sip_order: int = 3,
        sip_inverse_order: int | None = None,
        crpix_center: bool = True,
        max_sources: int | None = None,
        deduplicate: bool = True,
        uniformize_index: bool = True,
        positional_noise_pixels: float = 1.0,
        distractor_ratio: float = 0.25,
        code_tolerance_l2_distance: float = 0.01,
        distance_from_quad_bonus: bool = True,
        maximum_quads: int = 0,
        maximum_matches: int = 0,
        minimum_quad_size_pixels: float | None = None,
        minimum_quad_size_fraction: float = 0.1,
        maximum_quad_size_pixels: float = 0.0,
        logodds_threshold: float = math.log(1e12),
        logodds_bail_threshold: float = math.log(1e-100),
        logodds_stoplooking: float | None = None,
        logodds_to_tune: float | None = 14.0,
    ) -> SolveResult:
        """Return a WCS and independent attempt diagnostics, including unsuccessful or canceled searches.

        Args:
            xy: Finite (N, 2) source coordinates, in pixel units; order sources brightest first without flux.
            flux: Optional finite (N,) source fluxes. Always sort sources by decreasing flux when supplied.
            width: Image width in pixels; None derives bounds from the input coordinates.
            height: Image height in pixels; None derives bounds from the input coordinates.
            ra_hours: Approximate field center right ascension in hours; None disables position constraints.
            dec_degs: Approximate field center declination in degrees; None disables position constraints.
            radius: Search radius in degrees, from 0 through 180; ignored unless both coordinates are supplied.
                An explicit radius of 180 searches the whole sky.
            min_scale: Lower pixel scale bound in arcseconds per pixel.
            max_scale: Upper pixel scale bound in arcseconds per pixel.
            parity: False for normal parity, True for flipped, None to search both (Astrometry.net convention).
            retry_lost: If a constrained search fails, retry without position or parity constraints.
                A search already unrestricted in position and parity is not repeated.
            sip_order: SIP polynomial order; 0 or 1 disables distortion fitting (maximum 8).
            sip_inverse_order: Inverse SIP order 0–9; None uses sip_order + 1. Ignored when SIP is disabled.
            crpix_center: Place the WCS reference pixel at image center.
            max_sources: Maximum brightest sources to examine; None or 0 adds no limit beyond the native 1000.
            deduplicate: Deduplicate nearby field detections during verification (verify_dedup; default True).
            uniformize_index: Spatially uniformize field detections at index scale (verify_uniformize; default True).
            positional_noise_pixels: Nonnegative field positional uncertainty in pixels (verify_pix; default 1).
            distractor_ratio: Distractor fraction in (0, 1] for verification (default 0.25).
            code_tolerance_l2_distance: Nonnegative 4D quad-code L2 tolerance (codetol; default 0.01).
            distance_from_quad_bonus: Increase positional variance away from the matched quad (default True).
            maximum_quads: Quad search limit, checked after each source; zero means unlimited (maxquads).
            maximum_matches: Quad-match limit, checked after each source; zero means unlimited (maxmatches).
            minimum_quad_size_pixels: Minimum field quad size in pixels; None uses minimum_quad_size_fraction.
            minimum_quad_size_fraction: Nonnegative fraction of the smaller image dimension (default 0.1),
                used when minimum_quad_size_pixels is None.
            maximum_quad_size_pixels: Maximum field quad size in pixels; zero means no extra limit.
            logodds_threshold: Natural-log odds required to accept a solution (logratio_tokeep; log(1e12)).
            logodds_bail_threshold: Reject verification below this natural-log odds (default log(1e-100)).
            logodds_stoplooking: Stop verification early above this natural-log odds; None disables early stopping.
            logodds_to_tune: Natural-log odds for intermediate refinement (default 14.0); None or disabled SIP
                prevents tuning.

        Raises:
            ValueError: Invalid inputs, no loaded indexes, or no indexes covering the requested scale and position.
            RuntimeError: This solver is closed or an operation is attempted from a callback.
        """
        # Treat zero image dimensions as inferred bounds.
        if width == 0:
            width = None
        if height == 0:
            height = None
        # Own contiguous copies so callers cannot mutate arrays while native code has released its thread state.
        coordinates = np.array(xy, dtype=np.float64, order="C", copy=True)
        if coordinates.ndim != 2 or coordinates.shape[1] != 2 or len(coordinates) < 4:
            raise ValueError("xy must have shape (N, 2) with at least four sources")
        if len(coordinates) > np.iinfo(np.int32).max or not np.isfinite(coordinates).all():
            raise ValueError("xy must contain a supported number of finite coordinates")
        fluxes = None if flux is None else np.array(flux, dtype=np.float64, order="C", copy=True)
        if fluxes is not None and (fluxes.shape != (len(coordinates),) or not np.isfinite(fluxes).all()):
            raise ValueError("flux must be a finite one-dimensional array of the same length as xy")
        if not np.isfinite(
            [value for value in (ra_hours, dec_degs, radius, min_scale, max_scale) if value is not None]
        ).all():
            raise ValueError("Search coordinates, radius, and scales must be finite")
        if (dec_degs is not None and not -90 <= dec_degs <= 90) or not 0 <= radius <= 180:
            raise ValueError("dec_degs must be in [-90, 90] or None, and radius in [0, 180]")
        if not 0 < min_scale <= max_scale:
            raise ValueError("Scales must satisfy 0 < min_scale <= max_scale")
        for name, value in (
            ("sip_order", sip_order),
            ("sip_inverse_order", sip_inverse_order),
            ("max_sources", max_sources),
            ("maximum_quads", maximum_quads),
            ("maximum_matches", maximum_matches),
            ("width", width),
            ("height", height),
        ):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, np.integer))
                or not 0 <= value <= np.iinfo(np.int32).max
            ):
                raise ValueError(f"{name} must be a nonnegative C integer")
        if not 0 <= sip_order < an_engine.SIP_MAXORDER - 1:
            raise ValueError(f"sip_order must be from 0 to {an_engine.SIP_MAXORDER - 2}")
        if sip_inverse_order is not None and not 0 <= sip_inverse_order < an_engine.SIP_MAXORDER:
            raise ValueError(f"sip_inverse_order must be from 0 to {an_engine.SIP_MAXORDER - 1}")
        if any(size is not None and size <= 1 for size in (width, height)):
            raise ValueError("width and height must be integers greater than one")
        for name, flag in (
            ("parity", parity),
            ("crpix_center", crpix_center),
            ("retry_lost", retry_lost),
            ("deduplicate", deduplicate),
            ("uniformize_index", uniformize_index),
            ("distance_from_quad_bonus", distance_from_quad_bonus),
        ):
            if flag is not None and not isinstance(flag, bool):
                raise TypeError(f"{name} must be a bool" + (" or None" if name == "parity" else ""))
            if flag is None and name != "parity":
                raise TypeError(f"{name} must be a bool")
        for name, number in (
            ("positional_noise_pixels", positional_noise_pixels),
            ("distractor_ratio", distractor_ratio),
            ("code_tolerance_l2_distance", code_tolerance_l2_distance),
            ("minimum_quad_size_pixels", minimum_quad_size_pixels),
            ("minimum_quad_size_fraction", minimum_quad_size_fraction),
            ("maximum_quad_size_pixels", maximum_quad_size_pixels),
            ("logodds_threshold", logodds_threshold),
            ("logodds_bail_threshold", logodds_bail_threshold),
            ("logodds_stoplooking", logodds_stoplooking),
            ("logodds_to_tune", logodds_to_tune),
        ):
            if number is not None and (
                isinstance(number, bool) or not isinstance(number, (int, float, np.number)) or not math.isfinite(number)
            ):
                raise ValueError(f"{name} must be a finite real number")
        if positional_noise_pixels < 0 or code_tolerance_l2_distance < 0 or not 0 < distractor_ratio <= 1:
            raise ValueError("Noise and code tolerance must be nonnegative; distractor_ratio must be in (0, 1]")
        if minimum_quad_size_fraction < 0:
            raise ValueError("minimum_quad_size_fraction must be nonnegative")
        if maximum_quad_size_pixels < 0 or (minimum_quad_size_pixels is not None and minimum_quad_size_pixels < 0):
            raise ValueError("Quad sizes must be nonnegative")
        if (
            maximum_quad_size_pixels
            and minimum_quad_size_pixels is not None
            and minimum_quad_size_pixels > maximum_quad_size_pixels
        ):
            raise ValueError("Minimum quad size must not exceed the maximum")
        if np.any(np.ptp(coordinates, axis=0) == 0):
            raise ValueError("Source coordinates must span both axes")
        if getattr(_active, "running", False):
            raise RuntimeError("Solver operations cannot be called from a timer callback")
        settings = SolveSettings(
            source_count=len(coordinates),
            has_flux=fluxes is not None,
            width=int(width) if width else None,
            height=int(height) if height else None,
            ra_hours=None if ra_hours is None else float(ra_hours),
            dec_degs=None if dec_degs is None else float(dec_degs),
            radius=float(radius),
            min_scale=float(min_scale),
            max_scale=float(max_scale),
            parity=parity,
            sip_order=int(sip_order),
            crpix_center=crpix_center,
            max_sources=int(max_sources) if max_sources else None,
            retry_lost=retry_lost,
            deduplicate=deduplicate,
            uniformize_index=uniformize_index,
            positional_noise_pixels=float(positional_noise_pixels),
            distractor_ratio=float(distractor_ratio),
            code_tolerance_l2_distance=float(code_tolerance_l2_distance),
            distance_from_quad_bonus=distance_from_quad_bonus,
            sip_inverse_order=None if sip_inverse_order is None else int(sip_inverse_order),
            maximum_quads=int(maximum_quads),
            maximum_matches=int(maximum_matches),
            minimum_quad_size_pixels=None if minimum_quad_size_pixels is None else float(minimum_quad_size_pixels),
            minimum_quad_size_fraction=float(minimum_quad_size_fraction),
            maximum_quad_size_pixels=float(maximum_quad_size_pixels),
            logodds_threshold=float(logodds_threshold),
            logodds_bail_threshold=float(logodds_bail_threshold),
            logodds_stoplooking=None if logodds_stoplooking is None else float(logodds_stoplooking),
            logodds_to_tune=None if logodds_to_tune is None else float(logodds_to_tune),
        )
        # Keep all attempts, their snapshots, and native cleanup under the process-wide lock.
        attempts: list[SolverState] = []
        wcs: WCS | None = None
        retry_unrestricted = retry_lost and (
            parity is not None or (ra_hours is not None and dec_degs is not None and radius < 180)
        )
        with _engine_lock:
            for unrestricted in (False, True) if retry_unrestricted else (False,):
                wcs, state = self._solve(
                    coordinates,
                    fluxes,
                    settings,
                    None if unrestricted else settings.ra_hours,
                    None if unrestricted else settings.dec_degs,
                    180.0 if unrestricted else float(radius),
                    None if unrestricted else parity,
                )
                attempts.append(state)
                if wcs is not None or state.canceled:
                    break
                if not unrestricted and retry_unrestricted:
                    _logger.warning("Retrying as lost-in-space")
        return SolveResult(wcs, settings, tuple(attempts))

    def _solve(
        self,
        xy: NDArray[np.float64],
        flux: NDArray[np.float64] | None,
        settings: SolveSettings,
        ra_hours: float | None,
        dec_degs: float | None,
        radius: float,
        parity: bool | None,
    ) -> tuple[WCS | None, SolverState]:
        """Search owned xy/flux with settings and effective position/parity, then snapshot before cleanup."""
        width, height = settings.width, settings.height
        min_scale, max_scale = settings.min_scale, settings.max_scale
        sip_order, crpix_center, max_sources = settings.sip_order, settings.crpix_center, settings.max_sources
        timer_callback = self.timer_callback
        _logger.info(
            "Solving field at α = %s, δ = %s, radius=%s, scale=%s..%s″/px, parity=%s, SIP order=%s with %d detections",
            "unspecified" if ra_hours is None else _format_angle(ra_hours, False),
            "unspecified" if dec_degs is None else _format_angle(dec_degs, True),
            "unrestricted" if ra_hours is None or dec_degs is None else f"{radius}°",
            min_scale,
            max_scale,
            parity,
            sip_order,
            len(xy),
        )

        wcs = None
        with _engine_lock:
            if getattr(_active, "running", False):
                raise RuntimeError("Solver operations cannot be called from a timer callback")
            if self._solver is None:
                raise RuntimeError("Solver is closed")
            solver = self._solver
            ra = None if ra_hours is None else ra_hours * 15
            dec = dec_degs
            # The same position constraint controls native verification and index preselection.
            r = 180.0 if ra is None or dec is None else float(radius)

            # Set field star position array
            n = len(xy)
            if max_sources and n > max_sources:
                _logger.info("Limiting to %d brightest sources", max_sources)
                n = max_sources
            field = an_engine.starxy_new(n, flux is not None, False)
            if field is None:
                raise MemoryError("Unable to allocate field sources")
            # Transfer field ownership immediately so all subsequent failures are cleaned up.
            an_engine.solver_set_field(solver, field)
            try:
                if flux is not None:
                    # Keep bright sources ahead of both the requested and native search limits.
                    order = np.argsort(flux)[::-1][:n]
                    xy, flux = xy[order], flux[order]
                    an_engine.starxy_set_flux_array(field, np.ascontiguousarray(flux).ravel())
                an_engine.starxy_set_xy_array(field, np.ascontiguousarray(xy).ravel())
                # Initialize solver parameters
                if width:
                    minx, maxx = 1, int(width)
                else:
                    minx, maxx = xy[:, 0].min(), xy[:, 0].max()
                    width = maxx - minx + 1
                if height:
                    miny, maxy = 1, int(height)
                else:
                    miny, maxy = xy[:, 1].min(), xy[:, 1].max()
                    height = maxy - miny + 1
                an_engine.solver_set_field_bounds(solver, minx, maxx, miny, maxy)

                an_engine.solver_reset_counters(solver)
                # Upstream reset omits this counter and the elapsed CPU time.
                solver.num_meanx_skipped = 0
                solver.timeused = 0.0

                solver.quadsize_min = (
                    settings.minimum_quad_size_fraction * min(maxx - minx + 1, maxy - miny + 1)
                    if settings.minimum_quad_size_pixels is None
                    else settings.minimum_quad_size_pixels
                )
                solver.quadsize_max = settings.maximum_quad_size_pixels
                if solver.quadsize_max and solver.quadsize_min > solver.quadsize_max:
                    raise ValueError("Effective minimum quad size exceeds the maximum")

                solver.set_crpix = solver.set_crpix_center = crpix_center

                if ra is None or dec is None:
                    an_engine.solver_clear_radec(solver)
                else:
                    an_engine.solver_set_radec(solver, ra, dec, r)

                solver.funits_lower = float(min_scale)
                solver.funits_upper = float(max_scale)

                solver.logratio_toprint = 0.0
                solver.logratio_tokeep = settings.logodds_threshold
                solver.logratio_bail_threshold = settings.logodds_bail_threshold
                # LARGE_VAL is the upstream sentinel for disabled thresholds (mathutil.h).
                solver.logratio_stoplooking = (
                    1e30 if settings.logodds_stoplooking is None else settings.logodds_stoplooking
                )
                solver.logratio_totune = (
                    1e30 if sip_order < 2 or settings.logodds_to_tune is None else settings.logodds_to_tune
                )
                solver.verify_dedup = settings.deduplicate
                solver.verify_uniformize = settings.uniformize_index
                solver.verify_pix = settings.positional_noise_pixels
                solver.distractor_ratio = settings.distractor_ratio
                solver.codetol = settings.code_tolerance_l2_distance
                solver.distance_from_quad_bonus = settings.distance_from_quad_bonus
                solver.maxquads = settings.maximum_quads
                solver.maxmatches = settings.maximum_matches
                solver.startobj = 0

                if parity is None:
                    solver.parity = an_engine.PARITY_BOTH
                elif parity:
                    solver.parity = an_engine.PARITY_FLIP
                else:
                    solver.parity = an_engine.PARITY_NORMAL

                enable_sip = sip_order and int(sip_order) >= 2
                if enable_sip:
                    solver.do_tweak = True
                    solver.tweak_aborder = int(sip_order)
                    solver.tweak_abporder = (
                        sip_order + 1 if settings.sip_inverse_order is None else settings.sip_inverse_order
                    )
                else:
                    solver.do_tweak = False
                    solver.tweak_aborder = solver.tweak_abporder = 3

                if max_sources:
                    solver.endobj = max_sources
                else:
                    solver.endobj = 0

                # Add indices needed to solve the field
                fmin = solver.quadsize_min * min_scale
                fmax = np.hypot(maxx - minx + 1, maxy - miny + 1) * max_scale
                indices = []
                distances = {}
                for index in self._indexes:
                    if fmin > index.index_scale_upper or fmax < index.index_scale_lower:
                        continue

                    if ra is not None and dec is not None and index.healpix >= 0:
                        distance = an_engine.healpix_distance_to_radec(index.healpix, index.hpnside, ra, dec)
                    else:
                        distance = 0
                    if distance <= r:
                        indices.append(index)
                        distances[index] = distance
                if not indices:
                    raise ValueError("No indexes found for the given scale and position")

                # Sort indices by scale (larger scales/smaller indices first - should be faster) then by distance from
                # the expected position
                _logger.info("%d suitable ind%s found", len(indices), "ex" if len(indices) == 1 else "ices")
                indices.sort(key=lambda _idx: (-_idx.index_scale_upper, distances[_idx]))
                an_engine.solver_clear_indexes(solver)
                for index in indices:
                    an_engine.solver_add_index(solver, index)

                # Release Python's thread state in C; reattach it only while the Python timer runs.
                _active.running = True
                try:
                    continued = an_engine.solver_run_with_callback(
                        solver, (lambda: timer_callback(self)) if timer_callback is not None else None
                    )
                finally:
                    _active.running = False
                if solver.have_best_match:
                    best_match = solver.best_match
                else:
                    best_match = None

                if continued and solver.best_match_solves and best_match is not None:
                    _logger.info("Solution found")
                    # Get WCS parameters of best solution
                    wcs = WCS(naxis=2)
                    suffix = ""
                    wcstan = best_match.wcstan

                    if enable_sip:
                        sip = best_match.sip
                        if sip is not None:
                            sip_wcstan = sip.wcstan
                            a_order, b_order = sip.a_order, sip.b_order
                            if a_order > 0 or b_order > 0:
                                ap_order, bp_order = sip.ap_order, sip.bp_order
                                maxorder = an_engine.SIP_MAXORDER
                                a = _array_from_swig(sip.a, (maxorder, maxorder))[: a_order + 1, : a_order + 1]
                                b = _array_from_swig(sip.b, (maxorder, maxorder))[: b_order + 1, : b_order + 1]
                                if a.any() or b.any():
                                    ap = _array_from_swig(sip.ap, (maxorder, maxorder))[: ap_order + 1, : ap_order + 1]
                                    bp = _array_from_swig(sip.bp, (maxorder, maxorder))[: bp_order + 1, : bp_order + 1]
                                    # Astropy's extension lacks Sip constructor hints and infers WCS.sip as None.
                                    wcs.sip = Sip(  # pyright: ignore[reportAttributeAccessIssue]
                                        a,  # pyright: ignore[reportCallIssue]
                                        b,
                                        ap,
                                        bp,
                                        _array_from_swig(sip_wcstan.crpix, (2,)),
                                    )
                                    suffix = "-SIP"
                                    wcstan = sip_wcstan

                    if wcstan.sin:
                        proj = "-SIN"
                    else:
                        proj = "-TAN"

                    wcs.wcs.ctype = "RA--" + proj + suffix, "DEC-" + proj + suffix
                    wcs.wcs.crpix = _array_from_swig(wcstan.crpix, (2,))
                    wcs.wcs.crval = _array_from_swig(wcstan.crval, (2,))
                    wcs.wcs.cd = _array_from_swig(wcstan.cd, (2, 2))
                elif continued:
                    _logger.warning("Solving field failed")
                else:
                    _logger.warning("Solving field canceled")
                centerxyz = _array_from_swig(solver.centerxyz, (3,))
                crpix = _array_from_swig(solver.crpix, (2,))
                state = SolverState(
                    pixel_xscale=float(solver.pixel_xscale),
                    funits_lower=float(solver.funits_lower),
                    funits_upper=float(solver.funits_upper),
                    logratio_toprint=float(solver.logratio_toprint),
                    logratio_tokeep=float(solver.logratio_tokeep),
                    logratio_totune=float(solver.logratio_totune),
                    verify_pix=float(solver.verify_pix),
                    distractor_ratio=float(solver.distractor_ratio),
                    codetol=float(solver.codetol),
                    quadsize_min=float(solver.quadsize_min),
                    quadsize_max=float(solver.quadsize_max),
                    r2=float(solver.r2),
                    logratio_bail_threshold=float(solver.logratio_bail_threshold),
                    logratio_stoplooking=float(solver.logratio_stoplooking),
                    minminAB2=float(solver.minminAB2),
                    maxmaxAB2=float(solver.maxmaxAB2),
                    field_minx=float(solver.field_minx),
                    field_maxx=float(solver.field_maxx),
                    field_miny=float(solver.field_miny),
                    field_maxy=float(solver.field_maxy),
                    field_diag=float(solver.field_diag),
                    timeused=float(solver.timeused),
                    best_logodds=float(solver.best_logodds),
                    tweak_aborder=int(solver.tweak_aborder),
                    tweak_abporder=int(solver.tweak_abporder),
                    startobj=int(solver.startobj),
                    endobj=int(solver.endobj),
                    parity=int(solver.parity),
                    maxquads=int(solver.maxquads),
                    maxmatches=int(solver.maxmatches),
                    numtries=int(solver.numtries),
                    nummatches=int(solver.nummatches),
                    numscaleok=int(solver.numscaleok),
                    last_examined_object=int(solver.last_examined_object),
                    num_cxdx_skipped=int(solver.num_cxdx_skipped),
                    num_meanx_skipped=int(solver.num_meanx_skipped),
                    num_radec_skipped=int(solver.num_radec_skipped),
                    num_abscale_skipped=int(solver.num_abscale_skipped),
                    num_verified=int(solver.num_verified),
                    distance_from_quad_bonus=bool(solver.distance_from_quad_bonus),
                    verify_uniformize=bool(solver.verify_uniformize),
                    verify_dedup=bool(solver.verify_dedup),
                    do_tweak=bool(solver.do_tweak),
                    use_radec=bool(solver.use_radec),
                    set_crpix=bool(solver.set_crpix),
                    set_crpix_center=bool(solver.set_crpix_center),
                    quit_now=bool(solver.quit_now),
                    best_match_solves=bool(solver.best_match_solves),
                    have_best_match=bool(solver.have_best_match),
                    centerxyz=(float(centerxyz[0]), float(centerxyz[1]), float(centerxyz[2])),
                    crpix=(float(crpix[0]), float(crpix[1])),
                    index_names=tuple(index.indexname for index in indices),
                    canceled=not bool(continued),
                    best_index_name=an_engine.solver_best_index_name(solver),
                    best_match=None
                    if best_match is None
                    else MatchObj(
                        code_err=float(best_match.code_err),
                        radius_deg=float(best_match.radius_deg),
                        scale=float(best_match.scale),
                        logodds=float(best_match.logodds),
                        worstlogodds=float(best_match.worstlogodds),
                        index_jitter=float(best_match.index_jitter),
                        quadno=int(best_match.quadno),
                        dimquads=int(best_match.dimquads),
                        quad_npeers=int(best_match.quad_npeers),
                        nmatch=int(best_match.nmatch),
                        ndistractor=int(best_match.ndistractor),
                        nconflict=int(best_match.nconflict),
                        nfield=int(best_match.nfield),
                        nindex=int(best_match.nindex),
                        nbest=int(best_match.nbest),
                        nagree=int(best_match.nagree),
                        indexid=int(best_match.indexid),
                        healpix=int(best_match.healpix),
                        hpnside=int(best_match.hpnside),
                        quads_tried=int(best_match.quads_tried),
                        quads_matched=int(best_match.quads_matched),
                        quads_scaleok=int(best_match.quads_scaleok),
                        objs_tried=int(best_match.objs_tried),
                        nverified=int(best_match.nverified),
                        parity=bool(best_match.parity),
                        wcs_valid=bool(best_match.wcs_valid),
                    ),
                )
                return wcs, state

            finally:
                # Make solver ready for the next solution
                an_engine.solver_cleanup_field(solver)
