"""Phase 5 — package the text-only dataset.

Output layout matches DESIGN.md §6:
    catalog/ artifacts/ links/ manifests/ jsonl/ splits/
    manifest.json  DATASET_CARD.md  _SUCCESS

The package never contains audio or images; every audio reference is a
relative path from the bucket root.

Usage:
    python -m src.package_dataset --build build --out build/dataset/v0.1
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

import pandas as pd

from .common import (
    Config,
    SCHEMA_COLUMNS,
    conform_table,
    ensure_dir,
    json_dumps,
    load_config,
    sha256_bytes,
    utcnow,
    write_json,
    write_jsonl,
    write_parquet,
)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_json_text(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _assign_split(work_id: str, seed: str) -> str:
    h = int(hashlib.sha256(f"{seed}|{work_id}".encode()).hexdigest()[:8], 16)
    pct = h % 100
    if pct < 80:
        return "train"
    if pct < 90:
        return "val"
    return "test"


def _table_counts(tables: dict[str, pd.DataFrame]) -> dict:
    return {name: int(len(df)) for name, df in tables.items()}


def _coverage(
    recordings: pd.DataFrame,
    works: pd.DataFrame,
    lyrics: pd.DataFrame,
    timings: pd.DataFrame,
    transcripts: pd.DataFrame,
    captions: pd.DataFrame,
    links: pd.DataFrame,
    unresolved: pd.DataFrame,
) -> pd.DataFrame:
    lyr_by_work = lyrics[lyrics["work_id"].notna()].groupby("work_id")
    tim_by_rec = timings.groupby("recording_id")
    trn_by_rec = transcripts.groupby("recording_id")
    cap_by_rec = captions.groupby("recording_id")

    # Link confidence touch index.
    link_conf: dict[str, list[float]] = {}
    for _, l in links.iterrows():
        for typ, ident in (("recording", l["src_id"]), ("recording", l["dst_id"]),
                           ("work", l["src_id"]), ("work", l["dst_id"])):
            if typ == "recording":
                link_conf.setdefault(str(ident), []).append(float(l["confidence"]))

    unresolved_by_entity: dict[str, str] = {}
    if not unresolved.empty:
        for _, u in unresolved.iterrows():
            unresolved_by_entity.setdefault(str(u["entity_id"]), str(u["reason"]))

    rows = []
    for _, r in recordings.iterrows():
        rec_id = str(r["recording_id"])
        wid = r.get("work_id")
        has_work = pd.notna(wid)
        lyr_rows = lyr_by_work.get_group(str(wid)) if has_work and str(wid) in lyr_by_work.groups else lyrics.iloc[0:0]
        n_lyrics = int(len(lyr_rows))
        n_timings = int(len(tim_by_rec.get_group(rec_id))) if rec_id in tim_by_rec.groups else 0
        has_transcript = rec_id in trn_by_rec.groups
        has_caption = rec_id in cap_by_rec.groups
        confs = link_conf.get(rec_id, [])
        if has_work:
            confs = confs + link_conf.get(str(wid), [])
        reason = None
        if not has_work:
            reason = unresolved_by_entity.get(rec_id, "no_metadata")
        elif n_lyrics == 0:
            reason = (
                unresolved_by_entity.get(rec_id)
                or unresolved_by_entity.get(str(wid), "missing_lyrics")
            )
        else:
            reason = unresolved_by_entity.get(rec_id) or unresolved_by_entity.get(str(wid))
        rows.append({
            "recording_id": rec_id,
            "work_id": str(wid) if has_work else None,
            "collection": r.get("collection"),
            "has_lyrics": bool(n_lyrics > 0),
            "has_timing": bool(n_timings > 0),
            "has_transcript": bool(has_transcript),
            "has_caption": bool(has_caption),
            "n_lyrics": n_lyrics,
            "n_timings": n_timings,
            "min_link_confidence": min(confs) if confs else None,
            "unresolved_reason": reason,
        })
    return pd.DataFrame(rows)


def _jsonl_rows(
    recordings: pd.DataFrame,
    artists: pd.DataFrame,
    albums: pd.DataFrame,
    works: pd.DataFrame,
    lyrics: pd.DataFrame,
    timings: pd.DataFrame,
    transcripts: pd.DataFrame,
    captions: pd.DataFrame,
    bucket: str,
    lyrics_text_policy: str,
) -> list[dict]:
    artist_by_id = {str(r["artist_id"]): r.to_dict() for _, r in artists.iterrows()}
    album_by_id = {str(r["album_id"]): r.to_dict() for _, r in albums.iterrows()}
    work_by_id = {str(r["work_id"]): r.to_dict() for _, r in works.iterrows()}
    lyr_by_work = lyrics[lyrics["work_id"].notna()].groupby("work_id")
    tim_by_rec = timings.groupby("recording_id")
    trn_by_rec = transcripts.groupby("recording_id")
    cap_by_rec = captions.groupby("recording_id")

    out: list[dict] = []
    for _, r in recordings.iterrows():
        rec_id = str(r["recording_id"])
        wid = r.get("work_id")
        artist = artist_by_id.get(str(r.get("artist_id")), {})
        album = album_by_id.get(str(r.get("album_id")), {}) if pd.notna(r.get("album_id")) else {}
        work = work_by_id.get(str(wid), {}) if pd.notna(wid) else {}

        lyr_items = []
        if pd.notna(wid) and str(wid) in lyr_by_work.groups:
            for _, l in lyr_by_work.get_group(str(wid)).iterrows():
                item = {
                    "lyric_id": l["lyric_id"],
                    "source": l["source"],
                    "source_url": l["source_url"],
                    "title_ar": l["title_ar"],
                    "is_primary": bool(l["is_primary"]),
                    "completeness": l["completeness"],
                }
                if lyrics_text_policy == "full":
                    item["text"] = l["text"]
                else:
                    item["text_sha256"] = sha256_bytes((l["text"] or "").encode("utf-8")) if l["text"] else None
                lyr_items.append(item)

        tim_items = []
        if rec_id in tim_by_rec.groups:
            for _, t in tim_by_rec.get_group(rec_id).iterrows():
                tim_items.append({
                    "timing_id": t["timing_id"],
                    "run_id": t["run_id"],
                    "srt_path": t["srt_path"],
                    "vtt_path": t["vtt_path"],
                    "words_path": t["words_path"],
                    "n_words": int(t["n_words"]) if pd.notna(t["n_words"]) else None,
                    "is_preferred": bool(t["is_preferred"]),
                    "qa": _safe_json_text(t["qa"]),
                })

        trn = None
        if rec_id in trn_by_rec.groups:
            t = trn_by_rec.get_group(rec_id).iloc[0]
            trn = {
                "transcript_id": t["transcript_id"],
                "abc_path": t["abc_path"],
                "status": t["status"],
                "model": t["model"],
                "model_revision": t["model_revision"],
            }

        cap = None
        if rec_id in cap_by_rec.groups:
            c = cap_by_rec.get_group(rec_id).iloc[0]
            cap = {
                "caption_id": c["caption_id"],
                "model": c["model"],
                "instruments": list(c["instruments"]) if c["instruments"] is not None else [],
                "vocals": list(c["vocals"]) if c["vocals"] is not None else [],
                "production": list(c["production"]) if c["production"] is not None else [],
                "caption_text": c["caption_text"],
                "suno_prompt": c["suno_prompt"],
                "caption_seconds": c["caption_seconds"],
            }

        out.append({
            "recording_id": rec_id,
            "work": {
                "work_id": str(wid) if pd.notna(wid) else None,
                "title_ar": work.get("title_ar"),
                "title_lat": work.get("title_lat"),
                "title_norm": work.get("title_norm"),
            },
            "artist": {
                "artist_id": artist.get("artist_id"),
                "name": artist.get("name_lat"),
                "name_ar": artist.get("name_ar"),
            },
            "album": {
                "album_id": album.get("album_id"),
                "title": album.get("title"),
                "year": int(album["year"]) if pd.notna(album.get("year")) else None,
            } if album else None,
            "audio": {
                "bucket": bucket,
                "path": r["audio_path"],
                "sha256": r["audio_sha256"],
                "duration_s": float(r["duration_s"]) if pd.notna(r["duration_s"]) else None,
                "codec": r["codec"],
                "bitrate_kbps": int(r["bitrate_kbps"]) if pd.notna(r["bitrate_kbps"]) else None,
            },
            "lyrics": lyr_items,
            "timings": tim_items,
            "transcript": trn,
            "caption": cap,
            "provenance": [{"source": "gcs", "path": r["audio_path"]}],
        })
    return out


def _schema_json(tables: dict[str, pd.DataFrame]) -> dict:
    out: dict[str, dict] = {}
    for name, df in tables.items():
        out[name] = {}
        for col in df.columns:
            dtype = str(df[col].dtype)
            out[name][col] = dtype
    return out


def package(
    config: Config,
    build_dir: Path,
    out_dir: Path,
) -> dict:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    ensure_dir(out_dir)

    manifest_dir = build_dir / "manifest"
    core_dir = build_dir / "core"
    links_dir = build_dir / "links"
    artifacts_dir = build_dir / "artifacts"

    files = pd.read_parquet(manifest_dir / "files.parquet")
    audio_index = pd.read_parquet(manifest_dir / "audio_index.parquet")
    snapshot = json.loads((manifest_dir / "snapshot.json").read_text())
    artists = pd.read_parquet(core_dir / "artists.parquet")
    albums = pd.read_parquet(core_dir / "albums.parquet")
    works = pd.read_parquet(core_dir / "works.parquet")
    recordings = pd.read_parquet(core_dir / "recordings.parquet")
    legacy_ids = pd.read_parquet(core_dir / "legacy_ids.parquet")
    recording_works = pd.read_parquet(core_dir / "recording_works.parquet")
    lyrics = pd.read_parquet(artifacts_dir / "lyrics.parquet")
    timings = pd.read_parquet(links_dir / "timings.parquet")
    transcripts = pd.read_parquet(links_dir / "transcripts.parquet")
    captions = pd.read_parquet(links_dir / "captions.parquet")

    link_frames = [pd.read_parquet(links_dir / "links.parquet")]
    lyrics_links_path = links_dir / "lyrics_links.parquet"
    if lyrics_links_path.exists():
        link_frames.append(pd.read_parquet(lyrics_links_path))
    links = pd.concat(link_frames, ignore_index=True).drop_duplicates("link_id", keep="first")

    # Manual overrides.
    manual_path = Path("config/manual_links.csv")
    if manual_path.exists():
        manual = pd.read_csv(manual_path, dtype=str).fillna("")
        if not manual.empty and "src_type" in manual.columns:
            for _, m in manual.iterrows():
                links = pd.concat([links, pd.DataFrame([{
                    "link_id": "lnk_" + hashlib.sha256(
                        f"manual|{m['src_type']}|{m['src_id']}|{m['dst_type']}|{m['dst_id']}".encode()
                    ).hexdigest()[:16],
                    "src_type": m["src_type"],
                    "src_id": m["src_id"],
                    "dst_type": m["dst_type"],
                    "dst_id": m["dst_id"],
                    "relation": m["relation"],
                    "method": m["method"],
                    "confidence": float(m["confidence"]),
                    "evidence": m["evidence"] or "{}",
                    "is_gold": float(m["confidence"]) >= float(config.thresholds["gold"]),
                    "created_at": utcnow(),
                }])], ignore_index=True)
            links = links.drop_duplicates("link_id", keep="last")

    unresolved_path = links_dir / "unresolved.parquet"
    unresolved = pd.read_parquet(unresolved_path) if unresolved_path.exists() else pd.DataFrame(
        columns=["entity_type", "entity_id", "collection", "title", "reason", "details"]
    )

    coverage = _coverage(recordings, works, lyrics, timings, transcripts, captions,
                         links, unresolved)

    # ---- splits by work_id ------------------------------------------------
    seed = str(config.package.get("splits", {}).get("seed", "v0.1"))
    split_rows = []
    for _, w in works.iterrows():
        wid = str(w["work_id"])
        split_rows.append({"work_id": wid, "split": _assign_split(wid, seed)})
    splits = pd.DataFrame(split_rows)
    # Validate: every work appears once.
    if not splits.empty and splits["work_id"].duplicated().any():
        raise RuntimeError("split leakage: a work_id appears more than once")

    tables = {
        "artists": conform_table(artists, "artists"),
        "albums": conform_table(albums, "albums"),
        "works": conform_table(works, "works"),
        "recordings": conform_table(recordings, "recordings"),
        "legacy_ids": conform_table(legacy_ids, "legacy_ids"),
        "recording_works": conform_table(recording_works, "recording_works"),
        "lyrics": conform_table(lyrics, "lyrics"),
        "timings": conform_table(timings, "timings"),
        "transcripts": conform_table(transcripts, "transcripts"),
        "captions": conform_table(captions, "captions"),
        "links": conform_table(links, "links"),
        "files": conform_table(files, "files"),
        "coverage": conform_table(coverage, "coverage"),
        "splits": conform_table(splits, "splits"),
    }

    # ---- write catalog/artifacts/links/manifests --------------------------
    layout = {
        "catalog": ["artists", "albums", "works", "recordings", "legacy_ids",
                    "recording_works"],
        "artifacts": ["lyrics", "timings", "transcripts", "captions"],
        "links": ["links"],
        "manifests": ["files"],
    }
    for folder, names in layout.items():
        ensure_dir(out_dir / folder)
        for name in names:
            write_parquet(tables[name], out_dir / folder / f"{name}.parquet")
    write_parquet(tables["coverage"], out_dir / "manifests" / "coverage.parquet")
    write_parquet(tables["splits"], out_dir / "splits" / "splits.parquet")
    for split_name in ("train", "val", "test"):
        write_parquet(
            tables["splits"][tables["splits"]["split"] == split_name].reset_index(drop=True),
            out_dir / "splits" / f"{split_name}.parquet",
        )
    write_parquet(audio_index, out_dir / "manifests" / "audio_index.parquet")
    write_parquet(unresolved, out_dir / "links" / "unresolved.parquet")

    # ---- JSONL -----------------------------------------------------------
    policy = str(config.package.get("lyrics_text_policy", "full"))
    jsonl_rows = _jsonl_rows(recordings, artists, albums, works, lyrics,
                             timings, transcripts, captions, config.bucket, policy)
    write_jsonl(jsonl_rows, out_dir / "jsonl" / "recordings.jsonl")

    # ---- schema ----------------------------------------------------------
    ensure_dir(out_dir / "schema")
    schema_src = Path("schema/core.sql")
    if schema_src.exists():
        shutil.copy2(schema_src, out_dir / "schema" / "core.sql")
    write_json(_schema_json(tables), out_dir / "schema" / "schema.json")

    # ---- manifest.json ---------------------------------------------------
    expected = {
        "recordings": 329,
        "album_tracks": 236,
        "timings": 167,
        "transcripts": 236,
        "captions": 230,
        "lyrics_gold": 174,
    }
    lyric_gold_mask = (
        (links["src_type"] == "lyric")
        & (links["dst_type"] == "work")
        & links["is_gold"].fillna(False)
    )
    counts = _table_counts(tables)
    counts.update({
        "album_tracks": int((recordings["collection"] == "discography_album").sum()),
        "lyrics_gold": int(lyric_gold_mask.sum()),
        "unique_audio_files": int(len(audio_index)),
        "audio_bytes_referenced": int(audio_index["bytes"].fillna(0).sum()),
    })

    data_files: dict[str, dict] = {}
    for p in sorted(out_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(out_dir).as_posix()
        if rel in {"manifest.json", "_SUCCESS"}:
            continue
        data_files[rel] = {"sha256": _sha256_file(p), "bytes": p.stat().st_size}

    dataset_bytes = sum(v["bytes"] for v in data_files.values())
    delta_reasons = {
        "recordings": (
            "15 byte-identical duplicates were found (14 p0/pd + 1 pd/TODO), "
            "not 14; content-addressed identity therefore yields 328 unique "
            "recordings instead of the planned ~329."
        ),
        "lyrics_gold": (
            "174 title-map seed matches plus 3 additional exact title+artist "
            "matches outside the seed (Lola El Malama, Yana Yana, Hawa Ya Hawa)."
        ),
        "transcripts": (
            "The pinned ABC source contains 227 ok and 9 no-abc tracks; no-abc "
            "rows are kept as flagged transcripts with empty abc_text."
        ),
        "p0_pd_unresolved": (
            "79 unique p0/pd recordings remain without metadata (the 80th "
            "unique p0/pd file is byte-identical to a TODO reference and was "
            "canonicalised to it)."
        ),
    }
    manifest = {
        "dataset": "Arabic Music Discography Graph",
        "dataset_version": config.dataset_version,
        "pipeline_version": config.pipeline_version,
        "snapshot_id": snapshot.get("snapshot_id"),
        "gcs_snapshot_id": snapshot.get("gcs_snapshot_id"),
        "generated_at": utcnow().isoformat(),
        "source_bucket": config.bucket,
        "source_commits": snapshot.get("source_commits", {}),
        "source_repos": snapshot.get("github_repos", {}),
        "counts": counts,
        "expected_counts": expected,
        "count_deltas": {k: counts.get(k, 0) - v for k, v in expected.items()},
        "count_delta_reasons": delta_reasons,
        "audio_policy": {
            "audio_embedded": False,
            "images_embedded": False,
            "paths": "relative to bucket root",
            "audio_bucket_field": "recordings.audio_bucket",
        },
        "lyrics_text_policy": policy,
        "data_bytes": dataset_bytes,
        "data_files": data_files,
        "tables": {k: list(v.columns) for k, v in tables.items()},
    }
    write_json(manifest, out_dir / "manifest.json")

    # ---- dataset card ----------------------------------------------------
    _write_dataset_card(out_dir / "DATASET_CARD.md", config, manifest, coverage,
                        unresolved, lyrics)

    (out_dir / "_SUCCESS").write_text("ok\n", encoding="utf-8")
    print(
        f"[package] wrote {out_dir} ({dataset_bytes/1024/1024:.2f} MiB, "
        f"{len(data_files)} files)",
        flush=True,
    )
    return manifest


def _write_dataset_card(path: Path, config: Config, manifest: dict,
                        coverage: pd.DataFrame, unresolved: pd.DataFrame,
                        lyrics: pd.DataFrame) -> None:
    c = manifest["counts"]
    n_missing_text = int(
        (lyrics["text"].isna() & lyrics["work_id"].notna()).sum()
    ) if not lyrics.empty else 0
    unresolved_reasons = (
        unresolved["reason"].value_counts().to_dict() if not unresolved.empty else {}
    )
    lines = [
        f"# Arabic Music Discography Graph — {config.dataset_version}",
        "",
        f"**Snapshot:** `{manifest['snapshot_id']}`  ",
        f"**Pipeline:** `{manifest['pipeline_version']}`  ",
        f"**Generated:** {manifest['generated_at']}  ",
        f"**Source bucket:** `{config.bucket}`",
        "",
        "## What this is",
        "",
        "A text-only catalog and artifact graph for Arabic music. It links songs "
        "(works), specific audio performances (recordings), and derived text "
        "(lyrics, SRT timings, ABC transcriptions, audio captions) without "
        "shipping any audio or images.",
        "",
        "Audio is referenced by **bucket + relative path + SHA-256** only:",
        "",
        "```python",
        "path = f\"gs://{row.audio_bucket}/{row.audio_path}\"",
        "```",
        "",
        "## Counts",
        "",
        "| Item | Count |",
        "|---|---:|",
        f"| Unique recordings | {c.get('recordings', 0)} |",
        f"| Album tracks | {c.get('album_tracks', 0)} |",
        f"| Artists | {c.get('artists', 0)} |",
        f"| Albums | {c.get('albums', 0)} |",
        f"| Works | {c.get('works', 0)} |",
        f"| SRT timings | {c.get('timings', 0)} |",
        f"| ABC transcripts | {c.get('transcripts', 0)} |",
        f"| Captions | {c.get('captions', 0)} |",
        f"| Gold lyric–work links | {c.get('lyrics_gold', 0)} |",
        f"| Unresolved entities | {len(unresolved)} |",
        f"| Dataset size | {manifest['data_bytes']/1024/1024:.2f} MiB |",
        "",
        "## Sources",
        "",
        "| Source | What was used |",
        "|---|---|",
        "| GCS `DISCOGRAPHY/` | audio manifest, hashes, ffprobe metadata (no audio bytes) |",
        "| GCS `SRT/v1/` | SRT/VTT/words.json timings (167 tracks, 5 runs) |",
        "| GCS `TRANSCRIPTS/` | ABC/SheetSage2 JSON (236 tracks; 23 albums) |",
        "| `music_captioning_oct_2026` | ACE-Step captions and Suno prompts (230 real tracks) |",
        "| `yue2-karaoke-align` | legacy track ids, title map and prior links |",
        "| `arabic-music-lyrics` | public lyrics corpus + fnanen/kalimataghani scrapes |",
        "| `REF/maqam_analysis` | YouTube-id evidence only (full analysis deferred to v0.2) |",
        "",
        "Pinned commits:",
        "",
        "```json",
        json.dumps(manifest.get("source_commits", {}), indent=2),
        "```",
        "",
        "## Identity model",
        "",
        "* `recording_id` is content-addressed: `rec_<sha256[:16]>`.",
        "* `work_id` groups performances of the same song; transliterations are "
        "resolved through a title map and an alias table.",
        "* Source-specific ids (yue2, mcap, SRT, ABC, YouTube, Spotify, GCS) are "
        "kept in `catalog/legacy_ids.parquet`.",
        "* `links` is the auditable edge list; every row has `method`, "
        "`confidence` and JSON `evidence`.",
        "",
        "## Coverage",
        "",
    ]
    for field in ("has_lyrics", "has_timing", "has_transcript", "has_caption"):
        if field in coverage:
            pct = coverage[field].fillna(False).mean() * 100
            lines.append(f"* `{field}`: {pct:.1f}% of recordings")
    lines += [
        "",
        "Unresolved reasons:",
        "",
        "```json",
        json.dumps(unresolved_reasons, indent=2),
        "```",
        "",
        "## Known limitations (v0.1)",
        "",
        "* Audio and cover images are **not** included; only relative paths and hashes.",
        "* The 343 audio objects contain 15 byte-identical duplicates (14 p0/pd "
        "+ 1 pd/TODO), so content-addressed identity yields 328 unique recordings.",
        "* 79 unique `p0`/`pd` Spotify recordings have no metadata: `work_id = NULL`, "
        "`unresolved_reason = no_metadata`. One additional `pd` file is a byte "
        "duplicate of a TODO reference and is canonicalised to it.",
        "* 4 title-map matches have no lyric text in the pinned lyrics sources; "
        f"they are kept as title-level rows (`{n_missing_text}` rows with missing text).",
        "* SRT artifacts come from several runs; `is_preferred` selects one per recording.",
        "* The ABC source produced 227 ok tracks and 9 `no-abc` tracks; the latter "
        "are kept as `flagged` transcripts with no ABC text.",
        "* Generated `arabmaqamrock` material and full maqam analysis are deferred to v0.2.",
        "* Transcript/caption model revisions are recorded from the source indexes.",
        "",
        "## Rights and privacy",
        "",
        "* No audio, cover art or other media is redistributed.",
        "* Lyrics come from public web sources (fnanen, kalimataghani, Genius and "
        "the public `arabic-music-lyrics` corpus); the original source URL is kept "
        "on every lyrics row. Check each source's terms before redistributing the "
        "text in a commercial product. Set `package.lyrics_text_policy: hash` in "
        "`config/settings.yaml` to publish hashes and source URLs instead of text.",
        "* ABC transcriptions and captions are model outputs; model names and "
        "revisions are stored in the corresponding tables.",
        "",
        "## Usage",
        "",
        "```sql",
        "-- DuckDB",
        "SELECT r.recording_id, w.title_ar, l.source, t.n_words, c.suno_prompt",
        "FROM 'catalog/recordings.parquet' r",
        "LEFT JOIN 'catalog/works.parquet' w USING (work_id)",
        "LEFT JOIN 'artifacts/lyrics.parquet' l",
        "  ON l.work_id = w.work_id AND l.is_primary",
        "LEFT JOIN 'artifacts/timings.parquet' t",
        "  ON t.recording_id = r.recording_id AND t.is_preferred",
        "LEFT JOIN 'artifacts/captions.parquet' c",
        "  ON c.recording_id = r.recording_id",
        "WHERE r.collection = 'discography_album';",
        "```",
        "",
        "## Reproduction",
        "",
        "```bash",
        "make bootstrap",
        "make build",
        "make validate",
        "```",
        "",
        "The build is pinned by `manifest.json.snapshot_id` and the source commits "
        "above. No audio is downloaded permanently at any stage.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config/settings.yaml")
    ap.add_argument("--build", default="build")
    ap.add_argument("--out", default="build/dataset/v0.1")
    args = ap.parse_args(argv)
    config = load_config(args.config)
    package(config, Path(args.build), Path(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
