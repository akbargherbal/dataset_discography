"""Phase 6 — automated validation / QA.

Runs the checks from DESIGN.md §7.2 and writes build/report/validation.json.

Usage:
    python -m src.validate --dataset build/dataset/v0.1
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import pandas as pd

from .common import SCHEMA_COLUMNS, ensure_dir, load_config, write_json


class Checker:
    def __init__(self) -> None:
        self.checks: list[dict] = []

    def check(self, name: str, ok: bool, detail: str = "", warning: bool = False) -> None:
        self.checks.append({
            "name": name,
            "ok": bool(ok),
            "warning": bool(warning),
            "detail": detail,
        })

    @property
    def failed(self) -> list[dict]:
        return [c for c in self.checks if not c["ok"] and not c["warning"]]

    @property
    def warnings(self) -> list[dict]:
        return [c for c in self.checks if not c["ok"] and c["warning"]]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return {}
    return value or {}


def validate(config, dataset_dir: Path, out_dir: Path | None = None) -> dict:
    c = Checker()
    required = [
        "manifest.json",
        "DATASET_CARD.md",
        "_SUCCESS",
        "catalog/artists.parquet",
        "catalog/albums.parquet",
        "catalog/works.parquet",
        "catalog/recordings.parquet",
        "catalog/legacy_ids.parquet",
        "artifacts/lyrics.parquet",
        "artifacts/timings.parquet",
        "artifacts/transcripts.parquet",
        "artifacts/captions.parquet",
        "links/links.parquet",
        "links/unresolved.parquet",
        "manifests/files.parquet",
        "manifests/audio_index.parquet",
        "manifests/coverage.parquet",
        "jsonl/recordings.jsonl",
        "splits/train.parquet",
        "splits/val.parquet",
        "splits/test.parquet",
        "schema/core.sql",
    ]
    missing = [p for p in required if not (dataset_dir / p).exists()]
    c.check("required_files", not missing, f"missing: {missing}" if missing else "all present")
    if missing:
        return _finish(c, dataset_dir, out_dir)

    manifest = json.loads((dataset_dir / "manifest.json").read_text())
    artists = pd.read_parquet(dataset_dir / "catalog/artists.parquet")
    albums = pd.read_parquet(dataset_dir / "catalog/albums.parquet")
    works = pd.read_parquet(dataset_dir / "catalog/works.parquet")
    recordings = pd.read_parquet(dataset_dir / "catalog/recordings.parquet")
    legacy = pd.read_parquet(dataset_dir / "catalog/legacy_ids.parquet")
    lyrics = pd.read_parquet(dataset_dir / "artifacts/lyrics.parquet")
    timings = pd.read_parquet(dataset_dir / "artifacts/timings.parquet")
    transcripts = pd.read_parquet(dataset_dir / "artifacts/transcripts.parquet")
    captions = pd.read_parquet(dataset_dir / "artifacts/captions.parquet")
    links = pd.read_parquet(dataset_dir / "links/links.parquet")
    unresolved = pd.read_parquet(dataset_dir / "links/unresolved.parquet")
    files = pd.read_parquet(dataset_dir / "manifests/files.parquet")
    audio_index = pd.read_parquet(dataset_dir / "manifests/audio_index.parquet")
    coverage = pd.read_parquet(dataset_dir / "manifests/coverage.parquet")
    splits = pd.read_parquet(dataset_dir / "splits/splits.parquet")

    # ---- schema ---------------------------------------------------------
    tables = {
        "artists": artists, "albums": albums, "works": works,
        "recordings": recordings, "legacy_ids": legacy, "lyrics": lyrics,
        "timings": timings, "transcripts": transcripts, "captions": captions,
        "links": links, "files": files, "coverage": coverage, "splits": splits,
    }
    for name, df in tables.items():
        missing_cols = [x for x in SCHEMA_COLUMNS[name] if x not in df.columns]
        extra_cols = [x for x in df.columns if x not in SCHEMA_COLUMNS[name]]
        c.check(f"schema_{name}", not missing_cols and not extra_cols,
                f"missing={missing_cols} extra={extra_cols}")

    # ---- identity -------------------------------------------------------
    c.check("recording_id_unique", not recordings["recording_id"].duplicated().any())
    c.check("work_id_unique", not works["work_id"].duplicated().any())
    c.check("album_id_unique", not albums["album_id"].duplicated().any())

    # work_id exists or has a reason
    valid_works = set(works["work_id"].astype(str))
    bad_work = recordings[
        recordings["work_id"].notna() & ~recordings["work_id"].astype(str).isin(valid_works)
    ]
    c.check("recording_work_fk", bad_work.empty,
            f"{len(bad_work)} recordings point to missing works")

    cov_by_rec = coverage.set_index("recording_id")
    no_work = recordings[recordings["work_id"].isna()]
    no_reason = []
    for _, r in no_work.iterrows():
        reason = cov_by_rec.loc[r["recording_id"], "unresolved_reason"] if r["recording_id"] in cov_by_rec.index else None
        if reason is None or pd.isna(reason) or str(reason) == "":
            no_reason.append(r["recording_id"])
    c.check("null_work_has_reason", not no_reason,
            f"{len(no_reason)} recordings have NULL work_id and no reason")

    # ---- foreign keys ---------------------------------------------------
    rec_ids = set(recordings["recording_id"].astype(str))
    for name, df in (("timings", timings), ("transcripts", transcripts),
                     ("captions", captions)):
        bad = df[~df["recording_id"].astype(str).isin(rec_ids)]
        c.check(f"{name}_recording_fk", bad.empty,
                f"{len(bad)} rows point to missing recordings")
    bad_lyr_work = lyrics[
        lyrics["work_id"].notna() & ~lyrics["work_id"].astype(str).isin(valid_works)
    ]
    c.check("lyrics_work_fk", bad_lyr_work.empty,
            f"{len(bad_lyr_work)} lyrics point to missing works")

    # link endpoints
    endpoints = {
        "work": valid_works,
        "recording": rec_ids,
        "timing": set(timings["timing_id"].astype(str)),
        "transcript": set(transcripts["transcript_id"].astype(str)),
        "caption": set(captions["caption_id"].astype(str)),
        "lyric": set(lyrics["lyric_id"].astype(str)),
    }
    bad_links = []
    for _, l in links.iterrows():
        for side in ("src", "dst"):
            typ = str(l[f"{side}_type"])
            ident = str(l[f"{side}_id"])
            if typ in endpoints and ident not in endpoints[typ]:
                bad_links.append((typ, ident, l["link_id"]))
    c.check("link_endpoints", not bad_links,
            f"{len(bad_links)} dangling link endpoints")

    # ---- paths / media --------------------------------------------------
    path_cols = [c for c in recordings.columns if c.endswith("_path")]
    bad_paths = []
    for col in path_cols:
        for v in recordings[col].dropna().astype(str):
            if v.startswith("gs://") or v.startswith("/") or ".." in Path(v).parts:
                bad_paths.append((col, v))
    c.check("audio_path_relative", not bad_paths,
            f"{len(bad_paths)} non-relative audio paths")
    c.check("no_audio_uri_column", "audio_uri" not in recordings.columns)

    for name, df, cols in (
        ("timings", timings, ["srt_path", "vtt_path", "words_path"]),
        ("transcripts", transcripts, ["abc_path"]),
    ):
        bad = []
        for col in cols:
            for v in df[col].dropna().astype(str):
                if v.startswith("gs://") or v.startswith("/"):
                    bad.append((col, v))
        c.check(f"{name}_paths_relative", not bad, f"{len(bad)} non-relative paths")

    media = []
    image_ext = {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".aac",
                 ".jpg", ".jpeg", ".png", ".webp", ".gif"}
    for p in dataset_dir.rglob("*"):
        if p.is_file() and p.suffix.lower() in image_ext:
            media.append(str(p.relative_to(dataset_dir)))
    c.check("no_media_files", not media, f"found {media[:5]}")

    # ---- timings --------------------------------------------------------
    bad_timing = []
    for _, t in timings.iterrows():
        qa = _read_json(t["qa"])
        if not qa.get("monotone", True):
            bad_timing.append((t["timing_id"], "non-monotone"))
            continue
        if qa.get("duration_ok") is False:
            bad_timing.append((t["timing_id"], "end beyond duration"))
        if qa.get("last_end_s") is not None and qa.get("duration_s") is not None:
            if float(qa["last_end_s"]) > float(qa["duration_s"]) + 2.0:
                bad_timing.append((t["timing_id"], "end beyond duration"))
    c.check("timings_monotonic", not bad_timing,
            f"{len(bad_timing)} invalid timings")

    # ---- ABC ------------------------------------------------------------
    bad_abc = []
    flagged_no_text = 0
    for _, t in transcripts.iterrows():
        text = str(t["abc_text"] or "")
        if not text:
            # `no-abc` source tracks are kept as flagged rows with no text.
            if t["status"] == "flagged":
                flagged_no_text += 1
            else:
                bad_abc.append((t["transcript_id"], "empty ABC with ok status"))
            continue
        if not text.startswith("X:"):
            bad_abc.append((t["transcript_id"], "missing X:"))
        elif not re.search(r"V:\s*Vocal", text):
            bad_abc.append((t["transcript_id"], "missing V: Vocal"))
    c.check("abc_structure", not bad_abc,
            f"{len(bad_abc)} invalid ABC rows; {flagged_no_text} flagged no-abc rows")

    # ---- confidence / gold ---------------------------------------------
    bad_gold = links[
        links["is_gold"].fillna(False) & (links["confidence"].astype(float) < float(config.thresholds["gold"]))
    ]
    c.check("gold_confidence", bad_gold.empty,
            f"{len(bad_gold)} gold links below threshold")
    bad_conf = links[
        (links["confidence"].astype(float) < 0) | (links["confidence"].astype(float) > 1)
    ]
    c.check("confidence_range", bad_conf.empty, f"{len(bad_conf)} out-of-range confidence values")
    c.check("links_have_evidence", links["evidence"].notna().all() and
            (links["evidence"].astype(str).str.len() > 2).all(),
            "all links carry evidence")

    # ---- splits ---------------------------------------------------------
    c.check("split_unique_work", not splits["work_id"].duplicated().any())
    c.check("split_values", set(splits["split"]).issubset({"train", "val", "test", "generated"}),
            f"values={sorted(set(splits['split']))}")
    split_files = {}
    for name in ("train", "val", "test"):
        part = pd.read_parquet(dataset_dir / "splits" / f"{name}.parquet")
        split_files[name] = set(part["work_id"].astype(str))
    c.check("split_files_match", all(
        (set(splits[splits["split"] == name]["work_id"].astype(str)) ==
         split_files[name]) for name in ("train", "val", "test")
    ), "split parquet files do not match splits.parquet")
    overlap = (split_files["train"] & split_files["val"]) | \
              (split_files["train"] & split_files["test"]) | \
              (split_files["val"] & split_files["test"])
    c.check("split_no_leakage", not overlap, f"{len(overlap)} works in more than one split")

    # ---- manifest hashes / size -----------------------------------------
    bad_hash = []
    for rel, meta in (manifest.get("data_files") or {}).items():
        p = dataset_dir / rel
        if not p.exists():
            bad_hash.append((rel, "missing"))
        elif _sha256_file(p) != meta.get("sha256"):
            bad_hash.append((rel, "hash mismatch"))
    c.check("manifest_hashes", not bad_hash, f"{len(bad_hash)} mismatches")
    size_mb = sum(p.stat().st_size for p in dataset_dir.rglob("*") if p.is_file()) / 1024 / 1024
    c.check("dataset_size", size_mb < float(config.package.get("max_size_mb", 100)),
            f"{size_mb:.2f} MiB")
    c.check("snapshot_id", bool(manifest.get("snapshot_id")),
            str(manifest.get("snapshot_id")))

    # ---- counts ---------------------------------------------------------
    expected = manifest.get("expected_counts", {})
    actual = manifest.get("counts", {})
    deltas = {k: int(actual.get(k, 0)) - int(v) for k, v in expected.items()}
    hard = {k: v for k, v in deltas.items() if v != 0 and k in {"recordings", "album_tracks",
                                                                 "timings", "transcripts", "captions"}}
    c.check("expected_counts", not hard, f"deltas={deltas}", warning=bool(hard))
    c.check("lyrics_gold_count", deltas.get("lyrics_gold", 0) == 0,
            f"deltas={deltas}", warning=True)
    c.check("unresolved_reasons", unresolved["reason"].notna().all() and
            (unresolved["reason"].astype(str).str.len() > 0).all(),
            "every unresolved row has a reason")

    return _finish(c, dataset_dir, out_dir)


def _finish(c: Checker, dataset_dir: Path, out_dir: Path | None) -> dict:
    result = {
        "dataset": str(dataset_dir),
        "ok": not c.failed,
        "n_checks": len(c.checks),
        "n_failed": len(c.failed),
        "n_warnings": len(c.warnings),
        "failed": c.failed,
        "warnings": c.warnings,
        "checks": c.checks,
    }
    out = out_dir or (dataset_dir.parent.parent / "report")
    ensure_dir(out)
    write_json(result, out / "validation.json")
    lines = ["# Validation report", "", f"Dataset: `{dataset_dir}`", ""]
    lines.append(f"* checks: {result['n_checks']}")
    lines.append(f"* failed: {result['n_failed']}")
    lines.append(f"* warnings: {result['n_warnings']}")
    lines.append("")
    for chk in c.checks:
        mark = "PASS" if chk["ok"] else ("WARN" if chk["warning"] else "FAIL")
        suffix = f" — {chk['detail']}" if chk["detail"] else ""
        lines.append(f"- **{mark}** `{chk['name']}`{suffix}")
    (out / "VALIDATION.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        f"[validate] checks={result['n_checks']} failed={result['n_failed']} "
        f"warnings={result['n_warnings']} ok={result['ok']}",
        flush=True,
    )
    if not result["ok"]:
        for f in result["failed"]:
            print(f"[validate] FAIL {f['name']}: {f['detail']}", flush=True)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config/settings.yaml")
    ap.add_argument("--dataset", default="build/dataset/v0.1")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    config = load_config(args.config)
    result = validate(config, Path(args.dataset),
                      Path(args.out) if args.out else None)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
