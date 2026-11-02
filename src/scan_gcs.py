"""Phase 1a — scan GCS and build the file manifest.

* Lists every object under the configured prefixes.
* Streams each object through SHA-256 (never writes audio bytes anywhere).
* For audio, runs ffprobe on the same stream to capture duration/codec/bitrate.
* Parses DISCOGRAPHY paths into collection / artist / album / file.
* Detects byte-identical duplicates by SHA-256 (MD5 is kept as a cross-check).
* Writes build/manifest/{files.parquet,audio_index.parquet,snapshot.json}.

Usage:
    python -m src.scan_gcs --config config/settings.yaml
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from .common import (
    AUDIO_EXTENSIONS,
    IMAGE_EXTENSIONS,
    Config,
    choose_canonical,
    classify_collection,
    conform_table,
    ensure_dir,
    get_bucket,
    hash_blob,
    load_artist_aliases,
    load_config,
    parse_audio_path,
    probe_gcs_url,
    probe_metadata_to_fields,
    recording_id,
    sha256_bytes,
    stream_hash_and_probe,
    utcnow,
    write_json,
    write_parquet,
)

TEXT_CACHE_EXTENSIONS = {
    ".json", ".jsonl", ".srt", ".vtt", ".csv", ".md", ".txt",
}


def _file_id(source: str, rel: str) -> str:
    return "fil_" + sha256_bytes(f"{source}\x1f{rel}".encode("utf-8"))[:16]


def _is_audio(name: str, content_type: str | None) -> bool:
    ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return (content_type or "").startswith("audio/") or ext in AUDIO_EXTENSIONS


def _is_image(name: str, content_type: str | None) -> bool:
    ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return (content_type or "").startswith("image/") or ext in IMAGE_EXTENSIONS


def scan(
    config: Config,
    out_dir: Path,
    cache_dir: Path | None,
    do_hash: bool = True,
    do_probe: bool = True,
    resume: bool = True,
    offline: bool = False,
) -> None:
    ensure_dir(out_dir)
    bucket = get_bucket(config)
    _, aliases = load_artist_aliases()

    previous: dict[str, dict] = {}
    prev_path = out_dir / "files.parquet"
    if resume and prev_path.exists():
        prev = pd.read_parquet(prev_path)
        prev = prev[prev["source"] == "gcs"]
        for _, row in prev.iterrows():
            if row.get("sha256"):
                previous[str(row["relative_path"])] = row.to_dict()
        print(f"[scan_gcs] resume cache: {len(previous)} objects", flush=True)

    prefixes = list(config.prefixes.values())
    rows: list[dict] = []
    scanned_at = utcnow()
    n = 0
    for prefix in prefixes:
        print(f"[scan_gcs] listing {prefix}", flush=True)
        blobs = list(bucket.list_blobs(prefix=prefix))
        print(f"[scan_gcs]   {len(blobs)} objects", flush=True)
        for blob in blobs:
            n += 1
            name = blob.name
            if name.endswith("/"):
                continue
            audio = _is_audio(name, blob.content_type)
            image = _is_image(name, blob.content_type)
            sha = md5 = crc32c = None
            probe: dict = {}
            meta: dict = {}
            cached = previous.get(name)
            if cached and cached.get("sha256") and cached.get("bytes") == blob.size:
                sha = cached.get("sha256")
                probe = cached.get("_probe") or {}
                # Reuse the previously extracted metadata directly.
                meta = {
                    "duration_s": cached.get("duration_s"),
                    "codec": cached.get("codec"),
                    "bitrate_kbps": cached.get("bitrate_kbps"),
                    "channels": cached.get("channels"),
                    "sample_rate": cached.get("sample_rate"),
                }
            else:
                if do_hash:
                    if offline:
                        raise RuntimeError(
                            f"offline mode but {name} is not in the resume cache"
                        )
                    if audio:
                        sha, probe = stream_hash_and_probe(blob, probe=do_probe)
                    else:
                        sha = hash_blob(blob)
            if cached:
                md5 = cached.get("md5")
                crc32c = cached.get("crc32c")
            md5 = md5 or blob.md5_hash
            crc32c = crc32c or blob.crc32c

            parsed = parse_audio_path(name, aliases) if audio else {
                "collection": classify_collection(name),
                "artist_dir": name.split("/")[1] if "/" in name else None,
                "album_dir": name.split("/")[-2] if name.count("/") >= 2 else None,
                "file_name": name.rsplit("/", 1)[-1],
                "title": None,
                "artist_name": None,
                "youtube_id": None,
                "album_title": None,
                "album_year": None,
                "disc_no": None,
                "track_no": None,
                "version_type": None,
                "is_instrumental": False,
                "is_generated": False,
            }

            def _missing_duration(v) -> bool:
                return v is None or v == "" or (isinstance(v, float) and pd.isna(v))

            if audio and _missing_duration(meta.get("duration_s")) and probe:
                meta = probe_metadata_to_fields(probe)
            if audio and _missing_duration(meta.get("duration_s")) and not offline:
                # ffprobe cannot determine MP3 duration from a non-seekable
                # pipe.  A direct range request gives it a seekable URL.
                extra = probe_gcs_url(config, name, config.bucket)
                if extra:
                    extra_fields = probe_metadata_to_fields(extra)
                    for k, v in extra_fields.items():
                        if v is not None:
                            meta[k] = v
            elif not audio:
                meta = {}
            row = {
                "file_id": _file_id("gcs", name),
                "source": "gcs",
                "bucket": config.bucket,
                "repo": None,
                "commit_sha": None,
                "relative_path": name,
                "uri": f"gs://{config.bucket}/{name}",
                "bytes": int(blob.size or 0),
                "sha256": sha,
                "md5": md5,
                "crc32c": crc32c,
                "content_type": blob.content_type,
                "generation": str(blob.generation) if blob.generation else None,
                "etag": blob.etag,
                "mtime": blob.updated.replace(tzinfo=None) if blob.updated else None,
                "scanned_at": scanned_at,
                "is_audio": audio,
                "is_image": image,
                "collection": parsed["collection"],
                "artist_dir": parsed["artist_dir"],
                "album_dir": parsed["album_dir"],
                "file_name": parsed["file_name"],
                "title": parsed["title"],
                "artist_name": parsed["artist_name"],
                "album_title": parsed["album_title"],
                "album_year": parsed["album_year"],
                "disc_no": parsed["disc_no"],
                "track_no": parsed["track_no"],
                "youtube_id": parsed["youtube_id"],
                "version_type": parsed["version_type"],
                "is_instrumental": parsed["is_instrumental"],
                "is_generated": parsed["is_generated"],
                "duration_s": meta.get("duration_s"),
                "codec": meta.get("codec"),
                "bitrate_kbps": meta.get("bitrate_kbps"),
                "channels": meta.get("channels"),
                "sample_rate": meta.get("sample_rate"),
            }
            rows.append(row)

            # Cache small text artifacts for later phases.
            if cache_dir is not None and not audio and not image:
                ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
                if ext in TEXT_CACHE_EXTENSIONS and int(blob.size or 0) < 8 * 1024 * 1024:
                    from .common import ensure_dir as _ensure

                    target = cache_dir / name
                    _ensure(target.parent)
                    if not target.exists():
                        blob.download_to_filename(str(target))

            if n % 50 == 0:
                print(f"[scan_gcs]   hashed {n} objects...", flush=True)

    df = pd.DataFrame(rows)
    if df.empty:
        raise SystemExit("no objects found")

    # Duplicate detection: SHA-256 first, MD5 as a cross-check.
    df["duplicate_group"] = None
    df["duplicate_of"] = None
    if "sha256" in df:
        valid = df["sha256"].notna() & (df["sha256"] != "")
        for sha, group in df[valid].groupby("sha256", sort=False):
            if len(group) <= 1:
                continue
            idx = group.index
            df.loc[idx, "duplicate_group"] = sha[:16]
            # Prefer an album path as canonical; then TODO/ajam, p0, pd.
            canonical = choose_canonical(group)
            for i in idx:
                if i != canonical.name:
                    df.at[i, "duplicate_of"] = canonical["relative_path"]

    # Audio index: one row per unique recording.
    audio_df = df[df["is_audio"].fillna(False)].copy()
    index_rows: list[dict] = []
    for sha, group in audio_df.groupby("sha256", sort=False):
        canonical = choose_canonical(group)
        copies = sorted(group["relative_path"].tolist())
        index_rows.append({
            "recording_id": recording_id(sha, canonical["relative_path"]),
            "bucket": config.bucket,
            "relative_path": canonical["relative_path"],
            "audio_path": canonical["relative_path"],
            "sha256": sha,
            "md5": canonical.get("md5"),
            "crc32c": canonical.get("crc32c"),
            "bytes": int(canonical.get("bytes") or 0),
            "content_type": canonical.get("content_type"),
            "generation": canonical.get("generation"),
            "duration_s": canonical.get("duration_s"),
            "codec": canonical.get("codec"),
            "bitrate_kbps": canonical.get("bitrate_kbps"),
            "channels": canonical.get("channels"),
            "sample_rate": canonical.get("sample_rate"),
            "collection": canonical.get("collection"),
            "title": canonical.get("title"),
            "n_copies": len(copies),
            "duplicate_paths": copies,
        })
    audio_index = pd.DataFrame(index_rows)

    # Snapshot id: deterministic over path + hash + generation + size.
    snap_src = df[["source", "relative_path", "sha256", "generation", "bytes"]].copy()
    snap_src = snap_src.sort_values(["source", "relative_path"])
    snap_payload = "\n".join(
        "|".join("" if pd.isna(v) else str(v) for v in row)
        for row in snap_src.itertuples(index=False, name=None)
    )
    snapshot_id = "snap_" + sha256_bytes(snap_payload.encode("utf-8"))[:24]

    manifest = df.copy()
    write_parquet(manifest, out_dir / "files.parquet")
    write_parquet(audio_index, out_dir / "audio_index.parquet")
    # Keep only schema columns in the published-shaped files table?  The
    # manifest intentionally carries the parsed fields; package_dataset drops
    # the extras when writing the final `files.parquet`.
    write_json(
        {
            "snapshot_id": snapshot_id,
            "scanned_at": scanned_at.isoformat(),
            "bucket": config.bucket,
            "prefixes": prefixes,
            "counts": {
                "objects": int(len(df)),
                "audio_objects": int(df["is_audio"].fillna(False).sum()),
                "image_objects": int(df["is_image"].fillna(False).sum()),
                "text_objects": int(len(df) - df["is_audio"].fillna(False).sum() - df["is_image"].fillna(False).sum()),
                "unique_audio": int(len(audio_index)),
                "duplicate_audio_objects": int(audio_df["duplicate_of"].notna().sum()),
            },
            "audio_bytes": int(audio_df["bytes"].fillna(0).sum()),
            "unique_audio_bytes": int(audio_index["bytes"].fillna(0).sum()) if not audio_index.empty else 0,
            "pipeline_version": config.pipeline_version,
        },
        out_dir / "snapshot.json",
    )
    print(
        f"[scan_gcs] wrote {len(df)} files, {len(audio_index)} unique audio, "
        f"snapshot={snapshot_id}",
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config/settings.yaml")
    ap.add_argument("--out", default="build/manifest")
    ap.add_argument("--cache-dir", default="build/cache/gcs")
    ap.add_argument("--no-hash", action="store_true", help="metadata only (for smoke tests)")
    ap.add_argument("--no-probe", action="store_true", help="skip ffprobe (still hashes)")
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--offline", action="store_true", help="use the existing resume cache only")
    args = ap.parse_args(argv)

    config = load_config(args.config)
    scan(
        config,
        out_dir=Path(args.out),
        cache_dir=Path(args.cache_dir) if args.cache_dir else None,
        do_hash=not args.no_hash,
        do_probe=not args.no_probe,
        resume=not args.no_resume,
        offline=args.offline,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
