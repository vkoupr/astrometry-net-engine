# astrometry-net-engine

Independent, cross-platform Python bindings to the Astrometry.net plate-solving engine, producing WCS solutions from
pre-extracted source coordinates.

> This is an independently developed Python interface to the Astrometry.net plate-solving engine. It is not affiliated
> with or maintained by the Astrometry.net project.

Give the solver a list of measured XY star positions and it returns a result containing an `astropy.wcs.WCS`
transformation, with optional SIP distortion coefficients. Source extraction belongs in your application: this package
does not detect stars in images or provide a complete image-to-WCS pipeline.

- Self-contained package (except indexes), no Astrometry.net runtime installation and configuration needed.
- Native extensions on Linux, macOS, and Windows with MSVC; x86_64/amd64 and arm64/aarch64 on all platforms.
- Reuse a solver and its loaded indexes across many fields.
- Constrain the search by position, pixel scale, parity, or source count, with an optional unrestricted retry.
- Use Python callbacks to check progress or cancel a search.

When approximate field coordinates are available, the wrapper filters and orders indexes to focus the search,
potentially improving solving performance.

## Installation

```sh
python -m pip install astrometry-net-engine
```

### Build from source

```sh
git clone --recurse-submodules https://github.com/vkoupr/astrometry-net-engine.git
cd astrometry-net-engine
python -m pip install .
```

You need GCC or Clang on Linux, Xcode Command Line Tools on macOS, or Visual Studio Build Tools with MSVC and the
Windows SDK on Windows. Windows ARM64 cross-compilation also needs the ARM64 MSVC tools and target Python headers and
libraries.

A source distribution already contains the required C sources, headers, patches, and generated wrappers. It needs
neither Git nor SWIG, and the build does not download upstream sources:

```sh
python -m pip install ./astrometry_net_engine-1.0.0.tar.gz
```

SWIG 4.4.0 or newer is optional. If installed, it regenerates the wrappers; otherwise the build uses the included files.
Set `AN_ENGINE_SKIP_SWIG=1` to use those files even when SWIG is available. For a fully offline installation, install
the Python build and runtime dependencies in advance and use `--no-index --no-build-isolation`.

To build both wheel and source distributions:

```sh
python -m pip install build
python -m build
```

## Getting started

