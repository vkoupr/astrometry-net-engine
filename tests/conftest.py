# Copyright (c) 2026 Vladimir Kouprianov (v.kouprianov@gmail.com)
# SPDX-License-Identifier: GPL-3.0-only
"""Native-extension checks and lazy, cached test index downloads."""

from __future__ import annotations

import os
import shutil
import ssl
import sys
import urllib.request
from importlib import import_module
from pathlib import Path
from typing import cast

import pytest
from astropy.io import fits


def pytest_sessionstart(session: pytest.Session) -> None:
    """Ensure session can import the native extension from an installed or in-place build."""
    import_module("astrometry_net_engine._an_engine")


@pytest.fixture(scope="session")
def index_path() -> Path:
    """Return the cached 4110 index outside the installed package, downloading only with explicit opt-in."""
    project_root = Path(__file__).resolve().parents[1]
    cache_root = project_root if (project_root / "astrometry_net_engine").is_dir() else Path.cwd()
    default_cache = cache_root / ".test-indexes"
    directory = Path(os.environ.get("AN_ENGINE_INDEX_CACHE", str(default_cache))).expanduser()
    for name in ("index-4110.fits",):
        target = directory / name
        valid = False
        if target.exists():
            try:
                with fits.open(target) as hdul:
                    valid = cast(fits.PrimaryHDU, hdul[0]).header.get("AN_FILE") == "QUAD"
            except OSError:
                pass
        if not valid:
            if os.environ.get("AN_ENGINE_DOWNLOAD_INDEXES") != "1":
                pytest.skip("Set AN_ENGINE_DOWNLOAD_INDEXES=1 to download the test indexes")
            directory.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".download")
            try:
                context = ssl.create_default_context()
                # Python.org macOS installs may lack OpenSSL roots; use the OS bundle without overriding custom trust.
                if (
                    sys.platform == "darwin"
                    and not context.get_ca_certs()
                    and "SSL_CERT_FILE" not in os.environ
                    and "SSL_CERT_DIR" not in os.environ
                ):
                    context.load_verify_locations(cafile="/etc/ssl/cert.pem")
                with urllib.request.urlopen(
                    f"https://data.astrometry.net/4100/{name}", timeout=120, context=context
                ) as response:
                    with temporary.open("wb") as stream:
                        shutil.copyfileobj(response, stream)
                with fits.open(temporary) as hdul:
                    assert cast(fits.PrimaryHDU, hdul[0]).header.get("AN_FILE") == "QUAD"
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
    return directory
