# Copyright (c) 2010-2026 Vladimir Kouprianov (v.kouprianov@gmail.com)
# SPDX-License-Identifier: GPL-3.0-only
"""Configure and build the bundled Astrometry.net solver with setuptools."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path
from typing import NamedTuple

from setuptools import Extension, setup
from setuptools.command.bdist_wheel import bdist_wheel
from setuptools.command.build_ext import build_ext
from setuptools.command.egg_info import egg_info, manifest_maker


class BuildExt(build_ext):
    """Patch and compile a private source copy, optionally regenerating SWIG wrappers."""

    def build_extensions(self) -> None:
        """Compile extensions while keeping the checkout unchanged, including on failure."""
        source_root = Path(__file__).resolve().parent
        Path(self.build_temp).mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="anet-", dir=Path(self.build_temp).resolve()) as directory:
            staging = Path(directory)
            # Upstream's astrometry -> . and doc/net directory links form cycles; source header links are safe.
            shutil.copytree(
                source_root / "astrometry.net",
                staging / "astrometry.net",
                ignore=lambda directory, names: [
                    name
                    for name in names
                    if name == ".git" or ((Path(directory) / name).is_symlink() and (Path(directory) / name).is_dir())
                ],
            )
            shutil.copytree(
                source_root / "astrometry_net_engine",
                staging / "astrometry_net_engine",
                ignore=shutil.ignore_patterns("__pycache__", "*.so", "*.pyd"),
            )
            upstream = staging / "astrometry.net"
            _apply_patch(upstream, source_root / "astrometry.net.patch")
            if self.compiler.compiler_type == "msvc":
                _apply_patch(upstream, source_root / "astrometry.net.msvc.patch")

            # Regenerate only in staging; official builds must have the pinned SWIG available.
            swig = shutil.which("swig")
            if os.environ.get("AN_ENGINE_SKIP_SWIG") == "1":
                if os.environ.get("CIBUILDWHEEL") == "1":
                    raise RuntimeError("Official wheels must regenerate their SWIG wrappers")
                swig = None
            if swig:
                version = subprocess.check_output([swig, "-version"], text=True)
                match = re.search(r"SWIG Version (\d+)\.(\d+)\.(\d+)", version)
                if not match or tuple(map(int, match.groups())) < (4, 4, 0):
                    raise RuntimeError("SWIG 4.4.0 or newer is required when regenerating wrappers")
                package = staging / "astrometry_net_engine"
                subprocess.run(
                    [
                        swig,
                        "-python",
                        "-threads",
                        "-nogil",
                        f"-I{upstream / 'include'}",
                        "-outdir",
                        str(package),
                        "-o",
                        str(package / "an_engine_wrap.c"),
                        str(package / "an_engine.i"),
                    ],
                    check=True,
                )
                shutil.copy2(package / "an_engine.py", Path(self.build_lib) / "astrometry_net_engine")
            elif os.environ.get("CIBUILDWHEEL") == "1":
                raise RuntimeError("SWIG is required for official wheel builds")

            for ext in self.extensions:
                ext.sources = [str(staging / path) for path in ext.sources]
                ext.include_dirs = [str(staging / path) for path in ext.include_dirs]

            # MSVC's command line has a 32K limit; use a response file for the large link command.
            original_spawn = self.compiler.spawn

            def spawn(command: list[str], **kwargs: object) -> None:
                """Run compiler command with its keyword options, using a response file if needed."""
                if self.compiler.compiler_type != "msvc" or sum(len(arg) + 1 for arg in command) < 30000:
                    original_spawn(command, **kwargs)
                    return
                response = staging / "compiler.rsp"
                response.write_text(
                    subprocess.list2cmdline([arg.replace("\\", "/") for arg in command[1:]]), encoding="utf-8"
                )
                original_spawn([command[0], f"@{response.as_posix()}"], **kwargs)

            self.compiler.spawn = spawn
            try:
                super().build_extensions()
            finally:
                self.compiler.spawn = original_spawn


class BdistWheel(bdist_wheel):
    """Set stable ABI wheel tags only for binaries compiled with the Limited API."""

    def finalize_options(self) -> None:
        """Set the CPython 3.12 floor for regular Limited API builds."""
        super().finalize_options()
        if all(ext.py_limited_api for ext in self.distribution.ext_modules or []):
            self.py_limited_api = "cp312"

    def get_tag(self) -> tuple[str, str, str]:
        """Match macOS wheel architecture to the compiler's ARCHFLAGS, including universal Python hosts."""
        python, abi, platform_tag = super().get_tag()
        if sys.platform == "darwin":
            architectures = set(re.findall(r"-arch\s+(\S+)", os.environ.get("ARCHFLAGS", "")))
            match sorted(architectures):
                case ["arm64", "x86_64"]:
                    architecture = "universal2"
                case ["x86_64"]:
                    architecture = "x86_64"
                case ["arm64"]:
                    architecture = "arm64"
                case _:
                    raise RuntimeError(f"Unsupported macOS ARCHFLAGS: {os.environ.get('ARCHFLAGS')}")
            platform_tag = re.sub(r"_(universal2|x86_64|arm64)$", "_" + architecture, platform_tag)
        return python, abi, platform_tag


