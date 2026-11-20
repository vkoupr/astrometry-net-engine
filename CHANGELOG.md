# Release notes

## 1.0.0

Initial standalone release: reusable coordinate-to-WCS solvers, optional SIP refinement, constrained and unrestricted
search with optional sky coordinates and native parity conventions, structured results with nested match snapshots and
configurable verification and quad sizes, Python callbacks receiving the current solver, explicit resource management,
and native build support for Linux, macOS, and Windows/MSVC, both x86_64 and arm64. macOS wheels use universal2 binaries
for Intel and Apple Silicon, targeting macOS 11 or newer. Free-threaded CPython 3.14 and 3.15 use separate
version-specific wheels; the 3.15 build provides temporary support while stable `abi3t` support is pending in SWIG.
Installed packages include the regression tests and reference-field data for post-installation validation.