Download suitable index files first; see [Index files](#index-files). Supply source coordinates using the FITS
convention, where the bottom-left pixel center is `(1, 1)`. Add one if your extraction tool returns coordinates starting
at zero.

```python
import numpy as np
from astrometry_net_engine import Solver

# N rows of measured X/Y positions, with at least four detections.
xy = np.loadtxt("sources.xy")
with Solver("/path/to/indexes") as solver:
    result = solver.solve(
        xy,
        width=2048,
        height=2048,
        ra_hours=12.5,
        dec_degs=45.0,
        radius=2.0,
        min_scale=0.8,
        max_scale=1.2,
    )
    if result.wcs is not None:
        ra_dec = result.wcs.all_pix2world([[1024, 1024]], 1)
        print(ra_dec)
        result.wcs.to_header(relax=True).tofile("solution.hdr", overwrite=True)
```

Don't set `ra_hours`, `dec_degs`, and `radius` to perform unconstrained all-sky blind search. Pixel scale constraints
still apply, although the default `min_scale=0.1`, `max_scale=10.0` works for almost any possible imaging system. Note
that relaxed constraints may increase the probability of a false match.

Without fluxes, put the brightest sources first if applicable. When fluxes are supplied (see [Solving a
field](#solving-a-field)), coordinates and fluxes are always sorted together by decreasing flux, including with
`max_sources=None` or zero. This keeps bright sources ahead of the native Astrometry.net 1000-source quad-search limit.
An explicit `max_sources` limit also removes fainter sources from verification; without it, verification can use the
full input field.

## Solver API

`solver = Solver(index_path)` loads indexes once. `index_path` accepts a directory string, a `Path`, a sequence of
directories, or an OS-path-separated string (`:` on Unix, `;` on Windows). Non-index files are ignored; no valid indexes
raises `ValueError`.

Use a context manager or call `close()` when finished. Closing twice is harmless; solving after close raises
`RuntimeError`. Each instance owns its native resources. You can reuse an instance for any number of fields.

### Solving a field

```python
result = solver.solve(xy, param=value, ...)
```

or

```python
result = solver.solve(xy, flux, param=value, ...)
```

| Parameter, default | Meaning | Astrometry.net equivalent |
| --- | --- | --- |
| `xy` | Finite numeric array of shape `(N, 2)`, at least four sources, in pixels, 1-based. `xy[:,0]` is _X_, `xy[:,1]` is _Y_. | – |
| `flux` | Optional finite array of shape `(N,)`; supplied sources are always sorted brightest first. | – |
| `width`, `height` | Image dimensions in pixels; omit to derive bounds from the source coordinates. | – |
| `ra_hours=None`, `dec_degs=None` | Approximate center: RA in **hours**, Dec in degrees; omit for blind search. | – |
| `radius=180.0` | Search radius, 0–180 degrees; ignored unless both coordinates are provided. | – |
| `min_scale=0.1`, `max_scale=10.0` | Positive pixel scale limits in arcseconds per pixel. | `funits_lower`, `funits_upper` |
| `parity=None` | `False`: normal; `True`: flipped; `None`: try both. | `parity` |
| `retry_lost=True` | Retry a failed constrained search without position/parity limits; scale limits remain. | – |
| `sip_order=3` | 0 or 1 disables distortion fitting; 2–8 selects the SIP polynomial order. | `tweak_aborder` |
| `sip_inverse_order=None` | Inverse SIP order 0–9; `None` uses `sip_order + 1`. | `tweak_abporder` |
| `crpix_center=True` | Place the WCS reference pixel at image center. | `set_crpix`, `set_crpix_center` |
| `max_sources=None` | Nonnegative source limit; 0 or `None` adds no limit beyond the native cap. | `endobj` |
| `deduplicate=True` | Deduplicate nearby field detections during verification. | `verify_dedup` |
| `uniformize_index=True` | Uniformize field detections at the index density scale. | `verify_uniformize` |
| `positional_noise_pixels=1.0` | Field positional uncertainty in pixels, nonnegative. | `verify_pix` |
| `distractor_ratio=0.25` | Fraction of distractors in verification, greater than zero and at most one. | `distractor_ratio` |
| `code_tolerance_l2_distance=0.01` | Nonnegative L2 tolerance in the 4D quad code space. | `codetol` |
| `distance_from_quad_bonus=True` | Allow greater positional variance farther from the matched quad. | `distance_from_quad_bonus` |
| `maximum_quads=0`, `maximum_matches=0` | Per-attempt search limits; zero means unlimited. | `maxquads`, `maxmatches` |
| `minimum_quad_size_pixels=None` | Minimum quad size in pixels; `None` uses the fraction below. | `quadsize_min` |
| `minimum_quad_size_fraction=0.1` | Nonnegative fraction of the smaller image dimension, unless pixels are supplied. | – |
| `maximum_quad_size_pixels=0.0` | Maximum quad size in pixels; zero adds no limit. | `quadsize_max` |
| `logodds_threshold=log(1e12)` | Acceptance threshold for a solution. | `logratio_tokeep` |
| `logodds_bail_threshold=log(1e-100)` | Reject a candidate when verification falls below this threshold. | `logratio_bail_threshold` |
| `logodds_stoplooking=None` | Stop verification early above this threshold; default keeps looking for peak odds. | `logratio_stoplooking` |
| `logodds_to_tune=14.0` | Intermediate SIP refinement threshold; `None` disables tuning. | `logratio_totune` |

Omit the coordinates, or pass `None` for either, to disable the position constraint and ignore `radius`. A retry uses
`ra_hours=None`, `dec_degs=None`, `radius=180`, and `parity=None`. Searches already unrestricted in position and parity
are not repeated; in particular, the default `None` coordinates and parity never trigger a second attempt. A parity
constraint can still trigger a retry without a known position.

Invalid inputs raise `ValueError` or `TypeError`; a lack of indexes covering the requested scale and position raises
`ValueError`. Log-odds values are natural logarithms, not odds ratios. Quad and match limits are checked after each
source, so the counters can exceed a configured limit. Index quad sizes and pixel-scale constraints can further tighten
the quad range. When `sip_order` is 0 or 1, SIP fitting is disabled and `sip_inverse_order` and `logodds_to_tune` are
ignored. An explicit inverse order is passed directly to the native fitter, including zero.

The method always returns a [`SolveResult`](#results-and-diagnostics). Its `wcs` is `None` when the search fails or is
canceled; `solved` and `canceled` distinguish these outcomes.

### Results and diagnostics

`SolveResult`, `SolveSettings`, `SolverState`, and `MatchObj` are frozen, typed dataclasses exported by the package.
Settings describe the **original request**; each attempt records its effective native configuration. An unrestricted
retry keeps verification, scale, SIP, and search limits while relaxing position and parity. Snapshots contain owned
Python values and remain valid after solver reuse or close. The Astropy WCS owns its arrays and remains editable.

The references below describe values returned by `solve()`; constructing a dataclass directly does not validate ranges.
Counts and source indices are integers. Log-odds quantities use natural logarithms; native scores can contain sentinel
or infinite values. `None` denotes an absent value, rather than a numeric sentinel.

#### `SolveResult`

| Field | Description, units and range |
| --- | --- |
| `wcs` | Accepted Astropy `WCS` with optional SIP, or `None`; celestial coordinates are in degrees. |
| `settings` | The original normalized [`SolveSettings`](#solvesettings); no source or flux arrays are retained. |
| `attempts` | Tuple of [`SolverState`](#solverstate) snapshots in search order; one attempt, or two after an unrestricted retry. |
| `solved` | Boolean property: `wcs is not None`. |
| `canceled` | Boolean property: the final attempt was canceled by the timer; no retry follows cancellation. |

#### `SolveSettings`

All solve options are retained after normalization: input zero dimensions and source limits become `None`. Coordinates
and flux arrays are represented only by `source_count` and `has_flux`. Defaults are listed above.

| Field | Description, units and range |
| --- | --- |
| `source_count` | Number of supplied detections; integer 4–2³¹−1. |
| `has_flux` | Boolean: a flux array was supplied. |
| `width`, `height` | Requested image dimensions, pixels; integers 2–2³¹−1 or `None` for inferred bounds. |
| `ra_hours` | Requested RA, hours; `None` or finite, conventionally [0, 24). `None` disables position. |
| `dec_degs` | Requested Dec, degrees in [−90, 90], or `None` to disable position. |
| `radius` | Requested radius, degrees in [0, 180]; used only with both coordinates; 180 means all sky. |
| `min_scale`, `max_scale` | Requested lower/upper scale, arcsec/pixel; finite and `0 < min_scale <= max_scale`. |
| `parity` | `False` normal, `True` flipped, or `None` for both. |
| `retry_lost` | Boolean: retry a failed position/parity-constrained attempt without those constraints. |
| `sip_order` | Forward SIP order, integer 0–8; 0 or 1 disables fitting. |
| `sip_inverse_order` | Inverse SIP order, integer 0–9 or `None` for `sip_order + 1`; ignored if SIP is disabled. |
| `crpix_center` | Boolean: constrain the WCS reference pixel to the native field center. |
| `max_sources` | Positive integer ≤2³¹−1, or `None` for no extra limit; input zero normalizes to `None`. |
| `deduplicate` | Boolean: deduplicate nearby field detections during verification. |
| `uniformize_index` | Boolean: spatially uniformize field detections at the index density scale. |
| `positional_noise_pixels` | Field-position uncertainty, pixels; finite and ≥0. |
| `distractor_ratio` | Assumed distractor fraction, dimensionless, in (0, 1]. |
| `code_tolerance_l2_distance` | Dimensionless quad-code L2 tolerance, finite and ≥0; 4D for four-star quads. |
| `distance_from_quad_bonus` | Boolean: increase allowed positional variance away from the matched quad. |
| `maximum_quads`, `maximum_matches` | Per-attempt quad/hit limits; integers 0–2³¹−1, with zero unlimited. |
| `minimum_quad_size_pixels` | Minimum quad size, pixels; finite and ≥0, or `None` to use the fraction below. |
| `minimum_quad_size_fraction` | Fraction of smaller image dimension, finite and ≥0; explicit pixels override it. |
| `maximum_quad_size_pixels` | Maximum quad size, pixels; finite and ≥0, with zero adding no limit. |
| `logodds_threshold` | Acceptance threshold, natural-log odds; any finite real value. |
| `logodds_bail_threshold` | Verification rejection threshold, natural-log odds; any finite real value. |
| `logodds_stoplooking` | Early verification stop threshold, natural-log odds; finite real or `None` to disable. |
| `logodds_to_tune` | Intermediate refinement threshold, natural-log odds; finite or `None`; ignored without SIP. |

#### `SolverState`

Native field names follow [solver.h][native-solver]. Values are captured before cleanup under the engine lock. Counters
and CPU time reset for each attempt. `centerxyz` and `r2` are inactive when `use_radec` is false: native clearing leaves
their numeric values at zero or at an earlier constraint. The native search examines at most 1000 sources; verification
can use more detections. Quad/hit limits are checked after each source, so counts can exceed their configured limits.

| Field | Description, units and range |
| --- | --- |
| `pixel_xscale` | Native _X_-coordinate scale factor; dimensionless, zero disables scaling (the public API uses zero). |
| `funits_lower`, `funits_upper` | Effective lower/upper pixel-scale bounds, arcsec/pixel; positive and ordered. |
| `logratio_toprint` | Minimum natural-log odds for reporting a candidate; zero in this interface. |
| `logratio_tokeep` | Effective acceptance threshold, natural-log odds; finite real. |
| `logratio_totune` | Effective intermediate-tuning threshold; natural-log odds, `1e30` when disabled. |
| `logratio_bail_threshold` | Effective verification rejection threshold, natural-log odds; finite real. |
| `logratio_stoplooking` | Effective early-stop threshold, natural-log odds; `1e30` disables early stopping. |
| `verify_pix` | Effective field-position uncertainty, pixels; ≥0. |
| `distractor_ratio` | Effective verification distractor fraction in (0, 1]. |
| `codetol` | Effective dimensionless quad-code L2 tolerance; ≥0. |
| `quadsize_min`, `quadsize_max` | Configured field quad bounds, pixels; ≥0, with zero maximum adding no limit. |
| `minminAB2`, `maxmaxAB2` | Index-tightened quad-separation bounds, pixels²; ≥0; empty if lower > upper. |
| `field_minx`, `field_maxx` | Effective _X_ bounds, pixels; arbitrary coordinates with minimum < maximum. |
| `field_miny`, `field_maxy` | Effective _Y_ bounds, pixels; arbitrary coordinates with minimum < maximum. |
| `field_diag` | Diagonal of the native coordinate spans, pixels; >0. |
| `centerxyz` | Search-center unit vector, components in [−1, 1]; meaningful only with `use_radec=True`. |
| `r2` | Squared chord radius [0, 4]: `4*sin(r/2)²`, `r` in radians; active only with `use_radec=True`. |
| `use_radec` | Boolean: position filtering is active; false if either coordinate was omitted or on a retry. |
| `parity` | Native integer: 0 normal, 1 flipped, 2 both; matches `False`, `True`, `None` in settings. |
| `distance_from_quad_bonus` | Boolean: enable positional-variance growth away from the matched quad. |
| `verify_uniformize` | Boolean: spatially uniformize field detections during verification. |
| `verify_dedup` | Boolean: remove nearby duplicate field detections during verification. |
| `do_tweak` | Boolean: enable final SIP fitting. |
| `tweak_aborder`, `tweak_abporder` | Forward/inverse SIP orders: 2–8 and 0–9; inactive when fitting is off. |
| `startobj`, `endobj` | Zero-based first/exclusive-last search source; integers ≥0; `endobj=0` means all sources. |
| `maxquads`, `maxmatches` | Configured quad/hit limits, integers ≥0; zero means unlimited. |
| `set_crpix`, `set_crpix_center` | Booleans: constrain CRPIX, and use the field center, respectively. |
| `crpix` | Target reference-pixel pair, in pixels; meaningful when `set_crpix` is true. |
| `numtries` | Field quad trials; nonnegative integer, including repeated trials across indexes. |
| `nummatches` | Index quad-code hits considered before position/scale rejection; nonnegative integer. |
| `numscaleok` | Hits passing the fitted pixel-scale check; integer from 0 through `nummatches`. |
| `last_examined_object` | Zero-based last searched source in search order, 0–999; zero is also the reset value. |
| `num_cxdx_skipped` | Code permutations rejected by the index’s `cx <= dx` ordering; nonnegative integer. |
| `num_meanx_skipped` | Code permutations rejected by the canonical mean-_X_ constraint; nonnegative integer. |
| `num_radec_skipped` | Quad hits rejected because a quad star lies outside the position bound; nonnegative integer. |
| `num_abscale_skipped` | Quad hits rejected by the quick A–B pixel-scale estimate; nonnegative integer. |
| `num_verified` | Quad verification trials counted by the solver; nonnegative integer. |
| `timeused` | Elapsed process CPU time for this attempt, seconds; ≥0, including callback work. |
| `best_logodds` | Largest recorded solver score, natural-log odds; ≥0 (initially 0); may precede SIP tuning. |
| `quit_now` | Native boolean early-exit flag, also used for a solved match; use `canceled` for timer cancellation. |
| `have_best_match` | Boolean: a native best-match record exists; equivalent to `best_match is not None`. |
| `best_match_solves` | Boolean: the native best match was accepted as solving the field. |
| `index_names` | Tuple of selected index filename strings, in search order. |
| `best_index_name` | Copied best-match index filename, or `None` without a best match. |
| `best_match` | Independent [`MatchObj`](#matchobj) snapshot, or `None` without a recorded match. |
| `canceled` | Boolean: the timer returned zero during this attempt. |

#### `MatchObj`

These scalar fields follow [matchobj.h][native-match]. Counts refer to the retained candidate, while `SolverState`
counts describe the whole attempt. Match statistics can precede the returned WCS’s final SIP refinement. Signed 16-bit
native fields are copied without widening their original storage: values outside −32768–32767 can be truncated upstream.
`nagree` is not populated by the full command-line solver’s agreement tracking here and normally remains zero.

| Field | Description, units and range |
| --- | --- |
| `code_err` | Squared quad-code L2 distance, dimensionless; ≥0. |
| `radius_deg` | Field-center to lower-left-corner angular separation, degrees in [0, 180]. |
| `scale` | Pixel scale from the stored TAN fit, arcsec/pixel; >0. |
| `logodds` | Best verification score retained for this match, natural-log odds; a real score, not a probability. |
| `worstlogodds` | Lowest prefix score retained along that verification sequence, natural-log odds; ≤0. |
| `index_jitter` | Assumed reference-catalog position uncertainty, arcseconds; ≥0. |
| `quadno` | Zero-based matched quad identifier within its index; integer ≥0. |
| `dimquads` | Number of stars defining the pattern; integer 3–5, normally 4. |
| `quad_npeers` | Index neighbors for the same field-quad code; count, normally ≥1; native signed 16-bit storage. |
| `nmatch` | Matched detections in the best verification prefix; integer 0–`nfield`. |
| `ndistractor` | Unmatched detections in that prefix; integer 0–`nfield`. |
| `nconflict` | Detections competing for an already matched reference star; integer 0–`nfield`. |
| `nfield` | Supplied field detections before verification filtering; nonnegative integer. |
| `nindex` | Reference stars within the projected image bounds; nonnegative integer. |
| `nbest` | Best-prefix length: `nmatch + ndistractor + nconflict`; integer 0–`nfield`. |
| `nagree` | Other agreeing candidates when found; native signed 16-bit count, normally zero in this direct interface. |
| `indexid` | Index-series identifier, such as 4110; native signed 16-bit integer, not an ordinal. |
| `healpix` | Index sky tile: normally 0–`12*hpnside² - 1`, or −1 for all sky; native signed 16-bit integer. |
| `hpnside` | Index HEALPix NSIDE resolution; positive for tiled indexes, possibly zero for all sky; signed 16-bit. |
| `quads_tried` | Quad trial count when this match was found; nonnegative integer. |
| `quads_matched` | Index quad-code hit count when this match was found; nonnegative integer. |
| `quads_scaleok` | Scale-passing hit count when this match was found; nonnegative integer. |
| `objs_tried` | One plus the largest zero-based quad-source index in search order; integer 1–1000. |
| `nverified` | Zero-based verification-trial ordinal for this hit; nonnegative integer (counter before increment). |
| `parity` | Native boolean: `False` normal, `True` flipped. |
| `wcs_valid` | Boolean: the native TAN WCS is valid. |

The pinned [solver implementation][native-engine], [verification code][native-verify], and
[derived-match helpers][native-derived] define the counters and match statistics.

[native-solver]:
  https://github.com/dstndstn/astrometry.net/blob/6dd9c056ff382376e1f94c14e9dc72aad68104d3/include/astrometry/solver.h
[native-match]:
  https://github.com/dstndstn/astrometry.net/blob/6dd9c056ff382376e1f94c14e9dc72aad68104d3/include/astrometry/matchobj.h
[native-engine]:
  https://github.com/dstndstn/astrometry.net/blob/6dd9c056ff382376e1f94c14e9dc72aad68104d3/solver/solver.c
[native-verify]:
  https://github.com/dstndstn/astrometry.net/blob/6dd9c056ff382376e1f94c14e9dc72aad68104d3/solver/verify.c
[native-derived]:
  https://github.com/dstndstn/astrometry.net/blob/6dd9c056ff382376e1f94c14e9dc72aad68104d3/util/matchobj.c

```python
result = solver.solve(xy, min_scale=0.8, max_scale=1.2, maximum_quads=20000)
for attempt in result.attempts:
    print(attempt.numtries, attempt.num_verified, attempt.timeused)
    if attempt.best_match is not None:
        print(attempt.best_logodds, attempt.best_match.nmatch, attempt.best_index_name)
if result.canceled:
    print("Search canceled")
elif not result.solved:
    print("No accepted match")
else:
    print(repr(result.wcs.to_header(relax=True)))
```

A native candidate record and an accepted WCS are separate results: check `result.solved` or `result.wcs` before using
the WCS, and test `attempt.best_match is not None` before reading match fields. Input coordinates and flux are copied
before searching. Cancellation stops the entire request, including an optional retry.

### Timer callbacks

A callback receives the **actual `Solver` instance** performing the search:

```python
import time
from astrometry_net_engine import Solver


def timer_callback(solver: Solver) -> int:
    solver.last_check = time.monotonic()
    return 0 if solver.cancel_requested else 1


with Solver("/path/to/indexes", timer_callback=timer_callback) as solver:
    solver.cancel_requested = False
    solver.last_check = None
    result = solver.solve(xy, min_scale=0.8, max_scale=1.2)
    print(solver.last_check, result.canceled)
```

You can attach ordinary Python attributes to the solver to hold application state. Callback changes persist after
`solve()` returns. Your application can set the cancellation flag while the search runs.

Return zero to cancel the entire request, including its optional retry. A positive integer gives the interval in seconds
before the next callback. The first check occurs after roughly two seconds, so a quick solution may finish without a
callback. Exceptions, including `KeyboardInterrupt`, propagate with their tracebacks after native cleanup. The solver
can be used again after cancellation or an exception.

Callbacks run in the solving thread. They may read and modify Python attributes, but must not call `solve()`, `close()`,
or create another solver: reentrant solver operations raise `RuntimeError`. Avoid waiting for another thread that needs
access to a solver.

### Threads and logging

Solver operations are serialized across all instances because the native engine shares state. For parallel searches, use
separate processes. Configure standard Python `logging` to receive progress messages.

## Index files

Obtain indexes manually from <https://data.astrometry.net/> and pass their directories to `Solver`. The package does not
download indexes, and no index files are included in its distributions.

Choose indexes whose quad sizes cover roughly 10–100% of your field size, with the right sky coverage. The 4100 series
uses Tycho-2 and suits relatively wide fields; narrower fields generally need finer indexes. Consult the index download
page recommendations for other catalogs and scales. You can load several index directories into one solver.

## Python and platforms

Binary builds target Linux x86_64/aarch64, macOS universal2 (Intel and Apple Silicon in one wheel), and Windows
AMD64/ARM64. macOS wheels target macOS 11 or newer. Windows builds use MSVC and run natively, including on Windows on
ARM.

Regular CPython 3.12 and newer uses the `abi3` stable ABI. Free-threaded CPython 3.14 and 3.15 use separate,
version-specific `cp314-cp314t` and `cp315-cp315t` wheels. The 3.15 wheels use the full CPython API as a temporary
alternative to the free-threaded stable ABI; [SWIG support for `abi3t`](https://github.com/swig/swig/issues/3550)
remains pending.

Python 3.15 requires NumPy 2.5.5 and Astropy 8.0.3 or newer. On 3.15t, pip may need to build Astropy and PyERFA from
source when matching dependency wheels are unavailable; install the [source build tools](#build-from-source) first.

Some Astropy/PyERFA builds enable the GIL when imported by free-threaded Python. The [bundled
tests](#development-and-tests) exercise the solver with `python -X gil=0`; use compatible dependency builds if you need
imports to leave the GIL disabled automatically.

## Development and tests

Wheels include the test suite and its small reference-field data in `astrometry_net_engine/tests`. To test an installed
distribution, install a compatible wheel and use `--pyargs` to select its bundled tests:

```sh
python -m pip install dist/astrometry_net_engine-*.whl pytest
AN_ENGINE_DOWNLOAD_INDEXES=1 python -I -m pytest --import-mode=importlib --pyargs astrometry_net_engine.tests
```

In PowerShell, set `$env:AN_ENGINE_DOWNLOAD_INDEXES = "1"` before running pytest.

For local development, `python -m pytest tests` can run against the source checkout when a compatible compiled extension
is present in `astrometry_net_engine/`; a missing or incompatible extension prevents the tests from running.

On free-threaded Python 3.14 or 3.15, add `-X gil=0` after `-I` to exercise the solver with the GIL disabled.

Tests need `index-4110.fits` from `https://data.astrometry.net/4100/`. They download it only when explicitly enabled and
cache it in `.test-indexes` under the current directory for installed tests, or the project root for source tests.
Set `AN_ENGINE_INDEX_CACHE` to reuse another directory. Without opt-in, index-dependent tests skip when the file is
missing; CI enables downloads and treats download failures as errors. Existing cached index files are preserved.
No FITS indexes are distributed with the package, and the runtime library never downloads them.

The suite covers deterministic nonexistent fields, wrong scales with eligible indexes, native position rejection,
immutable attempt/match snapshots, result lifetime after reuse and close, positional options, quad fractions, native
option defaults and retries, WCS accuracy, SIP fitting, unrestricted retry, reuse, independent instances, invalid
inputs, cleanup, and serialization across concurrent callers. Timer tests pass the actual `Solver` to application
callbacks and verify persistent attribute changes, cancellation, exception identity and tracebacks, reentry rejection,
and callback-reference lifetime. Deliberately unmatched sources keep the native search running long enough to reach its
timer; quick solutions may finish without a callback.

## License and acknowledgments

astrometry-net-engine is distributed under [**GNU GPL v3 (`GPL-3.0-only`)**](LICENSE), which is required by the included
Astrometry.net/GSL code and the GPL-covered portability patch. Individual components retain their original terms and
copyright notices. Each binary release is accompanied by a matching source distribution containing the upstream sources,
patches, interfaces, generated wrappers, and build helpers.

The Astrometry.net team develops the native engine. The package author (Vladimir Kouprianov) developed the high-level
Python API, SWIG wrapper, test suite, and setuptools-based extension build helper. MSVC portability work was adapted,
with modifications, from the [`claude/windows-msvc-compat`
branch](https://github.com/arcsecond-io/astrometry/blob/claude/windows-msvc-compat/astrometry.net.patch) of the
[`arcsecond-astrometry` package](https://github.com/arcsecond-io/astrometry), a fork of
[`astrometry`](https://github.com/neuromorphicsystems/astrometry) by [Alexandre
Marcireau](https://github.com/aMarcireau), distributed under the same GNU GPL v3 license.

The field-solving regression test suite contains coordinate data for a real stellar field obtained by processing an
image owned by the package author.

### Astrometry.net and bundled components

The native engine comes from [Astrometry.net](https://github.com/dstndstn/astrometry.net), developed by the
Astrometry.net team.

Much of the engine and its libkd utilities is BSD 3-Clause licensed. The original copyright, conditions, and disclaimer
are reproduced in `licenses/ASTROMETRY-LICENSE`. The distinct University of California notices for the sorting and
FreeBSD-derived bit-search code, and the BSD notices for qfits additions, are reproduced verbatim in
`licenses/BSD-NOTICES`. These files accompany binary wheels and source distributions.

The linked numerical code includes GNU Scientific Library (`gsl-an`), copyright the GSL contributors named in its source
headers. Its GPL terms permit distribution of the combined work under GPL v3. The bundled qfits code includes copyright
(C) 2001–2004 European Southern Observatory, with contributions by Nicolas Devillard and Astrometry.net (including
copyright 2007–2013 Dustin Lang). The original ESO code and GPL-covered additions permit GPL v3 through their version
2-or-later terms; BSD additions retain their original terms. The qfits MD5 implementation credits Colin Plumb and is
identified in its source as public domain.

### Python interface and portability patches

Python interface, original tests, build adaptations, and package-specific changes:
Copyright (c) 2010–2026 Vladimir Kouprianov (<v.kouprianov@gmail.com>).

The Python interface and build support were adapted from the author’s FORTE Telescope Control System code. The original
test coordinates come from the author-owned image described above. These contributions are distributed under
GPL-3.0-only, with their legitimate copyright notices preserved.

`astrometry.net.msvc.patch` is a modified derivative of the Windows/MSVC patch from `arcsecond-astrometry`, credited
above and distributed under its [GNU GPL v3 license][msvc-license]. The original upstream and Astrometry.net notices
remain applicable; this package’s adaptations and the combined distribution use GPL-3.0-only. The portability patch is
included in the matching source distribution.

The GPL text and the original BSD copyright notices, redistribution conditions, and disclaimers in `licenses/` accompany
both binary and source distributions. This README accompanies wheels as a license/attribution document, as well as
appearing in package metadata. The complete corresponding source preserves the original per-file notices.

SWIG generates the wrappers. Its [licensing policy](https://www.swig.org/legal.html) permits generated wrappers to be
distributed under the license of the wrapped project; SWIG is not a runtime dependency.

[msvc-license]:
  https://github.com/arcsecond-io/astrometry/blob/claude/windows-msvc-compat/LICENSE

### External indexes

Test indexes are downloaded separately and are not redistributed in this package. Consult the index provider's terms
and the specific catalog notices when obtaining indexes.

## Citation

If this package contributes to your research, please cite **astrometry-net-engine** using [CITATION.cff](CITATION.cff)
and the original engine paper:

Lang, D., Hogg, D. W., Mierle, K., Blanton, M., & Roweis, S. (2010). _Astrometry.net: Blind astrometric calibration of
arbitrary astronomical images_. The Astronomical Journal, 139, 1782–1800.
[doi:10.1088/0004-6256/139/5/1782](https://doi.org/10.1088/0004-6256/139/5/1782).

This is a scholarly request, not an additional license condition.