class SourceManifest(manifest_maker):
    """Discover source files without following upstream cycles or local environment links."""

    ignore_egg_info_dir: bool

    def add_defaults(self) -> None:
        """Populate the source candidate list before applying setuptools defaults and MANIFEST.in."""
        candidates: list[str] = []
        for directory, directories, filenames in os.walk(".", followlinks=False):
            directories[:] = [
                name
                for name in directories
                if (not name.startswith(".") or name == ".github")
                and name not in {"build", "dist", "wheelhouse", "__pycache__"}
            ]
            candidates.extend(str(Path(directory) / name) for name in filenames)
        self.filelist.allfiles = candidates
        super().add_defaults()
        # Old SOURCES.txt files may contain paths reached through a now-excluded directory symlink.
        available = set(candidates)
        self.filelist.files = [name for name in self.filelist.files if name in available]


class EggInfo(egg_info):
    """Use source discovery that cannot traverse out of the checkout through local symlinks."""

    ignore_egg_info_in_manifest: bool

    def find_sources(self) -> None:
        """Generate the source manifest using SourceManifest's bounded traversal."""
        manifest = SourceManifest(self.distribution)
        manifest.ignore_egg_info_dir = self.ignore_egg_info_in_manifest
        manifest.manifest = str(Path(self.egg_info) / "SOURCES.txt")
        manifest.run()
        self.filelist = manifest.filelist


def _path(source_base: Path, relpath: str | Path) -> str:
    """Return a build source path relative to source_base."""
    return str(source_base / relpath)


def _glob_paths(source_base: Path, pattern: str) -> list[str]:
    """Return sorted source files matching pattern under source_base."""
    return [str(path) for path in sorted(source_base.glob(pattern)) if path.is_file()]


def _glob_paths_excluding(source_base: Path, pattern: str, excluded_names: set[str]) -> list[str]:
    """Return matching source files other than excluded_names under source_base."""
    return [path for path in _glob_paths(source_base, pattern) if Path(path).name not in excluded_names]


