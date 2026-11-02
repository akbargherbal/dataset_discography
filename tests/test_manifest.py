"""Integration checks for the scanner output (skipped before a build)."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MANIFEST = ROOT / "build" / "manifest"
pytestmark = pytest.mark.skipif(
    not (MANIFEST / "files.parquet").exists(), reason="manifest has not been built"
)


def test_audio_counts():
    files = pd.read_parquet(MANIFEST / "files.parquet")
    audio = files[files["is_audio"].fillna(False)]
    index = pd.read_parquet(MANIFEST / "audio_index.parquet")
    # 343 audio objects, 15 byte-identical duplicates (14 p0/pd + 1 pd/TODO).
    assert len(audio) == 343
    assert len(index) == 328
    assert int(audio["duplicate_of"].notna().sum()) == 15


def test_no_audio_bytes_in_manifest():
    files = pd.read_parquet(MANIFEST / "files.parquet")
    for column in ("audio_bytes", "audio_data", "bytes_content"):
        assert column not in files.columns


def test_all_hashes_are_sha256():
    files = pd.read_parquet(MANIFEST / "files.parquet")
    hashes = files["sha256"].dropna()
    assert len(hashes) == len(files)
    assert hashes.str.fullmatch(r"[0-9a-f]{64}").all()


def test_duplicate_groups_cross_p0_pd():
    files = pd.read_parquet(MANIFEST / "files.parquet")
    dups = files[files["duplicate_of"].notna() & files["is_audio"].fillna(False)]
    assert len(dups) == 15
    assert set(dups["collection"]).issubset({"spotify_p0", "spotify_pd"})
