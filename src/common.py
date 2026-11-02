"""Shared helpers for the Dataset Discography pipeline.

Everything here is deterministic: IDs are content hashes, normalisation is
stable, and every path that leaves the pipeline is relative to its source
root (never a gs:// URI).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import pandas as pd
import yaml

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent


def load_yaml(path: str | Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@dataclass(frozen=True)
class Config:
    raw: dict
    path: Path

    @property
    def pipeline_version(self) -> str:
        return str(self.raw["pipeline_version"])

    @property
    def dataset_version(self) -> str:
        return str(self.raw["dataset_version"])

    @property
    def bucket(self) -> str:
        return self.raw["gcs"]["bucket"]

    @property
    def project(self) -> str | None:
        return self.raw["gcs"].get("project")

    @property
    def prefixes(self) -> dict[str, str]:
        return dict(self.raw["gcs"]["prefixes"])

    @property
    def output_prefix(self) -> str:
        return self.raw["gcs"]["output_prefix"]

    @property
    def repos(self) -> dict[str, dict]:
        return dict(self.raw["github"]["repos"])

    @property
    def sources_dir(self) -> Path:
        p = Path(self.raw["github"]["sources_dir"])
        return p if p.is_absolute() else ROOT / p

    @property
    def package(self) -> dict:
        return dict(self.raw["package"])

    @property
    def thresholds(self) -> dict:
        return dict(self.raw["thresholds"])


def load_config(path: str | Path = "config/settings.yaml") -> Config:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    return Config(load_yaml(p), p)


# ---------------------------------------------------------------------------
# Time / hashing / IO
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def stable_hash(*parts: Any, length: int = 16) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(str(part).encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()[:length]


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text or "unknown"


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def write_parquet(df: pd.DataFrame, path: str | Path) -> None:
    p = Path(path)
    ensure_dir(p.parent)
    df.to_parquet(p, index=False)


def write_json(obj: Any, path: str | Path, indent: int = 2) -> None:
    p = Path(path)
    ensure_dir(p.parent)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=indent, default=str)
        fh.write("\n")


def read_json(path: str | Path, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def read_jsonl(path: str | Path) -> list[dict]:
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_jsonl(rows: Iterable[dict], path: str | Path) -> None:
    p = Path(path)
    ensure_dir(p.parent)
    with open(p, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str))
            fh.write("\n")


def read_parquet(path: str | Path) -> pd.DataFrame:
    return pd.read_parquet(path)


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

_AR_DIACRITICS = re.compile(
    "[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed]"
)
_AR_TRANSLATION = str.maketrans(
    {
        "أ": "ا",
        "إ": "ا",
        "آ": "ا",
        "ٱ": "ا",
        "ى": "ي",
        "ئ": "ي",
        "ؤ": "و",
        "ة": "ه",
        "ـ": "",
    }
)
_LATIN_TRANSLATION = str.maketrans(
    {
        "’": "'",
        "‘": "'",
        "`": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
    }
)


def strip_arabic_diacritics(text: str) -> str:
    return _AR_DIACRITICS.sub("", text or "")


def normalize_text(text: str | None) -> str:
    """NFKC + Arabic/Latin fold + punctuation removal.  The match key."""
    if text is None:
        return ""
    text = unicodedata.normalize("NFKC", str(text))
    text = strip_arabic_diacritics(text)
    text = text.translate(_AR_TRANSLATION).translate(_LATIN_TRANSLATION)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = re.sub(r"_+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_for_path(text: str | None) -> str:
    """A slightly more aggressive key used for album/file crosswalks."""
    text = normalize_text(text)
    text = re.sub(r"^(?:cd|disc|disk)\s*\d+\s*", "", text)
    text = re.sub(r"^\d+[\s._-]+", "", text)
    text = re.sub(r"\b(19|20)\d{2}\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def clean_title(text: str | None) -> str:
    """Remove file prefixes, YouTube id suffixes and common noise."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", str(text))
    text = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", text)
    text = re.sub(r"^\s*\d{1,3}\s*[\.\-_ ]+\s*", "", text)
    text = re.sub(r"[ _]{1,3}[A-Za-z0-9_-]{11}\s*$", "", text)
    text = re.sub(
        r"\s*[\(\[]\s*(?:official\s*(?:hd\s*)?(?:music\s*)?video|official\s*audio|"
        r"lyrics?\s*video|audio|hd|hq|remastered(?:\s*\d{4})?)\s*[\)\]]",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = text.replace("_", " ")
    # Unbracketed YouTube noise, e.g. "Title Official Music Video".
    text = re.sub(
        r"\b(?:official\s*(?:hd\s*)?(?:music\s*)?video|official\s*audio|"
        r"lyrics?\s*video)\b",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s+-\s+", " - ", text)
    text = re.sub(r"\s+", " ", text).strip(" -_")
    return text


def extract_year(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", str(text))
    return int(m.group(1)) if m else None


def extract_track_no(text: str | None) -> tuple[int | None, str]:
    if not text:
        return None, ""
    m = re.match(r"^\s*(\d{1,3})\s*[\.\-_ ]+\s*(.+)$", str(text))
    if m:
        return int(m.group(1)), m.group(2).strip()
    return None, str(text).strip()


# A YouTube id is only recognised when it is explicitly delimited by the
# source naming conventions: `__<id>` or ` _<id>`.  This avoids mistaking an
# 11-character song word (e.g. `Abu_3_Daaat`) for a video id.
_YT_ID_TAIL = re.compile(r"(?:__| _)([A-Za-z0-9_-]{11})\s*$")


def extract_youtube_id(text: str | None) -> str | None:
    if not text:
        return None
    stem = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", str(text).strip())
    m = _YT_ID_TAIL.search(stem)
    return m.group(1) if m else None


def strip_youtube_id(text: str | None) -> str:
    if not text:
        return ""
    stem = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", str(text).strip())
    return re.sub(r"(?:__| _)[A-Za-z0-9_-]{11}\s*$", "", stem).strip(" _-")


def infer_version_type(title: str | None) -> str:
    t = normalize_text(title)
    if any(k in t for k in ("instrumental", "karaoke", "minus one")):
        return "instrumental"
    # Album rips commonly name instrumental takes "Music" or "Title (Music)".
    if re.search(r"(?:^|\s)music(?:\s|$)", t):
        return "instrumental"
    if "live" in t or "hafla" in t:
        return "live"
    if any(k in t for k in ("remix", "mix", "rmx", "edit", "version")):
        return "remix"
    return "studio"


def infer_album_type(album: str | None) -> str:
    t = normalize_text(album)
    if "hafla" in t or "live" in t:
        return "live"
    if "best of" in t or "rawae" in t or "compilation" in t:
        return "compilation"
    return "studio"


# ---------------------------------------------------------------------------
# IDs (see DESIGN.md §3.1)
# ---------------------------------------------------------------------------


def artist_id(name_lat: str) -> str:
    return "art_" + slugify(name_lat)


def album_id(artist: str, title: str, year: int | None) -> str:
    return "alb_" + stable_hash(artist, normalize_text(title), year)


def work_id(artist: str, title: str) -> str:
    return "w_" + stable_hash(artist, normalize_text(title))


def recording_id(audio_sha256: str | None, fallback: str) -> str:
    if audio_sha256:
        return "rec_" + audio_sha256[:16]
    return "rec_" + stable_hash(fallback)


def lyric_id(source: str, source_id: str) -> str:
    return "lyr_" + stable_hash(source, source_id)


def timing_id(rec_id: str, run_id: str, artifact_sha: str | None) -> str:
    return "tim_" + stable_hash(rec_id, run_id, artifact_sha or "")


def transcript_id(rec_id: str, model: str, revision: str, artifact_sha: str | None) -> str:
    return "trn_" + stable_hash(rec_id, model, revision, artifact_sha or "")


def caption_id(rec_id: str, model: str, revision: str, artifact_sha: str | None) -> str:
    return "cap_" + stable_hash(rec_id, model, revision, artifact_sha or "")


def link_id(src_type: str, src_id: str, dst_type: str, dst_id: str,
            relation: str, method: str) -> str:
    return "lnk_" + stable_hash(src_type, src_id, dst_type, dst_id, relation, method)


# ---------------------------------------------------------------------------
# Path parsing
# ---------------------------------------------------------------------------

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".aac"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

_ALBUM_PREFIX = re.compile(r"^\s*(\d{1,3})\s*[\.\-_ ]+\s*")
_ALBUM_YEAR_TAIL = re.compile(r"[\.\-_ ]+((?:19|20)\d{2})\s*$")
_ELISSA_ALBUM = re.compile(r"^\s*Elissa\s*-\s*((?:19|20)\d{2})\s+(.+)$", re.IGNORECASE)


def _album_parts(album_dir: str) -> tuple[str, int | None, int | None, str]:
    """Return (album_title, year, disc_no, raw_album_dir)."""
    raw = album_dir.strip()
    year = extract_year(raw)
    disc_no: int | None = None
    title = raw
    m = _ELISSA_ALBUM.match(raw)
    if m:
        year = int(m.group(1))
        title = m.group(2)
    else:
        m2 = _ALBUM_PREFIX.match(raw)
        if m2:
            disc_no = int(m2.group(1))
            title = raw[m2.end():]
        title = _ALBUM_YEAR_TAIL.sub("", title)
    title = title.replace("_", " ").strip(" ._-")
    return title, year, disc_no, raw


def classify_collection(relative_path: str) -> str:
    p = relative_path
    if p.startswith(("DISCOGRAPHY/ELISSA/", "DISCOGRAPHY/FADL_SHAKER/")):
        return "discography_album"
    if p.startswith("DISCOGRAPHY/p0/"):
        return "spotify_p0"
    if p.startswith("DISCOGRAPHY/pd/"):
        return "spotify_pd"
    if p.startswith(("DISCOGRAPHY/TODO/", "DISCOGRAPHY/ajam_major/")):
        return "youtube_ref"
    if p.startswith("arabmaqamrock/"):
        return "generated_arabmaqamrock"
    return "other"


def parse_artist_from_youtube(name: str, aliases: dict[str, str]) -> tuple[str, str]:
    """Return (canonical artist latin name, title) from a YouTube-ish filename."""
    stem = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", name)
    stem = strip_youtube_id(stem)
    stem = stem.replace("_", " ")
    stem = re.sub(r"\s+", " ", stem).strip()
    # Split on the first " - " if present.
    if " - " in stem:
        artist, title = stem.split(" - ", 1)
        return artist.strip(), clean_title(title)
    # Otherwise try the longest known alias as a prefix.
    norm = normalize_text(stem)
    best: tuple[int, str] | None = None
    for alias_norm, canonical in aliases.items():
        if norm.startswith(alias_norm + " "):
            if best is None or len(alias_norm) > best[0]:
                best = (len(alias_norm), canonical)
    if best:
        canonical = best[1]
        # Recover the title by matching the canonical alias length in words.
        words = stem.split()
        n_artist_words = len(canonical.split())
        # Prefer the actual matched alias word count.
        for alias_norm, canon in aliases.items():
            if canon == canonical and normalize_text(" ".join(words[:len(alias_norm.split())])) == alias_norm:
                return canonical, clean_title(" ".join(words[len(alias_norm.split()):]))
        return canonical, clean_title(" ".join(words[n_artist_words:]))
    return "Unknown", clean_title(stem)


def parse_audio_path(relative_path: str, aliases: dict[str, str] | None = None) -> dict:
    """Parse a DISCOGRAPHY relative path into catalog fields.

    `relative_path` must start with DISCOGRAPHY/ (case preserved).
    """
    aliases = aliases or {}
    parts = relative_path.split("/")
    collection = classify_collection(relative_path)
    file_name = parts[-1]
    ext = "." + file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    out: dict[str, Any] = {
        "collection": collection,
        "relative_path": relative_path,
        "artist_dir": None,
        "album_dir": None,
        "album_title": None,
        "album_year": None,
        "disc_no": None,
        "track_no": None,
        "file_name": file_name,
        "file_ext": ext,
        "title": None,
        "artist_name": None,
        "youtube_id": None,
        "is_generated": False,
    }

    if collection == "discography_album":
        artist_dir = parts[1]
        album_dir = parts[-2]
        title_raw = file_name
        track_no, title_rest = extract_track_no(re.sub(r"\.[A-Za-z0-9]{2,5}$", "", file_name))
        album_title, year, disc_no, _ = _album_parts(album_dir)
        if artist_dir.upper() == "ELISSA":
            artist_name = "Elissa"
        elif artist_dir.upper() == "FADL_SHAKER":
            artist_name = "Fadl Shaker"
        else:
            artist_name = artist_dir.replace("_", " ").title()
        out.update(
            artist_dir=artist_dir,
            album_dir=album_dir,
            album_title=album_title,
            album_year=year,
            disc_no=disc_no,
            track_no=track_no,
            title=clean_title(title_rest),
            artist_name=artist_name,
        )
    elif collection in {"spotify_p0", "spotify_pd"}:
        out.update(
            artist_dir=parts[1],
            file_name=file_name,
            title=None,
            artist_name="Unknown (Spotify)",
        )
    elif collection == "youtube_ref":
        artist_name, title = parse_artist_from_youtube(file_name, aliases)
        out.update(
            artist_dir=parts[1],
            title=title,
            artist_name=artist_name,
            youtube_id=extract_youtube_id(file_name),
        )
    else:
        out.update(artist_name="Unknown", title=clean_title(file_name))

    if out["title"]:
        out["version_type"] = infer_version_type(out["title"])
        out["is_instrumental"] = out["version_type"] == "instrumental"
    else:
        out["version_type"] = "unknown"
        out["is_instrumental"] = False
    return out


def gcs_uri_to_relative(uri: str | None) -> str | None:
    if not uri:
        return None
    prefix = "gs://"
    if not uri.startswith(prefix):
        return uri
    rest = uri[len(prefix):]
    if "/" not in rest:
        return ""
    return rest.split("/", 1)[1]


def relative_to_bucket(path: str | None) -> str | None:
    """Accept gs://bucket/path or a relative path."""
    if not path:
        return None
    if path.startswith("gs://"):
        return gcs_uri_to_relative(path)
    return path.lstrip("/")


# ---------------------------------------------------------------------------
# Artist aliases
# ---------------------------------------------------------------------------


def load_artist_aliases(path: str | Path = "config/artist_aliases.csv") -> tuple[pd.DataFrame, dict[str, str]]:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    df = pd.read_csv(p, dtype=str).fillna("")
    lookup: dict[str, str] = {}
    for _, row in df.iterrows():
        names = [row["name_lat"], row["name_ar"]]
        names += [x.strip() for x in str(row.get("aliases", "")).split("|")]
        for name in names:
            norm = normalize_text(name)
            if norm:
                lookup[norm] = row["name_lat"]
    return df, lookup


def canonical_artist(name: str | None, aliases: dict[str, str]) -> str:
    if not name:
        return "Unknown"
    norm = normalize_text(name)
    if norm in aliases:
        return aliases[norm]
    # Try a containment match for e.g. "Fadl Shaker.Full Discography".
    for alias_norm, canonical in sorted(aliases.items(), key=lambda kv: -len(kv[0])):
        if alias_norm and (alias_norm in norm or norm in alias_norm):
            return canonical
    return name.strip()


# ---------------------------------------------------------------------------
# GCS access / caching
# ---------------------------------------------------------------------------


def get_storage_client(config: Config):
    from google.cloud import storage

    return storage.Client(project=config.project)


def get_bucket(config: Config):
    return get_storage_client(config).bucket(config.bucket)


def gcs_cache_path(cache_dir: str | Path, relative_path: str) -> Path:
    return Path(cache_dir) / relative_path


def read_gcs_bytes(
    config: Config,
    relative_path: str,
    cache_dir: str | Path | None = None,
    bucket=None,
) -> bytes:
    """Read an object, optionally caching it on local disk."""
    if cache_dir is not None:
        cached = gcs_cache_path(cache_dir, relative_path)
        if cached.exists():
            return cached.read_bytes()
    bucket = bucket or get_bucket(config)
    blob = bucket.blob(relative_path)
    data = blob.download_as_bytes()
    if cache_dir is not None:
        cached = gcs_cache_path(cache_dir, relative_path)
        ensure_dir(cached.parent)
        cached.write_bytes(data)
    return data


def read_gcs_text(
    config: Config,
    relative_path: str,
    cache_dir: str | Path | None = None,
    bucket=None,
) -> str:
    return read_gcs_bytes(config, relative_path, cache_dir, bucket).decode("utf-8", errors="replace")


def read_gcs_json(
    config: Config,
    relative_path: str,
    cache_dir: str | Path | None = None,
    bucket=None,
) -> Any:
    return json.loads(read_gcs_text(config, relative_path, cache_dir, bucket))


def stream_hash_and_probe(
    blob,
    probe: bool = True,
    chunk_size: int = 1024 * 1024,
) -> tuple[str, dict]:
    """Stream one GCS blob through SHA-256 and (optionally) ffprobe.

    No file is written.  Returns (sha256, ffprobe_dict).
    """
    sha = hashlib.sha256()
    proc = None
    if probe:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration,bit_rate,format_name:stream=codec_name,bit_rate,channels,sample_rate",
            "-of", "json", "-i", "pipe:0",
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            proc = None

    with blob.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            sha.update(chunk)
            if proc is not None and proc.stdin is not None:
                try:
                    proc.stdin.write(chunk)
                except BrokenPipeError:
                    proc = None
    if proc is not None:
        try:
            out, _err = proc.communicate(timeout=120)
        except Exception:
            proc.kill()
            out = b""
        try:
            meta = json.loads(out.decode("utf-8", errors="replace") or "{}")
        except json.JSONDecodeError:
            meta = {}
    else:
        meta = {}
    return sha.hexdigest(), meta


def hash_blob(blob, chunk_size: int = 1024 * 1024) -> str:
    sha = hashlib.sha256()
    with blob.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            sha.update(chunk)
    return sha.hexdigest()


def _access_token() -> str | None:
    """Return a short-lived OAuth token for direct GCS range reads."""
    try:
        from google.auth import default
        from google.auth.transport.requests import Request

        creds, _ = default()
        if getattr(creds, "requires_scopes", False):
            creds = creds.with_scopes_if_required(
                creds, ["https://www.googleapis.com/auth/cloud-platform"]
            )
        if not creds.valid:
            creds.refresh(Request())
        return creds.token
    except Exception:
        return None


def probe_gcs_url(config: Config, relative_path: str, bucket: str | None = None) -> dict:
    """Probe an object through GCS range requests (works for mp3 duration)."""
    from urllib.parse import quote

    token = _access_token()
    if not token:
        return {}
    bucket = bucket or config.bucket
    url = f"https://storage.googleapis.com/{bucket}/{quote(relative_path)}"
    cmd = [
        "ffprobe", "-v", "error",
        "-headers", f"Authorization: Bearer {token}\r\n",
        "-show_entries",
        "format=duration,bit_rate,format_name:stream=codec_name,bit_rate,channels,sample_rate",
        "-of", "json", "-i", url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception:
        return {}
    if proc.returncode != 0 or not proc.stdout.strip():
        return {}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}


def probe_metadata_to_fields(meta: dict) -> dict:
    fmt = (meta or {}).get("format", {}) or {}
    streams = (meta or {}).get("streams", []) or []
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if audio is None and streams:
        audio = streams[0]
    audio = audio or {}

    def _float(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _int(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    return {
        "duration_s": _float(fmt.get("duration")),
        "codec": audio.get("codec_name") or fmt.get("format_name"),
        "bitrate_kbps": _int(audio.get("bit_rate") or fmt.get("bit_rate")) // 1000
        if _int(audio.get("bit_rate") or fmt.get("bit_rate")) is not None
        else None,
        "channels": _int(audio.get("channels")),
        "sample_rate": _int(audio.get("sample_rate")),
    }


# ---------------------------------------------------------------------------
# Source loaders (GitHub clones)
# ---------------------------------------------------------------------------


def repo_path(config: Config, key: str, sources_dir: str | Path | None = None) -> Path:
    base = Path(sources_dir) if sources_dir else config.sources_dir
    return base / key


def load_yue2(sources_dir: str | Path, repo_key: str = "yue2") -> dict[str, pd.DataFrame | list[dict]]:
    base = Path(sources_dir) / repo_key / "index" / "lyrics"
    out: dict[str, Any] = {}
    for name in ("inventory", "title_map", "coverage", "readiness", "lyrics_index", "manual_sources"):
        p = base / f"{name}.csv"
        out[name] = pd.read_csv(p, dtype=str).fillna("") if p.exists() else pd.DataFrame()
    corpus = Path(sources_dir) / repo_key / "results" / "corpus" / "corpus.jsonl"
    out["corpus"] = read_jsonl(corpus) if corpus.exists() else []
    return out


def load_mcap(sources_dir: str | Path, repo_key: str = "mcap") -> tuple[dict, dict[str, dict]]:
    base = Path(sources_dir) / repo_key / "outputs"
    index = read_json(base / "index.json", default={"albums": {}}) or {"albums": {}}
    albums: dict[str, dict] = {}
    for key, info in (index.get("albums") or {}).items():
        p = base / info["path"]
        if p.exists():
            albums[key] = read_json(p)
    return index, albums


def load_lyrics_repo(sources_dir: str | Path, repo_key: str = "lyrics") -> list[dict]:
    """Load fnanen + kalimataghani artist JSONL files into one list."""
    base = Path(sources_dir) / repo_key / "lyrics"
    rows: list[dict] = []
    for source in ("fnanen", "kalimataghani"):
        for p in sorted((base / source / "artists").glob("*/*.jsonl")):
            artist_slug = p.parent.name
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                row["_source"] = source
                row["_artist_dir"] = artist_slug
                rows.append(row)
    return rows


def load_arabic_lyrics_parquet(sources_dir: str | Path, repo_key: str = "lyrics") -> pd.DataFrame:
    p = Path(sources_dir) / repo_key / "data" / "processed" / "arabic_lyrics.parquet"
    return pd.read_parquet(p) if p.exists() else pd.DataFrame()


def list_gcs_rel(config: Config, prefix: str, bucket=None) -> list[str]:
    bucket = bucket or get_bucket(config)
    return [b.name for b in bucket.list_blobs(prefix=prefix)]


def load_abc_tracks(
    config: Config,
    manifest: pd.DataFrame,
    cache_dir: str | Path | None = None,
    bucket=None,
) -> list[dict]:
    """Read every TRANSCRIPTS/*/abc/*.json and return flattened track rows."""
    rows: list[dict] = []
    candidates = manifest[
        manifest["relative_path"].str.match(r"^TRANSCRIPTS/[^/]+/abc/.+\.json$")
    ]
    for _, f in candidates.iterrows():
        rel = f["relative_path"]
        try:
            payload = read_gcs_json(config, rel, cache_dir, bucket)
        except Exception as exc:  # pragma: no cover - network/format guard
            print(f"[abc] skipping {rel}: {exc}", flush=True)
            continue
        artist_dir = rel.split("/")[1]
        album = payload.get("album")
        for track in payload.get("tracks", []) or []:
            row = dict(track)
            row.update(
                {
                    "abc_path": rel,
                    "abc_artist_dir": artist_dir,
                    "album": album,
                    "model": payload.get("model"),
                    "model_revision": payload.get("model_revision"),
                    "json_status": payload.get("status"),
                    "artist": payload.get("artist"),
                }
            )
            rows.append(row)
    return rows


def load_srt_corpus(sources_dir: str | Path, repo_key: str = "yue2") -> list[dict]:
    p = Path(sources_dir) / repo_key / "results" / "corpus" / "corpus.jsonl"
    return read_jsonl(p) if p.exists() else []


# ---------------------------------------------------------------------------
# Audio canonicalisation
# ---------------------------------------------------------------------------

_COLLECTION_PRIORITY = {
    "discography_album": 0,
    "youtube_ref": 1,
    "spotify_p0": 2,
    "spotify_pd": 3,
}


def choose_canonical(group: pd.DataFrame) -> pd.Series:
    g = group.copy()
    g["_prio"] = g["collection"].map(lambda c: _COLLECTION_PRIORITY.get(c, 9))
    g["_len"] = g["relative_path"].str.len()
    g = g.sort_values(["_prio", "_len", "relative_path"])
    return g.iloc[0]


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

SCHEMA_COLUMNS: dict[str, list[str]] = {
    "artists": ["artist_id", "name_lat", "name_ar", "aliases", "country", "source", "created_at"],
    "albums": ["album_id", "artist_id", "title", "title_norm", "year", "album_type",
               "source_collection", "source_uri", "created_at"],
    "works": ["work_id", "artist_id", "title_lat", "title_ar", "title_norm", "year",
              "language", "dialect", "genre", "maqam", "is_medley", "created_at"],
    "recordings": ["recording_id", "work_id", "artist_id", "album_id", "collection",
                   "legacy_track_id", "disc_no", "track_no", "title_lat", "title_ar",
                   "duration_s", "audio_sha256", "audio_md5", "audio_crc32c",
                   "audio_bucket", "audio_path", "codec", "bitrate_kbps", "bytes",
                   "is_instrumental", "is_generated", "version_type",
                   "recording_group_id", "audio_fingerprint", "source_license", "created_at"],
    "legacy_ids": ["namespace", "value", "recording_id", "is_primary", "note"],
    "recording_works": ["recording_id", "work_id", "position", "is_medley"],
    "lyrics": ["lyric_id", "work_id", "recording_id", "artist_name_raw", "title_raw",
               "title_ar", "source", "source_id", "source_url", "text", "text_match",
               "n_lines", "n_words", "arabic_ratio", "harakat_ratio", "has_translation",
               "is_primary", "completeness", "source_license", "retrieved_at"],
    "timings": ["timing_id", "recording_id", "run_id", "srt_path", "vtt_path",
                "words_path", "n_words", "duration_s", "model", "model_revision", "qa",
                "is_preferred", "artifact_sha256", "created_at"],
    "transcripts": ["transcript_id", "recording_id", "album_id", "abc_text", "abc_path",
                    "status", "model", "model_revision", "duration_s", "gpu_seconds",
                    "rtf", "artifact_sha256", "created_at"],
    "captions": ["caption_id", "recording_id", "model", "model_revision", "instruments",
                 "vocals", "production", "caption_text", "suno_prompt",
                 "caption_seconds", "is_generated", "artifact_sha256", "created_at"],
    "links": ["link_id", "src_type", "src_id", "dst_type", "dst_id", "relation",
              "method", "confidence", "evidence", "is_gold", "created_at"],
    "files": ["file_id", "source", "bucket", "repo", "commit_sha", "relative_path",
              "uri", "bytes", "sha256", "md5", "crc32c", "content_type", "generation",
              "etag", "mtime", "scanned_at"],
    "coverage": ["recording_id", "work_id", "collection", "has_lyrics", "has_timing",
                 "has_transcript", "has_caption", "n_lyrics", "n_timings",
                 "min_link_confidence", "unresolved_reason"],
    "splits": ["work_id", "split"],
}


def conform_table(df: pd.DataFrame, table: str) -> pd.DataFrame:
    cols = SCHEMA_COLUMNS[table]
    out = df.copy()
    for col in cols:
        if col not in out.columns:
            out[col] = None
    return out[cols]


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str, sort_keys=True)
