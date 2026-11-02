"""Phase 1b — clone the pinned GitHub sources and record their files.

The clones live in build/sources (gitignored).  Every file is hashed with
SHA-256 so the dataset manifest can prove exactly which revision was read.

Usage:
    python -m src.scan_github --config config/settings.yaml
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd

from .common import (
    Config,
    ensure_dir,
    load_config,
    utcnow,
    write_json,
    write_parquet,
)


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    # Never put a token in argv.  Public repos work without one; private clones
    # can use a bearer header supplied through git's config environment.
    token = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN")
    if token and any("github.com" in c for c in cmd):
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
        env["GIT_CONFIG_VALUE_0"] = "Authorization: Bearer " + token
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def ensure_repo(url: str, commit: str, dest: Path) -> str:
    """Fetch exactly `commit` into `dest`; return the checked-out sha."""
    if (dest / ".git").exists():
        head = _run(["git", "rev-parse", "HEAD"], cwd=dest, check=False).stdout.strip()
        if head == commit:
            return head
    else:
        ensure_dir(dest.parent)
        _run(["git", "init", "-q", str(dest)])
        _run(["git", "remote", "add", "origin", url], cwd=dest)
    # Fetch the exact commit (works even when it is no longer the branch head).
    _run(["git", "fetch", "-q", "--depth", "1", "origin", commit], cwd=dest)
    _run(["git", "checkout", "-q", "--detach", commit], cwd=dest)
    head = _run(["git", "rev-parse", "HEAD"], cwd=dest).stdout.strip()
    if head != commit:
        raise RuntimeError(f"{url}: wanted {commit}, got {head}")
    return head


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _md5_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def scan(config: Config, out_dir: Path, sources_dir: Path | None = None) -> dict[str, str]:
    ensure_dir(out_dir)
    base = Path(sources_dir) if sources_dir else config.sources_dir
    ensure_dir(base)
    scanned_at = utcnow()
    commits: dict[str, str] = {}
    rows: list[dict] = []

    for key, repo in config.repos.items():
        url = repo["url"]
        commit = repo["commit"]
        dest = base / key
        print(f"[scan_github] {key}: {url} @ {commit[:12]}", flush=True)
        head = ensure_repo(url, commit, dest)
        commits[key] = head
        repo_uri = url.removesuffix(".git")
        for p in sorted(dest.rglob("*")):
            if not p.is_file() or ".git" in p.parts:
                continue
            rel = p.relative_to(dest).as_posix()
            size = p.stat().st_size
            sha = _sha256_file(p)
            rows.append({
                "file_id": "fil_" + sha[:16],
                "source": "github",
                "bucket": None,
                "repo": url,
                "commit_sha": head,
                "relative_path": rel,
                "uri": f"{repo_uri}/blob/{head}/{rel}",
                "bytes": size,
                "sha256": sha,
                "md5": _md5_file(p),
                "crc32c": None,
                "content_type": None,
                "generation": None,
                "etag": None,
                "mtime": pd.Timestamp(p.stat().st_mtime, unit="s").to_pydatetime(),
                "scanned_at": scanned_at,
                "is_audio": False,
                "is_image": p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"},
                "collection": key,
                "artist_dir": None,
                "album_dir": None,
                "file_name": p.name,
                "title": None,
                "artist_name": None,
                "album_title": None,
                "album_year": None,
                "disc_no": None,
                "track_no": None,
                "youtube_id": None,
                "version_type": None,
                "is_instrumental": False,
                "is_generated": key == "mcap" and "ARAB_MAQAM_ROCK" in rel,
                "duration_s": None,
                "codec": None,
                "bitrate_kbps": None,
                "channels": None,
                "sample_rate": None,
            })

    gh = pd.DataFrame(rows)
    write_parquet(gh, out_dir / "github_files.parquet")

    gcs_path = out_dir / "files.parquet"
    gcs = pd.read_parquet(gcs_path) if gcs_path.exists() else pd.DataFrame()
    combined = pd.concat([gcs, gh], ignore_index=True) if not gcs.empty else gh
    combined = combined.sort_values(["source", "relative_path"]).reset_index(drop=True)
    write_parquet(combined, out_dir / "files.parquet")

    # Update snapshot.json with the combined manifest hash.
    snap_path = out_dir / "snapshot.json"
    import json

    snap = json.loads(snap_path.read_text()) if snap_path.exists() else {}
    payload_rows = combined[["source", "relative_path", "sha256", "generation", "bytes"]]
    payload_rows = payload_rows.sort_values(["source", "relative_path"])
    payload = "\n".join(
        "|".join("" if pd.isna(v) else str(v) for v in row)
        for row in payload_rows.itertuples(index=False, name=None)
    )
    snap["gcs_snapshot_id"] = snap.get("snapshot_id")
    snap["snapshot_id"] = "snap_" + hashlib.sha256(payload.encode()).hexdigest()[:24]
    snap["source_commits"] = commits
    snap["github_repos"] = {k: v["url"] for k, v in config.repos.items()}
    snap.setdefault("counts", {})
    snap["counts"]["github_files"] = int(len(gh))
    snap["counts"]["total_files"] = int(len(combined))
    write_json(snap, snap_path)
    print(
        f"[scan_github] wrote {len(gh)} GitHub files; snapshot={snap['snapshot_id']}",
        flush=True,
    )
    return commits


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config/settings.yaml")
    ap.add_argument("--out", default="build/manifest")
    ap.add_argument("--sources-dir", default=None)
    args = ap.parse_args(argv)
    config = load_config(args.config)
    scan(
        config,
        out_dir=Path(args.out),
        sources_dir=Path(args.sources_dir) if args.sources_dir else None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
