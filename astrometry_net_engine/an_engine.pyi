# Copyright (c) 2026 Vladimir Kouprianov (v.kouprianov@gmail.com)
# SPDX-License-Identifier: GPL-3.0-only
"""Types for the native subset used by the public Solver API (upstream headers are authoritative)."""

from collections.abc import Callable
from typing import SupportsInt

from numpy import float64
from numpy.typing import NDArray

SIP_MAXORDER: int
PARITY_NORMAL: int
PARITY_FLIP: int
PARITY_BOTH: int

class index_t:
    indexname: str
    index_scale_upper: float
    index_scale_lower: float
    healpix: int
    hpnside: int

class tan_t:
    crpix: SupportsInt
    crval: SupportsInt
    cd: SupportsInt
    sin: int

class sip_t:
    wcstan: tan_t
    a_order: int
    b_order: int
    ap_order: int
    bp_order: int
    a: SupportsInt
    b: SupportsInt
    ap: SupportsInt
    bp: SupportsInt

class MatchObj:
    wcstan: tan_t
    sip: sip_t | None
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

class solver_t:
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
    centerxyz: SupportsInt
    crpix: SupportsInt
    best_match: MatchObj

class starxy_t: ...

def solver_new() -> solver_t | None: ...
def solver_free(solver: solver_t) -> None: ...
def solver_run_with_callback(solver: solver_t, callback: Callable[[], int] | None) -> int: ...
def solver_clear_indexes(solver: solver_t) -> None: ...
def solver_add_index(solver: solver_t, index: index_t) -> None: ...
def solver_cleanup_field(solver: solver_t) -> None: ...
def solver_set_field(solver: solver_t, field: starxy_t) -> None: ...
def solver_set_field_bounds(solver: solver_t, minx: float, maxx: float, miny: float, maxy: float) -> None: ...
def solver_set_radec(solver: solver_t, ra: float, dec: float, radius: float) -> None: ...
def solver_clear_radec(solver: solver_t) -> None: ...
def index_load(filename: str, flags: int, dest: None) -> index_t | None: ...
def index_free(index: index_t) -> None: ...
def starxy_new(n: int, flux: bool, background: bool) -> starxy_t | None: ...
def starxy_set_xy_array(field: starxy_t, xy: NDArray[float64]) -> None: ...
def starxy_set_flux_array(field: starxy_t, flux: NDArray[float64]) -> None: ...
def healpix_distance_to_radec(hp: int, nside: int, ra: float, dec: float) -> float: ...
def solver_best_index_name(solver: solver_t) -> str | None: ...
def solver_reset_counters(solver: solver_t) -> None: ...