class _PatchHunk(NamedTuple):
    """A unified-diff hunk with source and destination ranges and marked lines."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[tuple[bytes, bytes]]


class _FilePatch(NamedTuple):
    """A file-level patch whose absent path represents creation or deletion."""

    old_path: Path | None
    new_path: Path | None
    hunks: list[_PatchHunk]


def _decode_patch_line(line: bytes) -> str:
    """Decode a patch line without its platform-specific newline."""
    return line.decode("utf-8").rstrip("\r\n")


def _parse_patch_path(raw: bytes) -> Path | None:
    """Parse raw patch-header bytes, rejecting paths outside the build copy."""
    value = _decode_patch_line(raw).split("\t", 1)[0].split(" ", 1)[0]
    if value == "/dev/null":
        return None
    if value.startswith(("a/", "b/")):
        value = value[2:]
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise RuntimeError(f"Unsafe path in patch: {value}")
    return path


def _parse_unified_patch(patch_file: Path) -> list[_FilePatch]:
    """Read patch_file into validated file/hunk records without invoking Git."""
    lines = patch_file.read_bytes().splitlines(keepends=True)
    patches: list[_FilePatch] = []
    index = 0
    while index < len(lines):
        if not lines[index].startswith(b"diff --git "):
            index += 1
            continue

        index += 1
        while index < len(lines) and not lines[index].startswith(b"--- "):
            index += 1
        if index >= len(lines):
            raise RuntimeError(f"Missing old file header in {patch_file.name}")
        old_path = _parse_patch_path(lines[index][4:])
        index += 1
        if index >= len(lines) or not lines[index].startswith(b"+++ "):
            raise RuntimeError(f"Missing new file header in {patch_file.name}")
        new_path = _parse_patch_path(lines[index][4:])
        index += 1

        hunks: list[_PatchHunk] = []
        while index < len(lines) and not lines[index].startswith(b"diff --git "):
            if not lines[index].startswith(b"@@ "):
                index += 1
                continue

            text = _decode_patch_line(lines[index])
            try:
                old_range, new_range = text.split(" ", 3)[1:3]
            except ValueError as exc:
                raise RuntimeError(f"Malformed hunk header in {patch_file.name}: {text}") from exc

            def parse_range(value: str, prefix: str) -> tuple[int, int]:
                """Parse a hunk range value beginning with the expected prefix."""
                if not value.startswith(prefix):
                    raise RuntimeError(f"Malformed hunk range in {patch_file.name}: {text}")  # noqa: B023
                start, separator, count = value[1:].partition(",")
                return int(start), int(count) if separator else 1

            old_start, old_count = parse_range(old_range, "-")
            new_start, new_count = parse_range(new_range, "+")
            index += 1
            hunk_lines: list[tuple[bytes, bytes]] = []
            while (
                index < len(lines)
                and not lines[index].startswith(b"@@ ")
                and not lines[index].startswith(b"diff --git ")
            ):
                line = lines[index]
                if line.startswith((b" ", b"+", b"-")):
                    hunk_lines.append((line[:1], line[1:]))
                elif line.startswith(b"\\ No newline at end of file"):
                    if hunk_lines:
                        sign, previous = hunk_lines[-1]
                        hunk_lines[-1] = (sign, previous.rstrip(b"\r\n"))
                elif line.strip():
                    raise RuntimeError(f"Unsupported patch line in {patch_file.name}: {_decode_patch_line(line)}")
                index += 1

            hunks.append(_PatchHunk(old_start, old_count, new_start, new_count, hunk_lines))
        patches.append(_FilePatch(old_path, new_path, hunks))
    return patches


def _apply_file_patch(target_dir: Path, patch: _FilePatch, patch_file: Path) -> tuple[Path, bytes | None]:
    """Validate patch against target_dir and produce replacement bytes; patch_file identifies errors."""
    relpath = patch.new_path or patch.old_path
    if relpath is None:
        raise RuntimeError(f"Patch {patch_file.name} has no target path")
    path = target_dir / relpath
    if patch.old_path is None and path.exists():
        raise RuntimeError(f"Patch {patch_file.name} would overwrite existing file {relpath}")
    original = b"" if patch.old_path is None else path.read_bytes()
    original_lines = original.splitlines(keepends=True)
    result: list[bytes] = []
    position = 0

    def line_matches(actual: bytes, expected: bytes) -> bool:
        """Compare actual and expected content while tolerating CRLF/LF differences."""
        return actual == expected or actual.rstrip(b"\r\n") == expected.rstrip(b"\r\n")

    for hunk in patch.hunks:
        hunk_position = max(hunk.old_start - 1, 0)
        if hunk_position < position or hunk_position > len(original_lines):
            raise RuntimeError(f"Patch {patch_file.name} does not match {relpath}")
        result.extend(original_lines[position:hunk_position])
        position = hunk_position

        old_seen = 0
        new_seen = 0
        for sign, content in hunk.lines:
            if sign == b" ":
                if position >= len(original_lines) or not line_matches(original_lines[position], content):
                    raise RuntimeError(f"Patch {patch_file.name} does not match {relpath}")
                result.append(original_lines[position])
                position += 1
                old_seen += 1
                new_seen += 1
            elif sign == b"-":
                if position >= len(original_lines) or not line_matches(original_lines[position], content):
                    raise RuntimeError(f"Patch {patch_file.name} does not match {relpath}")
                position += 1
                old_seen += 1
            elif sign == b"+":
                result.append(content)
                new_seen += 1

        if old_seen != hunk.old_count or new_seen != hunk.new_count:
            raise RuntimeError(f"Patch {patch_file.name} hunk counts do not match {relpath}")

    result.extend(original_lines[position:])
    if patch.new_path is None:
        return path, None
    return path, b"".join(result)


def _apply_patch(target_dir: Path, patch_file: Path) -> None:
    """Apply patch_file to the private target_dir, requiring exact hunk positions and content."""
    for patch in _parse_unified_patch(patch_file):
        path, content = _apply_file_patch(target_dir, patch, patch_file)
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)


def extension() -> Extension:
    """Describe the extension without patching sources or running build tools."""
    if sys.implementation.name != "cpython" or sys.version_info < (3, 12):
        raise RuntimeError("astrometry-net-engine requires CPython 3.12 or newer")
    free_threaded = sysconfig.get_config_var("Py_GIL_DISABLED") in {1, "1"}
    requested_abi = os.environ.get("AN_ENGINE_ABI", "auto")
    if requested_abi not in {"auto", "abi3", "cpython", "abi3t"}:
        raise RuntimeError(f"Unknown AN_ENGINE_ABI: {requested_abi}")
    if requested_abi == "abi3t":
        raise RuntimeError("abi3t is temporarily unsupported by SWIG runtime; refusing an incorrectly tagged binary.")
    if free_threaded and requested_abi == "abi3":
        raise RuntimeError("abi3 cannot be used with a free-threaded interpreter")
    limited = not free_threaded and requested_abi != "cpython"
    macros: list[tuple[str, str | None]] = []
    if sys.platform == "darwin":
        # Match cibuildwheel's universal2 target in local builds; retain explicit developer ARCHFLAGS overrides.
        os.environ.setdefault("ARCHFLAGS", "-arch x86_64 -arch arm64")
        os.environ.setdefault("MACOSX_DEPLOYMENT_TARGET", "11.0")
    if limited:
        macros.append(("Py_LIMITED_API", "0x030C0000"))
    if sys.platform == "win32":
        macros += [("_USE_MATH_DEFINES", "1"), ("_CRT_SECURE_NO_WARNINGS", "1"), ("_CRT_NONSTDC_NO_WARNINGS", "1")]
        if free_threaded:
            macros.append(("Py_GIL_DISABLED", "1"))
    elif sys.platform.startswith("linux") or "bsd" in sys.platform:
        macros.append(("_GNU_SOURCE", None))
    source_base = Path(".")
    anet = "astrometry.net/"
    gsl = anet + "gsl-an/"
    return Extension(
        name="astrometry_net_engine._an_engine",
        py_limited_api=limited,
        sources=["astrometry_net_engine/an_engine_wrap.c"]
        + [
            _path(source_base, anet + f"util/{filename}.c")
            for filename in (
                "an-endian",
                "bl",
                "codekd",
                "datalog",
                "errors",
                "fit-wcs",
                "fitsbin",
                "fitsfile",
                "fitsioutils",
                "fitstable",
                "gslutils",
                "healpix",
                "index",
                "ioutils",
                "log",
                "matchobj",
                "mathutil",
                "permutedsort",
                "quadfile",
                "sip",
                "sip-utils",
                "starkd",
                "starutil",
                "starxy",
                "tic",
            )
        ]
        + [
            _path(source_base, anet + f"solver/{filename}.c")
            for filename in ("quad-utils", "solver", "tweak", "tweak2", "verify")
        ]
        + _glob_paths(source_base, gsl + "blas/*")
        + [
            _path(source_base, gsl + f"linalg/{filename}.c")
            for filename in ("bidiag", "cholesky", "householder", "lu", "qr", "svd")
        ]
        + [
            _path(source_base, gsl + f"multiroots/{filename}.c")
            for filename in (
                "broyden",
                "convergence",
                "dnewton",
                "fdfsolver",
                "fdjac",
                "fsolver",
                "gnewton",
                "hybrid",
                "hybridj",
                "newton",
            )
        ]
        + _glob_paths(source_base, gsl + "sys/*.c")
        + _glob_paths_excluding(source_base, gsl + "cblas/*.c", {"hypot.c"})
        + _glob_paths(source_base, gsl + "err/*.c")
        + [_path(source_base, gsl + f"block/{filename}.c") for filename in ("block", "init")]
        + [
            _path(source_base, gsl + f"matrix/{filename}.c")
            for filename in ("copy", "init", "matrix", "rowcol", "submatrix", "swap", "view")
        ]
        + [
            _path(source_base, gsl + f"vector/{filename}.c")
            for filename in ("copy", "init", "oper", "prop", "subvector", "swap", "vector")
        ]
        + [_path(source_base, gsl + f"permutation/{filename}.c") for filename in ("init", "permutation", "permute")]
        + [
            _path(source_base, anet + f"libkd/{filename}.c")
            for filename in (
                "dualtree",
                "dualtree_nearestneighbour",
                "dualtree_rangesearch",
                "kdint_ddd",
                "kdint_dds",
                "kdint_ddu",
                "kdint_dss",
                "kdint_duu",
                "kdint_fff",
                "kdint_lll",
                "kdtree",
                "kdtree_dim",
                "kdtree_fits_io",
                "kdtree_mem",
            )
        ]
        + _glob_paths(source_base, anet + "qfits-an/*.c"),
        include_dirs=[]
        + [
            _path(source_base, anet + suffix)
            for suffix in ("include", "include/astrometry", "gsl-an", "libkd", "qfits-an", "util")
        ]
        + [_path(source_base, gsl + suffix) for suffix in ("", "cblas")],
        extra_compile_args=[],
        extra_link_args=[],
        define_macros=macros,
        libraries=["ws2_32"] if sys.platform == "win32" else [],
    )


setup(ext_modules=[extension()], cmdclass={"build_ext": BuildExt, "bdist_wheel": BdistWheel, "egg_info": EggInfo})
