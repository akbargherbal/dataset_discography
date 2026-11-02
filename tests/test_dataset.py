"""Dataset-level contract tests (skipped before packaging)."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.common import load_config  # noqa: E402
from src.validate import validate  # noqa: E402

DATASET = ROOT / "build" / "dataset" / "v0.1"
pytestmark = pytest.mark.skipif(
    not (DATASET / "manifest.json").exists(), reason="dataset has not been built"
)


def test_dataset_validates():
    config = load_config(ROOT / "config" / "settings.yaml")
    result = validate(config, DATASET)
    assert result["ok"], result["failed"]


def test_no_media_and_relative_paths():
    media_ext = {".mp3", ".m4a", ".wav", ".flac", ".jpg", ".jpeg", ".png", ".gif"}
    assert not [p for p in DATASET.rglob("*") if p.is_file() and p.suffix.lower() in media_ext]
    recordings = pd.read_parquet(DATASET / "catalog" / "recordings.parquet")
    assert not recordings["audio_path"].astype(str).str.startswith("gs://").any()
    assert not recordings["audio_path"].astype(str).str.startswith("/").any()
    assert "audio_uri" not in recordings.columns


def test_publication_shape():
    for rel in (
        "catalog/recordings.parquet",
        "artifacts/lyrics.parquet",
        "artifacts/timings.parquet",
        "artifacts/transcripts.parquet",
        "artifacts/captions.parquet",
        "links/links.parquet",
        "links/unresolved.parquet",
        "manifests/coverage.parquet",
        "jsonl/recordings.jsonl",
        "splits/train.parquet",
        "splits/val.parquet",
        "splits/test.parquet",
        "manifest.json",
        "DATASET_CARD.md",
        "_SUCCESS",
    ):
        assert (DATASET / rel).exists(), rel
