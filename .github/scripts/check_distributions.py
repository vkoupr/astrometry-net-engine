# Copyright (c) 2026 Vladimir Kouprianov (v.kouprianov@gmail.com)
# SPDX-License-Identifier: GPL-3.0-only
"""Validate distribution contents, metadata, ABI tags, and the complete release wheel set."""

from __future__ import annotations

import argparse
import ast
import email
import struct
import tarfile
import tomllib
import zipfile
from pathlib import Path

from packaging.tags import parse_tag
from packaging.utils import parse_wheel_filename


def _binary_architectures(data: bytes) -> set[str]:
    """Read machine types from ELF, PE, or Mach-O extension data without executing the binary."""
    if data.startswith(b"\x7fELF"):
        endian = "<" if data[5] == 1 else ">"
        machine = struct.unpack_from(endian + "H", data, 18)[0]
        architecture = {62: "x86_64", 183: "arm64"}[machine]
        return {architecture}
    if data.startswith(b"MZ"):
        offset = struct.unpack_from("<I", data, 0x3C)[0]
        assert data[offset : offset + 4] == b"PE\0\0", "Invalid PE binary"
        machine = struct.unpack_from("<H", data, offset + 4)[0]
        return {{0x8664: "x86_64", 0xAA64: "arm64"}[machine]}
    if data[:4] in {b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"}:
        count = struct.unpack_from(">I", data, 4)[0]
        size = 32 if data[:4] == b"\xca\xfe\xba\xbf" else 20
        machines = [struct.unpack_from(">I", data, 8 + index * size)[0] for index in range(count)]
    else:
        assert data[:4] == b"\xcf\xfa\xed\xfe", "Unrecognized 64-bit binary format"
        machines = [struct.unpack_from("<I", data, 4)[0]]
    return {{0x01000007: "x86_64", 0x0100000C: "arm64"}[machine] for machine in machines}


def main() -> None:
    """Check distributions supplied on the command line, optionally enforcing the full release matrix."""
    project_root = Path(__file__).resolve().parents[2]
    configuration = tomllib.loads((project_root / "pyproject.toml").read_text())
    release = configuration["tool"]["astrometry-net-engine"]["release"]
    targets: dict[str, dict[str, str]] = release["targets"]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--expected-abi", choices=targets)
    parser.add_argument("--release", action="store_true")
    args = parser.parse_args()
    release_abis: list[str] = release["abis"]
    # abi3t validation is retained for future activation; it is absent from the active release list.
    assert release_abis and len(set(release_abis)) == len(release_abis)
    assert set(release_abis) <= targets.keys(), release_abis
    files = list(args.directory.glob("*.whl")) + list(args.directory.glob("*.tar.gz"))
    assert files, "No distributions to check"
    seen: set[tuple[str, str]] = set()
    sdists = 0
    for path in files:
        if path.suffix == ".whl":
            name, version, _, tags = parse_wheel_filename(path.name)
            assert str(name) == "astrometry-net-engine" and str(version) == "1.0.0"
            platforms = {tag.platform for tag in tags}
            abis = {tag.abi for tag in tags}
            assert len(abis) == 1, f"Unexpected compressed ABI tag: {abis}"
            abi = next(iter(abis))
            assert abi in targets, abis
            expected_interpreter = "cp" + targets[abi]["python"].replace(".", "").removesuffix("t")
            assert {tag.interpreter for tag in tags} == {expected_interpreter}
            if args.expected_abi:
                assert abi == args.expected_abi, f"Expected {args.expected_abi}, got {abi}"
            # Repair tools may emit multiple equivalent manylinux tags; normalize to the five release platforms.
            for platform in platforms:
                if platform.startswith("macosx_"):
                    assert platform.endswith("_universal2"), f"macOS distributions must be universal2: {platform}"
                    family = "macos-universal2"
                elif platform.startswith("manylinux"):
                    family = "linux-" + ("x86_64" if platform.endswith("x86_64") else "aarch64")
                elif platform in {"win_amd64", "win_arm64"}:
                    family = platform
                else:
                    family = platform
                seen.add((family, abi))
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
                metadata_path = next(name for name in names if name.endswith(".dist-info/METADATA"))
                metadata = email.message_from_bytes(archive.read(metadata_path))
                wheel_metadata = email.message_from_bytes(
                    archive.read(metadata_path.removesuffix("METADATA") + "WHEEL")
                )
                assert {tag for value in wheel_metadata.get_all("Tag", []) for tag in parse_tag(value)} == tags
                assert metadata["Version"] == "1.0.0" and metadata["License-Expression"] == "GPL-3.0-only"
                assert metadata["Requires-Python"] == ">=3.12"
                assert all("forte" not in value.lower() for value in metadata.get_all("Requires-Dist", []))
                assert "astrometry_net_engine/__version__.py" in names
                assert archive.read("astrometry_net_engine/__version__.py").strip() == b'__version__ = "1.0.0"'
                assert "astrometry_net_engine/an_engine.py" in names
                assert "astrometry_net_engine/py.typed" in names
                assert "astrometry_net_engine/an_engine.pyi" in names
                assert {
                    "astrometry_net_engine/tests/__init__.py",
                    "astrometry_net_engine/tests/conftest.py",
                    "astrometry_net_engine/tests/test_callbacks.py",
                    "astrometry_net_engine/tests/test_solve_field.py",
                    "astrometry_net_engine/tests/data/field.json",
                } <= set(names), "Installed tests or their data are missing from the wheel"
                assert any(name.endswith((".so", ".pyd")) and "_an_engine" in name for name in names)
                native_path = next(name for name in names if name.endswith((".so", ".pyd")) and "_an_engine" in name)
                architectures = _binary_architectures(archive.read(native_path))
                for platform in platforms:
                    if platform.endswith("universal2"):
                        expected_architectures = {"x86_64", "arm64"}
                    elif platform.endswith(("arm64", "aarch64")):
                        expected_architectures = {"arm64"}
                    else:
                        expected_architectures = {"x86_64"}
                    assert architectures == expected_architectures, (platform, architectures)
                required_licenses = {"LICENSE", "README.md", "licenses/ASTROMETRY-LICENSE", "licenses/BSD-NOTICES"}
                assert set(metadata.get_all("License-File", [])) == required_licenses
                license_prefix = metadata_path.removesuffix("METADATA") + "licenses/"
                assert {license_prefix + name for name in required_licenses} <= set(names)
                readme_text = archive.read(license_prefix + "README.md").decode("utf-8")
                assert not any(name.endswith((".fits", ".i", ".c")) for name in names)
        else:
            sdists += 1
            with tarfile.open(path) as archive:
                members = archive.getmembers()
                names = {"/".join(member.name.split("/")[1:]) for member in members}
                required = {
                    "setup.py",
                    "pyproject.toml",
                    "LICENSE",
                    "README.md",
                    "licenses/ASTROMETRY-LICENSE",
                    "licenses/BSD-NOTICES",
                    "astrometry.net.patch",
                    "astrometry.net.msvc.patch",
                    "astrometry_net_engine/an_engine.i",
                    "astrometry_net_engine/an_engine_wrap.c",
                    "astrometry_net_engine/an_engine.py",
                    "astrometry_net_engine/an_engine.pyi",
                    "astrometry_net_engine/py.typed",
                    "astrometry.net/solver/solver.c",
                    "astrometry.net/include/astrometry/solver.h",
                    "astrometry.net/gsl-an/COPYING",
                    "astrometry.net/LICENSE",
                    "tests/__init__.py",
                    "tests/conftest.py",
                    "tests/test_callbacks.py",
                    "tests/test_solve_field.py",
                    "tests/data/field.json",
                }
                assert required <= names, f"Missing sdist files: {required - names}"
                # Some portability hunks cover supporting sources beyond the extension's compiled file list.
                for patch_name in ("astrometry.net.patch", "astrometry.net.msvc.patch"):
                    patch_member = next(member for member in members if member.name.endswith("/" + patch_name))
                    patch_stream = archive.extractfile(patch_member)
                    assert patch_stream is not None
                    patched_sources = {
                        "astrometry.net/" + line[6:].split()[0]
                        for line in patch_stream.read().decode().splitlines()
                        if line.startswith("--- a/")
                    }
                    assert patched_sources <= names, f"Missing portability-patch inputs: {patched_sources - names}"
                assert not any(name.endswith((".fits", ".so", ".pyd", ".pyc")) for name in names)
                assert not any(".git" in Path(name).parts for name in names)
                assert not any(".github" in Path(name).parts or "tools" in Path(name).parts for name in names)
                assert not any("astrometry.net/astrometry/" in name for name in names), "Recursive upstream link copied"
                version_member = next(member for member in members if member.name.endswith("/__version__.py"))
                stream = archive.extractfile(version_member)
                assert stream is not None
                node = ast.parse(stream.read())
                assert len(node.body) == 1 and isinstance(node.body[0], ast.Assign)
                assert ast.literal_eval(node.body[0].value) == "1.0.0"
                readme_member = next(member for member in members if member.name.split("/", 1)[-1] == "README.md")
                readme_stream = archive.extractfile(readme_member)
                assert readme_stream is not None
                readme_text = readme_stream.read().decode("utf-8")
        forbidden = {
            "AGENTS.md",
            "build_ext.py",
            "numpy.i",
            "NUMPY-SWIG-LICENSE",
            "test_apod4.py",
            "apod4_stars.json",
            "ASTROMETRY-SOURCE-NOTICES",
            "ASTROMETRY-CREDITS",
            "GSL-COPYING",
            "QFITS-NOTICE",
            "SWIG-RUNTIME-LICENSE",
            "SWIG-COPYRIGHT",
            "SWIG-LICENSE-UNIVERSITIES",
        }
        assert not any(Path(name).name in forbidden for name in names), "Obsolete files in distribution"
        assert not any(".github" in Path(name).parts or "tools" in Path(name).parts for name in names)
        assert all(term not in readme_text for term in ("APOD4", "numpy.i"))
        assert all(
            term in readme_text for term in ("GPL-3.0-only", "arcsecond-astrometry", "Astrometry.net", "GSL", "qfits")
        )
        print(f"Validated {path.name}")
    if args.release:
        expected = {
            (platform, abi)
            for platform in ["linux-x86_64", "linux-aarch64", "macos-universal2", "win_amd64", "win_arm64"]
            for abi in release_abis
        }
        assert sdists == 1, f"Expected one sdist, found {sdists}"
        assert len(list(args.directory.glob("*.whl"))) == len(expected), (
            f"Release requires {len(expected)} active wheels"
        )
        assert seen == expected, f"Release matrix mismatch; missing {expected - seen}, unexpected {seen - expected}"


if __name__ == "__main__":
    main()
