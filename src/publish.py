"""Phase 7 — publish the immutable text-only dataset.

Two destinations are supported:
  * a local directory (committed to GitHub)
  * gs://<bucket>/<output_prefix> (optional, requires write access)

The GCS prefix is immutable: publishing aborts if any object already exists
unless --force is given.  `_SUCCESS` is written last.

Usage:
    python -m src.publish --dataset build/dataset/v0.1 \
        --dest gs://akbar-december-2024-backup/DATASET/v0.1
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from .common import Config, ensure_dir, get_storage_client, load_config, write_json


MEDIA_EXT = {
    ".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".aac",
    ".jpg", ".jpeg", ".png", ".webp", ".gif",
}


def _check_dataset(dataset_dir: Path) -> list[Path]:
    files = [p for p in dataset_dir.rglob("*") if p.is_file()]
    media = [p for p in files if p.suffix.lower() in MEDIA_EXT]
    if media:
        raise SystemExit(f"refusing to publish media files: {[str(p) for p in media[:5]]}")
    if not (dataset_dir / "manifest.json").exists():
        raise SystemExit("dataset is missing manifest.json")
    if not (dataset_dir / "_SUCCESS").exists():
        raise SystemExit("dataset is missing _SUCCESS")
    return files


def publish_local(dataset_dir: Path, local_dir: Path, force: bool = False) -> None:
    if local_dir.exists():
        if not force:
            raise SystemExit(f"{local_dir} already exists; use --force to replace")
        shutil.rmtree(local_dir)
    ensure_dir(local_dir)
    for p in sorted(dataset_dir.rglob("*")):
        if p.is_file():
            rel = p.relative_to(dataset_dir)
            target = local_dir / rel
            ensure_dir(target.parent)
            shutil.copy2(p, target)
    print(f"[publish] copied {dataset_dir} -> {local_dir}", flush=True)


def _parse_gs(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://"):
        raise SystemExit(f"not a gs:// URI: {uri}")
    rest = uri[len("gs://"):]
    bucket, _, prefix = rest.partition("/")
    return bucket, prefix.rstrip("/") + "/"


def publish_gcs(config: Config, dataset_dir: Path, dest: str, force: bool = False) -> dict:
    bucket_name, prefix = _parse_gs(dest)
    client = get_storage_client(config)
    bucket = client.bucket(bucket_name)
    files = _check_dataset(dataset_dir)
    existing = [b.name for b in bucket.list_blobs(prefix=prefix) if b.name != prefix]
    if existing and not force:
        raise SystemExit(
            f"destination is not empty ({len(existing)} objects); "
            "refusing to overwrite an immutable release (use --force to override)"
        )
    uploaded = []
    for p in sorted(files):
        rel = p.relative_to(dataset_dir).as_posix()
        if rel == "_SUCCESS":
            continue
        blob = bucket.blob(prefix + rel)
        blob.upload_from_filename(str(p))
        uploaded.append(prefix + rel)
    success = bucket.blob(prefix + "_SUCCESS")
    success.upload_from_string("ok\n", content_type="text/plain")
    uploaded.append(prefix + "_SUCCESS")
    info = {
        "destination": dest,
        "files_uploaded": len(uploaded),
        "objects": uploaded,
    }
    print(f"[publish] uploaded {len(uploaded)} objects to {dest}", flush=True)
    return info


def publish(
    config: Config,
    dataset_dir: Path,
    local_dir: Path | None = None,
    dest: str | None = None,
    force: bool = False,
    skip_gcs: bool = False,
) -> dict:
    _check_dataset(dataset_dir)
    info = {"dataset": str(dataset_dir)}
    if local_dir:
        publish_local(dataset_dir, local_dir, force=force)
        info["local_dir"] = str(local_dir)
    if dest and not skip_gcs:
        info.update(publish_gcs(config, dataset_dir, dest, force=force))
    # Keep publish metadata outside the immutable release directory.
    record = dataset_dir.parent.parent / "report" / "publish.json"
    write_json(info, record)
    return info


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config/settings.yaml")
    ap.add_argument("--dataset", default="build/dataset/v0.1")
    ap.add_argument("--dest", default=None)
    ap.add_argument("--local-dir", default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--skip-gcs", action="store_true")
    args = ap.parse_args(argv)
    config = load_config(args.config)
    publish(
        config,
        Path(args.dataset),
        local_dir=Path(args.local_dir) if args.local_dir else None,
        dest=args.dest,
        force=args.force,
        skip_gcs=args.skip_gcs,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
