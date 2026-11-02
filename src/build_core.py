"""Phase 2 — build artists / albums / works / recordings / legacy_ids.

The catalog is built from the audio index (one row per unique SHA-256) and the
pinned source indexes (yue2, mcap, ABC, SRT).  p0/pd UUID tracks are kept with
work_id = NULL and unresolved_reason = no_metadata; they are never fuzzy
matched against UUIDs.

Usage:
    python -m src.build_core --manifest build/manifest --out build/core
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

from .common import (
    Config,
    album_id,
    artist_id as make_artist_id,
    canonical_artist,
    choose_canonical,
    clean_title,
    conform_table,
    ensure_dir,
    extract_youtube_id,
    infer_album_type,
    load_abc_tracks,
    load_artist_aliases,
    load_config,
    load_mcap,
    load_srt_corpus,
    load_yue2,
    normalize_for_path,
    normalize_text,
    parse_audio_path,
    recording_id,
    utcnow,
    work_id as make_work_id,
    write_json,
    write_parquet,
)


def _key(album: str | None, file: str | None) -> str:
    return normalize_text(album) + "|" + normalize_text(file)


def _base_work_title(title: str | None) -> str:
    t = normalize_text(title)
    t = re.sub(
        r"\b(?:remix|rmx|mix|live|instrumental|karaoke|acoustic|edit|version|"
        r"extended|radio|club|demo|cover)\b.*$",
        "",
        t,
    )
    return t.strip() or normalize_text(title)


def _language(row: pd.Series) -> str | None:
    text = " ".join(str(row.get(k) or "") for k in ("title_ar", "title_lat", "title"))
    if re.search(r"[\u0600-\u06ff]", text):
        return "ar"
    return "en" if text.strip() else None


def _legacy_row(namespace: str, value: str, rec_id: str | None,
                is_primary: bool = False, note: str | None = None) -> dict:
    return {
        "namespace": namespace,
        "value": str(value),
        "recording_id": rec_id,
        "is_primary": is_primary,
        "note": note,
    }


def build(config: Config, manifest_dir: Path, out_dir: Path,
          sources_dir: Path | None = None, cache_dir: Path | None = None) -> dict:
    ensure_dir(out_dir)
    files = pd.read_parquet(manifest_dir / "files.parquet")
    audio_index = pd.read_parquet(manifest_dir / "audio_index.parquet")
    snapshot = {}
    snap_path = manifest_dir / "snapshot.json"
    if snap_path.exists():
        import json

        snapshot = json.loads(snap_path.read_text())

    aliases_df, aliases = load_artist_aliases()
    sources = Path(sources_dir) if sources_dir else config.sources_dir
    yue2 = load_yue2(sources)
    inventory = yue2["inventory"]
    title_map = yue2["title_map"]
    corpus = yue2["corpus"]
    mcap_index, mcap_albums = load_mcap(sources)
    abc_tracks = load_abc_tracks(config, files, cache_dir=cache_dir)

    # ---- crosswalk indexes -------------------------------------------------
    inv_by_key: dict[str, dict] = {}
    for _, r in inventory.iterrows():
        inv_by_key[_key(r.get("album"), r.get("file"))] = r.to_dict()
    tm_by_key: dict[str, dict] = {}
    for _, r in title_map.iterrows():
        tm_by_key[_key(r.get("album"), r.get("file"))] = r.to_dict()

    corpus_by_key: dict[str, dict] = {}
    for r in corpus:
        parts = str(r.get("track_id", "")).split("/")
        if len(parts) >= 3:
            corpus_by_key[_key(parts[-2], parts[-1])] = r
    abc_by_sha: dict[str, dict] = {}
    for r in abc_tracks:
        if r.get("audio_sha256"):
            abc_by_sha[str(r["audio_sha256"])] = r
    mcap_tracks: dict[str, dict] = {}
    for album_key, payload in mcap_albums.items():
        parts = album_key.split("/")
        if len(parts) < 2 or parts[0] == "ARAB_MAQAM_ROCK":
            continue
        for t in (payload or {}).get("tracks", []) or []:
            mcap_tracks[_key(parts[1], t.get("file"))] = {
                **t, "mcap_album_key": album_key,
            }

    now = utcnow()
    recordings: list[dict] = []
    legacy: list[dict] = []
    album_rows: dict[str, dict] = {}
    artist_rows: dict[str, dict] = {}
    work_rows: dict[str, dict] = {}
    rec_work: list[dict] = []
    unresolved_seed: list[dict] = []

    # Fetch artist alias metadata.
    alias_meta = {r["name_lat"]: r.to_dict() for _, r in aliases_df.iterrows()}

    def register_artist(name: str, source: str) -> str:
        name = canonical_artist(name, aliases) if name else "Unknown"
        if name in alias_meta:
            meta = alias_meta[name]
            aid = meta["artist_id"]
            aliases_list = [x for x in str(meta.get("aliases", "")).split("|") if x]
        else:
            aid = make_artist_id(name)
            aliases_list = []
        if aid not in artist_rows:
            artist_rows[aid] = {
                "artist_id": aid,
                "name_lat": name,
                "name_ar": (alias_meta.get(name, {}) or {}).get("name_ar") or None,
                "aliases": aliases_list,
                "country": (alias_meta.get(name, {}) or {}).get("country") or None,
                "source": source,
                "created_at": now,
            }
        return aid

    # ---- one pass over unique audio ---------------------------------------
    for _, a in audio_index.iterrows():
        rel = a["relative_path"]
        parsed = parse_audio_path(rel, aliases)
        collection = parsed["collection"]
        artist_name = parsed["artist_name"] or "Unknown"
        aid = register_artist(artist_name, collection)
        sha = str(a["sha256"])
        key = _key(parsed.get("album_dir"), parsed.get("file_name"))
        tm = tm_by_key.get(key, {})
        inv = inv_by_key.get(key, {})
        abc = abc_by_sha.get(sha, {})
        corpus_row = corpus_by_key.get(key, {})
        mcap = mcap_tracks.get(key, {})

        title_lat = parsed.get("title")
        title_ar = None
        year = parsed.get("album_year")
        album_id_val = None
        work_key = None
        resolved = collection in {"discography_album", "youtube_ref"}

        if collection == "discography_album":
            album_title = parsed.get("album_title") or parsed.get("album_dir")
            album_id_val = album_id(aid, album_title, year)
            if album_id_val not in album_rows:
                album_rows[album_id_val] = {
                    "album_id": album_id_val,
                    "artist_id": aid,
                    "title": album_title,
                    "title_norm": normalize_text(album_title),
                    "year": year,
                    "album_type": infer_album_type(album_title),
                    "source_collection": collection,
                    "source_uri": f"DISCOGRAPHY/{parsed['artist_dir']}/{parsed['album_dir']}/",
                    "created_at": now,
                }
            if tm.get("decision") == "match" and tm.get("lyric_title"):
                title_ar = str(tm["lyric_title"])
                work_key = normalize_text(title_ar)
            else:
                work_key = _base_work_title(title_lat)
            if not title_lat and tm.get("title_lat"):
                title_lat = str(tm["title_lat"])
        elif collection == "youtube_ref":
            if title_lat:
                work_key = _base_work_title(title_lat)
        elif collection in {"spotify_p0", "spotify_pd"}:
            unresolved_seed.append({
                "entity_type": "recording",
                "entity_id": recording_id(sha, rel),
                "collection": collection,
                "title": None,
                "reason": "no_metadata",
                "details": {"relative_path": rel},
            })

        # Duration/codec: prefer ffprobe, then source metadata.
        duration = a.get("duration_s")
        if pd.isna(duration) or duration is None:
            for candidate in (
                abc.get("duration_s"),
                inv.get("duration_s"),
                corpus_row.get("duration_s"),
                mcap.get("duration_s"),
            ):
                try:
                    if candidate not in (None, ""):
                        duration = float(candidate)
                        break
                except (TypeError, ValueError):
                    pass
        duration = float(duration) if duration is not None and not pd.isna(duration) else None

        work_id_val = make_work_id(aid, work_key) if work_key else None
        rec = {
            "recording_id": recording_id(sha, rel),
            "work_id": work_id_val,
            "artist_id": aid,
            "album_id": album_id_val,
            "collection": collection,
            "legacy_track_id": corpus_row.get("track_id") or rel,
            "disc_no": parsed.get("disc_no"),
            "track_no": parsed.get("track_no"),
            "title_lat": title_lat,
            "title_ar": title_ar,
            "duration_s": duration,
            "audio_sha256": sha,
            "audio_md5": a.get("md5"),
            "audio_crc32c": a.get("crc32c"),
            "audio_bucket": config.bucket,
            "audio_path": rel,
            "codec": a.get("codec"),
            "bitrate_kbps": a.get("bitrate_kbps") if not pd.isna(a.get("bitrate_kbps")) else None,
            "bytes": int(a["bytes"]) if not pd.isna(a.get("bytes")) else None,
            "is_instrumental": bool(
                parsed.get("is_instrumental")
                or str(inv.get("instrumental", "")).lower() in {"true", "1"}
            ),
            "is_generated": False,
            "version_type": parsed.get("version_type") or "unknown",
            "recording_group_id": None,
            "audio_fingerprint": None,
            "source_license": "unknown",
            "created_at": now,
        }
        recordings.append(rec)

        if work_id_val:
            if work_id_val not in work_rows:
                work_rows[work_id_val] = {
                    "work_id": work_id_val,
                    "artist_id": aid,
                    "title_lat": title_lat,
                    "title_ar": title_ar,
                    "title_norm": work_key,
                    "year": year,
                    "language": _language(pd.Series(rec)),
                    "dialect": None,
                    "genre": None,
                    "maqam": None,
                    "is_medley": False,
                    "created_at": now,
                }
            else:
                w = work_rows[work_id_val]
                if not w.get("title_ar") and title_ar:
                    w["title_ar"] = title_ar
                if not w.get("year") and year:
                    w["year"] = year
                # Keep the shortest Latin title as the display title.
                if title_lat and (not w.get("title_lat") or len(title_lat) < len(w["title_lat"])):
                    w["title_lat"] = title_lat
            rec_work.append({
                "recording_id": rec["recording_id"],
                "work_id": work_id_val,
                "position": 1,
                "is_medley": False,
            })

        # ---- legacy ids ----------------------------------------------------
        legacy.append(_legacy_row("gcs", rel, rec["recording_id"], True))
        if inv.get("track_id"):
            legacy.append(_legacy_row("yue2", inv["track_id"], rec["recording_id"], True))
        if abc.get("track_id"):
            legacy.append(_legacy_row("abc", abc["track_id"], rec["recording_id"], True,
                                      abc.get("abc_path")))
        if corpus_row.get("track_id"):
            legacy.append(_legacy_row("srt", corpus_row["track_id"], rec["recording_id"], True))
        if mcap.get("mcap_album_key"):
            mcap_key = f"{mcap['mcap_album_key']}/{mcap.get('file')}"
            legacy.append(_legacy_row("mcap", mcap_key, rec["recording_id"], True))
        if parsed.get("youtube_id"):
            legacy.append(_legacy_row("youtube", parsed["youtube_id"], rec["recording_id"], True))
        stem = parsed["file_name"].rsplit(".", 1)[0]
        m = re.match(r"^\d+-([0-9a-fA-F]{8}-[0-9a-fA-F-]{27,})$", stem)
        if m:
            legacy.append(_legacy_row("spotify", m.group(1).lower(), rec["recording_id"], True))

    # Add duplicate GCS copies as legacy ids on the canonical recording.
    for _, row in files[files["is_audio"].fillna(False)].iterrows():
        if not row.get("duplicate_of"):
            continue
        canonical = files[files["relative_path"] == row["duplicate_of"]]
        if canonical.empty:
            continue
        sha = canonical.iloc[0]["sha256"]
        rec_id = recording_id(str(sha), str(canonical.iloc[0]["relative_path"]))
        legacy.append(_legacy_row("gcs", str(row["relative_path"]), rec_id, False,
                                  "duplicate_copy"))

    artists = pd.DataFrame(artist_rows.values())
    albums = pd.DataFrame(album_rows.values())
    works = pd.DataFrame(work_rows.values())
    recordings_df = pd.DataFrame(recordings)
    legacy_df = pd.DataFrame(legacy).drop_duplicates(["namespace", "value"], keep="first")
    recording_works = pd.DataFrame(rec_work).drop_duplicates(["recording_id", "work_id"])

    # A recording without a work must have a reason.
    unresolved = pd.DataFrame(unresolved_seed)
    if not unresolved.empty:
        unresolved = unresolved.drop_duplicates(["entity_type", "entity_id", "reason"])

    write_parquet(conform_table(artists, "artists"), out_dir / "artists.parquet")
    write_parquet(conform_table(albums, "albums"), out_dir / "albums.parquet")
    write_parquet(conform_table(works, "works"), out_dir / "works.parquet")
    write_parquet(conform_table(recordings_df, "recordings"), out_dir / "recordings.parquet")
    write_parquet(conform_table(legacy_df, "legacy_ids"), out_dir / "legacy_ids.parquet")
    write_parquet(conform_table(recording_works, "recording_works"),
                  out_dir / "recording_works.parquet")
    write_parquet(unresolved, out_dir / "unresolved.parquet")

    summary = {
        "counts": {
            "artists": int(len(artists)),
            "albums": int(len(albums)),
            "works": int(len(works)),
            "recordings": int(len(recordings_df)),
            "legacy_ids": int(len(legacy_df)),
            "unresolved": int(len(unresolved)),
        },
        "by_collection": recordings_df["collection"].value_counts().to_dict(),
        "recordings_without_work": int(recordings_df["work_id"].isna().sum()),
        "snapshot_id": snapshot.get("snapshot_id"),
        "pipeline_version": config.pipeline_version,
    }
    write_json(summary, out_dir / "summary.json")
    print(
        f"[build_core] artists={len(artists)} albums={len(albums)} works={len(works)} "
        f"recordings={len(recordings_df)} legacy_ids={len(legacy_df)}",
        flush=True,
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config/settings.yaml")
    ap.add_argument("--manifest", default="build/manifest")
    ap.add_argument("--out", default="build/core")
    ap.add_argument("--sources", default=None)
    ap.add_argument("--cache-dir", default="build/cache/gcs")
    args = ap.parse_args(argv)
    config = load_config(args.config)
    build(
        config,
        manifest_dir=Path(args.manifest),
        out_dir=Path(args.out),
        sources_dir=Path(args.sources) if args.sources else None,
        cache_dir=Path(args.cache_dir) if args.cache_dir else None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
