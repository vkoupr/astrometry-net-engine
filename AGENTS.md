# Project guidance

## Overview and file map

This independent Python interface solves astrometric fields from pre-extracted XY source coordinates and returns
Astropy WCS solutions, optionally with SIP distortion. It does not extract sources from images.
See [README.md](README.md) for installation, public API semantics, examples, and the complete result reference.

- `astrometry_net_engine/__init__.py`: typed high-level API, index selection, locking, WCS construction, and snapshots.
- `astrometry_net_engine/an_engine.i`: SWIG interface, buffer typemaps, and native callback bridge;
  `an_engine_wrap.c` and `an_engine.py` are the checked-in generated bindings. `an_engine.pyi` describes the native
  subset used by the Python API; `py.typed` marks the installed package as typed.
- `astrometry.net/`: pinned Git submodule of the independently maintained Astrometry.net project. Its headers and
  implementation are authoritative for native behavior; this repository does not develop upstream in place.
- `astrometry.net.patch`, `astrometry.net.msvc.patch`: general and MSVC adaptations applied during builds.
- `setup.py`: extension sources, ABI selection, private build staging, patch application, and setuptools commands.
  `pyproject.toml` owns metadata, dependencies, tool settings, and shared release ABIs;
  `MANIFEST.in` selects sdist files.
- `tests/`: original real-field data, fixed synthetic distractors, solver/metadata tests, callbacks, and concurrency.
  `tests/conftest.py` requires an importable native extension and manages the optional index cache.
  Setuptools maps this directory to `astrometry_net_engine.tests` in distributions, including its small JSON data.
- `.github/workflows/build-distributions.yml`: release pipeline;
  `.github/scripts/check_distributions.py`: distribution contents, metadata, architecture, and release-matrix checks.

## Coding and API conventions

- Make focused changes and preserve manual edits. Avoid unrelated refactoring, new frameworks, redundant configuration
  APIs, and unnecessary dependencies. Do not modify the original FORTE project.
- Use modern Python, type annotations, informative docstrings, standard `logging`, and 120-column lines. Follow the
  configured Ruff and Pyright checks. Keep native terminology in diagnostics where it helps comparison with headers.
- Preserve public semantics, parameter order, and defaults unless explicitly asked to change them. Use the pinned
  native implementation to resolve unclear meanings rather than copying another wrapper's assumptions.
- Use NumPy for Python array handling and WCS construction. The extension uses Python's buffer protocol, not NumPy's
  C API; retain owned contiguous input copies while native code runs without Python's thread state.
- Keep `Solver` reusable, with explicit ownership, context-manager support, idempotent `close()`, and safe cleanup.
  Results use frozen `SolveResult`, `SolveSettings`, `SolverState`, and nested `MatchObj` metadata, with an
  independently editable WCS. Do not retain input arrays in settings or substitute live native objects for snapshots.
- Preserve optimized scale/sky index preselection and ordering by larger index scale, then angular distance.
  Fluxes, when supplied, always sort coordinates and flux together before selecting the brightest `max_sources`.
  An explicit limit reduces the allocated native field; the native quad search also has its own 1000-source cap.

## Native safety, threading, and results

Astrometry.net has process-global logging, error, and FITS state and is not safely reentrant. Preserve the process-wide
`_engine_lock` (`RLock`) around all public native operations, including allocation, index loading, snapshots, and
cleanup. The GIL is not a mutex for this engine; free-threaded operation must remain correct. Direct use of the private
SWIG bindings bypasses these guarantees. Subinterpreters are unsupported.

- The public timer receives the current `Solver`; a per-call Python adapter supplies `self` to it while the internal
  Python callable invoked by the native bridge remains parameterless. Do not add global mutable callback state.
- The native bridge detaches Python's thread state for search and attaches it for Python callbacks. Keep callback
  references alive through the call, restore native callback/userdata fields afterward, and preserve exception identity
  and tracebacks through cleanup. Zero cancels; a positive integer schedules the next check in seconds.
