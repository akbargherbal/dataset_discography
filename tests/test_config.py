"""Configuration and schema contract tests."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.common import SCHEMA_COLUMNS, load_config  # noqa: E402


def test_config_loads():
    config = load_config(ROOT / "config" / "settings.yaml")
    assert config.bucket == "akbar-december-2024-backup"
    assert config.dataset_version == "v0.1"
    assert set(config.repos) == {"yue2", "mcap", "lyrics"}
    assert all(len(r["commit"]) == 40 for r in config.repos.values())


def test_schema_columns_have_keys():
    for table in ("recordings", "works", "lyrics", "timings", "links", "coverage"):
        assert table in SCHEMA_COLUMNS
        assert SCHEMA_COLUMNS[table]
        assert len(SCHEMA_COLUMNS[table]) == len(set(SCHEMA_COLUMNS[table]))


def test_schema_sql_exists():
    assert (ROOT / "schema" / "core.sql").exists()
    sql = (ROOT / "schema" / "core.sql").read_text()
    for table in SCHEMA_COLUMNS:
        assert f"CREATE TABLE {table}" in sql or table in {"splits"}
