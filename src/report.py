"""Phase 6b — coverage report and manual-review queue.

Usage:
    python -m src.report --dataset build/dataset/v0.1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from .common import ensure_dir, load_config, write_json, write_parquet


def _pct(series: pd.Series) -> float:
    if len(series) == 0:
        return 0.0
    return round(float(series.fillna(False).mean()) * 100, 1)


def report(config, dataset_dir: Path, out_dir: Path | None = None) -> dict:
    out = out_dir or (dataset_dir.parent.parent / "report")
    ensure_dir(out)
    recordings = pd.read_parquet(dataset_dir / "catalog/recordings.parquet")
    artists = pd.read_parquet(dataset_dir / "catalog/artists.parquet")
    albums = pd.read_parquet(dataset_dir / "catalog/albums.parquet")
    works = pd.read_parquet(dataset_dir / "catalog/works.parquet")
    coverage = pd.read_parquet(dataset_dir / "manifests/coverage.parquet")
    links = pd.read_parquet(dataset_dir / "links/links.parquet")
    unresolved = pd.read_parquet(dataset_dir / "links/unresolved.parquet")

    rec = recordings.merge(
        coverage.drop(columns=["work_id", "collection"], errors="ignore"),
        on="recording_id", how="left",
    )
    rec = rec.merge(
        artists[["artist_id", "name_lat"]], on="artist_id", how="left",
    )
    rec = rec.merge(
        albums[["album_id", "title"]].rename(columns={"title": "album_title"}),
        on="album_id", how="left",
    )
    rec = rec.merge(
        works[["work_id", "title_ar", "title_lat"]].rename(
            columns={"title_ar": "work_title_ar", "title_lat": "work_title_lat"}
        ),
        on="work_id", how="left",
    )

    agg_cols = ["has_lyrics", "has_timing", "has_transcript", "has_caption"]
    for field in agg_cols:
        rec[field] = rec[field].fillna(False).astype(bool)

    def summarize(group_cols: list[str]) -> pd.DataFrame:
        g = rec.groupby(group_cols, dropna=False).agg(
            recordings=("recording_id", "count"),
            has_lyrics=("has_lyrics", "sum"),
            has_timing=("has_timing", "sum"),
            has_transcript=("has_transcript", "sum"),
            has_caption=("has_caption", "sum"),
        ).reset_index()
        for field in agg_cols:
            g[field + "_pct"] = (g[field] / g["recordings"] * 100).round(1)
        return g.sort_values("recordings", ascending=False)

    by_artist = summarize(["artist_id", "name_lat"])
    by_album = summarize(["album_id", "album_title"])
    by_collection = summarize(["collection"])
    by_collection["collection"] = by_collection["collection"].fillna("unknown")

    write_parquet(by_artist, out / "coverage_by_artist.parquet")
    write_parquet(by_album, out / "coverage_by_album.parquet")
    write_parquet(by_collection, out / "coverage_by_collection.parquet")
    unresolved.to_csv(out / "unresolved.csv", index=False, encoding="utf-8-sig")

    # Manual review sample: 50 links, deterministic random seed.
    sample = links.sample(n=min(50, len(links)), random_state=20261102).copy()
    title_by_work = {
        str(r["work_id"]): (r.get("title_ar") or r.get("title_lat"))
        for _, r in works.iterrows()
    }
    title_by_rec = {
        str(r["recording_id"]): (r.get("title_lat") or r.get("title_ar") or r.get("audio_path"))
        for _, r in recordings.iterrows()
    }
    sample["src_title"] = sample.apply(
        lambda r: title_by_work.get(str(r["src_id"])) if r["src_type"] == "work"
        else title_by_rec.get(str(r["src_id"])), axis=1,
    )
    sample["dst_title"] = sample.apply(
        lambda r: title_by_work.get(str(r["dst_id"])) if r["dst_type"] == "work"
        else title_by_rec.get(str(r["dst_id"])), axis=1,
    )
    sample["verdict"] = ""
    sample["reviewer"] = ""
    sample["notes"] = ""
    sample = sample[[
        "link_id", "src_type", "src_id", "src_title", "dst_type", "dst_id",
        "dst_title", "relation", "method", "confidence", "evidence",
        "verdict", "reviewer", "notes",
    ]]
    sample.to_csv(out / "manual_review_sample.csv", index=False, encoding="utf-8-sig")

    # Automated precision proxy: exact methods are deterministic; priors are
    # counted at their own confidence.  This is not a substitute for the human
    # review file.
    method_conf = links.groupby("method")["confidence"].mean().to_dict()
    overall = round(float(links["confidence"].mean()) * 100, 2) if len(links) else 0.0

    summary = {
        "dataset": str(dataset_dir),
        "counts": {
            "recordings": int(len(recordings)),
            "works": int(len(works)),
            "links": int(len(links)),
            "unresolved": int(len(unresolved)),
        },
        "coverage": {
            field: _pct(rec[field]) for field in agg_cols
        },
        "by_collection": by_collection.to_dict(orient="records"),
        "unresolved_reasons": unresolved["reason"].value_counts().to_dict(),
        "method_mean_confidence": {k: round(float(v), 4) for k, v in method_conf.items()},
        "mean_link_confidence": overall,
        "manual_review_sample": str(out / "manual_review_sample.csv"),
        "gold_threshold": float(config.thresholds["gold"]),
        "pipeline_version": config.pipeline_version,
    }
    write_json(summary, out / "coverage.json")

    lines = [
        "# Coverage report",
        "",
        f"Dataset: `{dataset_dir}`",
        "",
        "## Overall",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Recordings | {len(recordings)} |",
        f"| Works | {len(works)} |",
        f"| Links | {len(links)} |",
        f"| Unresolved | {len(unresolved)} |",
    ]
    for field in agg_cols:
        lines.append(f"| {field} | {_pct(rec[field])}% |")
    lines += [
        "",
        "## By collection",
        "",
        "| Collection | Recordings | Lyrics | Timing | Transcript | Caption |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for _, r in by_collection.iterrows():
        lines.append(
            f"| {r['collection']} | {int(r['recordings'])} | "
            f"{r['has_lyrics_pct']}% | {r['has_timing_pct']}% | "
            f"{r['has_transcript_pct']}% | {r['has_caption_pct']}% |"
        )
    lines += [
        "",
        "## Unresolved reasons",
        "",
        "| Reason | Count |",
        "|---|---:|",
    ]
    for reason, count in summary["unresolved_reasons"].items():
        lines.append(f"| {reason} | {count} |")
    lines += [
        "",
        "## Manual review",
        "",
        f"A deterministic sample of {len(sample)} links is in "
        f"`{out / 'manual_review_sample.csv'}`. Fill `verdict` with "
        "`correct` / `wrong` / `unsure`; the file is intentionally blank in v0.1.",
        "",
        f"Mean link confidence (automated proxy): **{overall}%**. "
        f"Gold threshold: {config.thresholds['gold']}.",
        "",
    ]
    (out / "COVERAGE.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"[report] wrote {out}/COVERAGE.md and manual_review_sample.csv", flush=True)
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config/settings.yaml")
    ap.add_argument("--dataset", default="build/dataset/v0.1")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    config = load_config(args.config)
    report(config, Path(args.dataset), Path(args.out) if args.out else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