- Callbacks may mutate ordinary solver attributes. Reentrant solver operations raise `RuntimeError`; preserve the
  thread-local guard and avoid waiting inside callbacks for another thread that needs the engine lock.
- Snapshot every completed attempt before `solver_cleanup_field()`, under the engine lock: cleanup resets counters and
  match state. Copy scalars, strings, and arrays into owned Python values; no pointers, borrowed buffers, callbacks,
  or SWIG proxies may escape in results. Copy WCS/SIP coefficients before cleanup as well.
- Reset relevant options on every solve and counters on every attempt. Upstream `solver_reset_counters()` omits
  `num_meanx_skipped` and `timeused`; Python resets both explicitly, and the bridge refreshes elapsed CPU time even
  after an unsuccessful search. Preserve this behavior.
- Requested `SolveSettings` remain unchanged by retries. Preserve chronological attempt snapshots with effective
  settings. An unrestricted retry clears position/parity only; cancellation stops retries. Callback exceptions
  propagate after cleanup, and the instance remains reusable.
- Position requires both coordinates; otherwise call `solver_clear_radec()` and skip angular index filtering. That
  function clears only `use_radec`; inactive `centerxyz` and `r2` may retain prior values.
  Native parity is `False` normal, `True` flipped, `None` both. Do not repeat an already unrestricted search.

## Builds and binary compatibility

Setuptools with `setuptools.build_meta` is the sole backend. Keep programmatic extension configuration in `setup.py`
and sdist inclusion rules in `MANIFEST.in`; wheel package data is configured separately in `pyproject.toml`.

- Sdists must contain all required upstream C sources/headers, license notices, patches, SWIG interfaces, generated
  files, and build helpers. Building them must require neither Git, network access, nor submodule initialization.
- Patching uses the Python patch applier on a temporary source copy. Never patch or regenerate files in the upstream
  checkout. Preserve exact patch checks, bounded source discovery around upstream symlink cycles, MSVC response files,
  Windows ARM64 toolchain selection, and macOS architecture/tag handling.
- SWIG is optional for ordinary builds: use the included C wrapper/proxy if absent; if detected, regeneration failure
  must stop the build. `AN_ENGINE_SKIP_SWIG=1` selects included files explicitly; official cibuildwheel builds reject
  that override and require regeneration using the version selected in `pyproject.toml`.
- Changes to the interface or pinned headers require checking/regenerating the checked-in C wrapper and Python proxy
  and updating the handwritten `.pyi`. Build-time regeneration occurs only in staging, so it does not update repository
  files for you. Keep `an_engine.pyi` and `py.typed` in both wheels and sdists.
- Regular CPython uses `cp312-abi3` (3.12+), `Py_LIMITED_API=0x030C0000`, and SWIG proxy mode without `-builtin`,
  `-fastproxy`, or `-O`. Free-threaded 3.14 and 3.15 use version-specific `cp314-cp314t` and `cp315-cp315t` with the
  full C API, without `Py_LIMITED_API` or `Py_TARGET_ABI3T`. Preserve the Windows `Py_GIL_DISABLED` definition and SWIG
  threading options.
- Tags must match the compiled ABI and binary architecture. Validate actual binaries; setting flags or changing tags
  does not establish compatibility. Windows builds use MSVC, including ARM64 support.
- Prefer universal2 for macOS distribution. Local builds default to `-arch x86_64 -arch arm64` and a macOS 11 deployment
  target. Explicit `ARCHFLAGS` and `MACOSX_DEPLOYMENT_TARGET` overrides remain possible. Check compiler/linker flags,
  tags, and both binary slices whenever native builds change. Release validation requires universal2 even if a local
  build requests one arch.

Temporary limitation: `abi3t` is disabled pending [upstream SWIG support](https://github.com/swig/swig/issues/3550).
`AN_ENGINE_ABI=abi3t` currently fails explicitly. Before enabling it, validate generated runtime code and the actual
Python C ABI; never relabel an incompatible binary. Disabled targets do not block releases of supported targets.
The `cp315t` and disabled `abi3t` entries in `[tool.astrometry-net-engine.release.targets]` remain separate. After real
stable-ABI support is implemented and validated, replace `cp315t` with `abi3t` in the active `release.abis` list; the
interpreter selector stays `cp315t-*`. Leave regular `abi3` and `cp314t` settings unchanged.

## Upstream update policy

**Update Astrometry.net only when explicitly requested by the maintainer for a dedicated compatibility review.**
Never advance the submodule automatically or because a newer commit exists. The revision is pinned in Git and checked
against a literal commit in `.github/workflows/build-distributions.yml`; do not introduce a separate revision file.

For an authorized update:

1. Inspect upstream changes to sources, headers, and relevant native APIs.
2. Update the submodule to the selected revision.
3. Verify/revise both patches, especially Windows/MSVC portability, without silently dropping hunks.
4. Confirm required patches apply cleanly for every supported platform.
5. Review source selection and platform build configuration for changed upstream files or requirements.
6. Check the SWIG interface, regenerate wrappers as needed, and review generated code and `.pyi` against the headers.
7. Update the workflow's pinned revision check and any other configuration identifying that revision.
8. Run relevant native builds, installed-package tests, ABI audits, and distribution checks.
9. Update README attribution, licensing, and user documentation if affected.

Report compatibility problems; do not change solver behavior or discard portability work merely to obtain a build.

## Tests and validation

Use pytest, Ruff, and Pyright. For release validation, build and install a wheel matching the test interpreter and
architecture before pytest. Local development, including VS Code, may use the source checkout if its compiled
`astrometry_net_engine._an_engine` extension imports successfully; the session checks that import. Keep CI tests
isolated against installed distributions and run their bundled tests using `--pyargs astrometry_net_engine.tests`.
The wheel metadata test skips source-tree imports; native import and free-threading checks still run in that case.
In an activated environment with development tools:

```sh
python -m build
python -m pip install --force-reinstall --no-deps dist/astrometry_net_engine-*.whl
python -I -m pytest --import-mode=importlib --pyargs astrometry_net_engine.tests
ruff check .
pyright --pythonpath python
```

- Preserve coverage of TAN/SIP accuracy, unsuccessful searches that reach native search, scale/position/parity
  constraints, reuse, callbacks/cancellation, traceback propagation, snapshots after reuse/close, and concurrent access.
  Keep flux ordering/truncation tests and the independent binary-only free-threading import check.
- Use original `tests/data/field.json` and the required 4100-series indexes identified in `tests/conftest.py`. Reuse
  `.test-indexes` or `AN_ENGINE_INDEX_CACHE`; installed tests default to the current directory's cache rather than
  writing into the package. Source tests use the project-root cache. CI points all installed test runs to the shared
  checkout cache, including cibuildwheel's temporary test environments. Download missing indexes only with
  `AN_ENGINE_DOWNLOAD_INDEXES=1` opt-in.
  Never remove cached indexes or bundle FITS indexes into distributions. Missing indexes otherwise skip those tests;
  check skips before claiming native coverage. The runtime library never downloads indexes.
- On free-threaded Python, run
  `python -I -X gil=0 -m pytest --import-mode=importlib --pyargs astrometry_net_engine.tests`.
  Some Astropy/PyERFA builds enable the GIL at import, so an ordinary test run is insufficient.
- When native/build/generated code changes, test SWIG regeneration and a no-SWIG sdist install. Use
  `AN_ENGINE_SKIP_SWIG=1` for a local fallback check; the workflow also tests a compiler-only PATH without Git or SWIG,
  installing with `--no-index --no-build-isolation --no-deps` after dependencies are available.
- For packaging changes, run the distribution validator and `python -m twine check --strict dist/*`. Inspect sdist/wheel
  contents, metadata/version consistency, typing data, license files, and native architecture tags. Run
  `python -m abi3audit --strict --summary dist/*-abi3-*.whl` for regular stable-ABI wheels. Keep this guidance and
  GitHub-only scripts out of published distributions.
- Keep `tests/` as the source of the installed test package; include its Python modules and required small data in every
  wheel and sdist. CI must test the installed copy, not `{project}/tests` or checkout-relative `tests` paths. The tests
  must run without repository configuration files and without bundling indexes.
- Test platform-specific work on that platform when available; report unavailable architectures/interpreters honestly.
  Do not turn previous local test reports into claims about other targets.

## CI and release rules

The cibuildwheel workflow creates one sdist, builds/repairs/tests wheels from it, audits regular stable ABI binaries,
tests forward compatibility on newer regular interpreters, and tests sdist installation without Git/SWIG/network.
The collected artifacts must pass distribution and metadata validation before publication.

- The active matrix is Linux x86_64/aarch64, macOS universal2, and Windows AMD64/ARM64, each with `cp312-abi3`,
  `cp314-cp314t`, and `cp315-cp315t`: 15 wheels. `[tool.astrometry-net-engine.release].abis` selects active targets;
  `release.targets` centralizes their Python versions and native ABI modes for CI and validation. CI derives each
  cibuildwheel selector from its Python version. Also keep standalone cibuildwheel selectors consistent.
- Build each macOS ABI artifact only once using cibuildwheel's universal2 support and delocate's required-architecture
  check. The `test-macos` jobs install that same artifact on native Intel and ARM64 runners, using `-X gil=0` for 3.14t
  and 3.15t.
  Both slices must pass; compilation alone and Rosetta execution do not replace native coverage. Newer regular Python
  jobs on both macOS architectures also consume the same universal2 stable-ABI artifact.
- `.github/scripts/check_distributions.py` checks contents, licensing, version/tag consistency, binary architectures,
  and, with `--release`, exactly one sdist plus the complete active wheel set. It is repository-only tooling.
- Publication must depend on every active build, installed test, ABI audit, forward-compatibility test, and no-SWIG
  source test. A cross-compiled Windows ARM64 binary needs native ARM64 execution; compilation alone is not a test.
  Preserve the native ARM64 runner coverage configured in the workflow.
- Manual execution defaults to build/test only. Publishing uses the `pypi` environment and GitHub OIDC Trusted
  Publishing, not stored API tokens. Require appropriate environment protections and restricted permissions.
- Never create a release, push tags, or upload to PyPI without explicit instruction. When targets change, update the
  shared release configuration, build selectors, validation gates, tests, and README together.

## Changelog and migration policy

- Maintain root `CHANGELOG.md` by package version, newest first. The authoritative current version is
  `astrometry_net_engine/__version__.py`; never change that version automatically.
- Add or edit entries only in the current version's section; leave previous release sections unchanged. If absent,
  create the current section at the top, below the title. Do not invent a release date for an unpublished version.
- Record significant user-visible bug fixes, API enhancements, functionality, performance, and compatibility changes.
  Omit routine refactoring, formatting, documentation/test maintenance, and internal details unless they affect users.
  Keep entries concise and human-readable, not a Git commit history.
- Identify incompatible API changes under a **Breaking changes** heading. Explain what changed and what callers
  must do. Also add concise README migration/porting instructions, with old/new usage examples where helpful.

## Licensing and documentation

The combined distribution remains `GPL-3.0-only` because it contains GPL-covered code. Preserve original source
copyrights and required binary redistribution notices, including BSD terms. Keep attribution for Astrometry.net,
GSL, qfits, and the GPL-covered MSVC patch adapted from `arcsecond-astrometry`. See [LICENSE](LICENSE),
the README's [License and acknowledgments](README.md#license-and-acknowledgments), and `licenses/` for the notices.
The README is also packaged as a license/attribution file; do not remove it from wheel license metadata.

Keep `CITATION.cff` and the README citation guidance accurate. Preserve the README's detailed API reference and update
user documentation for changes to API behavior, supported targets, installation, or licensing. Keep user documentation
free of internal agent history and validation transcripts. Follow the changelog policy above. This root file is
repository guidance, excluded from wheels and sdists; do not add nested instruction files without a demonstrated need.
