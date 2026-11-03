#!/usr/bin/env python3
"""Export one song's audio, transcript and SRT subtitles.

The script is intentionally standalone (standard library only).  It assumes
three local source trees, matching the layout used by this repository:

    AUDIO_ROOT/ARTIST/ALBUM/NN.Title.mp3
    SRT_ROOT/v1/<run>/ALBUM_-_NN.Title.srt
    TRANSCRIPTS_ROOT/ARTIST/abc/ALBUM.json
    TRANSCRIPTS_ROOT/ARTIST/ALBUM.tar.gz   (contains NN.Title/score.abc)

Nothing is inferred from the network.  Point the roots at your own copies with
--audio-root, --srt-root and --transcript-root (or the SONG_* environment
variables).

Examples
--------
Interactive::

    ./fetch_song.py

One song, non-interactive::

    ./fetch_song.py --song "Hobak Khayal" --dest ~/exports

See the candidate list without copying::

    ./fetch_song.py --list "law faker"

Copy a song that occurs on several albums::

    ./fetch_song.py --song "Ya Habibi" --pick 2 --dest ~/exports
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tarfile
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Sequence

AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".aac"}

DEFAULT_AUDIO_ROOT = Path(os.environ.get("SONG_AUDIO_ROOT", "/content/DISCOGRAPHY"))
DEFAULT_SRT_ROOT = Path(os.environ.get("SONG_SRT_ROOT", "/content/SRT"))
DEFAULT_TRANSCRIPT_ROOT = Path(
    os.environ.get("SONG_TRANSCRIPT_ROOT", "/content/TRANSCRIPTS")
)
DEFAULT_DEST = Path(os.environ.get("SONG_DEST", "song_exports"))


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

_TRANSLATION = str.maketrans(
    {
        "\u2019": "'",
        "\u2018": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u00a0": " ",
    }
)


def norm(text: str | None) -> str:
    """Normalise names for matching (case/punctuation/diacritics agnostic)."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", str(text)).translate(_TRANSLATION)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("_", " ")
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def rel_key(path: str | Path) -> str:
    """A slash-preserving normalised key for a relative path."""
    parts = Path(str(path).replace("\\", "/")).parts
    return "/".join(norm(part) for part in parts)


def extract_year(text: str | None) -> int | None:
    if not text:
        return None
    match = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", str(text))
    return int(match.group(1)) if match else None


def strip_extension(name: str) -> str:
    return name.rsplit(".", 1)[0] if "." in name else name


def split_track(stem: str) -> tuple[int | None, str]:
    """Split '01.Song_Name' into (1, 'Song Name')."""
    text = stem.replace("_", " ")
    match = re.match(r"^\s*(\d{1,3})\s*[.\-_\s]+\s*(.*)$", text)
    if match:
        return int(match.group(1)), clean_title(match.group(2))
    return None, clean_title(text)


def clean_title(text: str | None) -> str:
    text = unicodedata.normalize("NFKC", str(text or "")).translate(_TRANSLATION)
    text = re.sub(r"^\s*\d{1,3}\s*[.\-_\s]+\s*", "", text)
    text = text.replace("_", " ")
    return re.sub(r"\s+", " ", text).strip(" .-")


def album_title_from_dir(raw: str | None) -> str:
    """Turn '05.Hobak_Khayal.2001' or 'Elissa - 2004 Ahla Donya' into a title."""
    if not raw:
        return ""
    text = raw.replace("_", " ")
    text = re.sub(
        r"^\s*[A-Za-z][A-Za-z0-9 .'&-]*\s*-\s*(?:19|20)\d{2}\s+", "", text
    )
    text = re.sub(r"^\s*\d{1,3}\s*[.\-_\s]+\s*", "", text)
    text = re.sub(r"\b(?:19|20)\d{2}\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .-_")
    return text


def artist_from_dir(raw: str | None) -> str:
    raw = str(raw or "").strip()
    if not raw:
        return "Unknown"
    if "_" in raw or raw.isupper():
        raw = raw.replace("_", " ").title()
    return re.sub(r"\s+", " ", raw).strip()


def sanitize_filename(name: str, fallback: str = "song") -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" ._")
    return name or fallback


# ---------------------------------------------------------------------------
# Library data
# ---------------------------------------------------------------------------


@dataclass
class AudioTrack:
    path: Path
    rel_key: str
    artist: str
    album: str
    title: str
    track_no: int | None
    year: int | None
    artist_key: str
    album_key: str
    title_key: str

    @property
    def haystack(self) -> str:
        bits = [
            self.artist_key,
            self.album_key,
            self.title_key,
            self.path.stem.lower(),
        ]
        if self.year:
            bits.append(str(self.year))
        return " ".join(bits)


@dataclass
class SrtFile:
    path: Path
    run: str
    artist: str
    album: str
    title: str
    track_no: int | None
    year: int | None
    album_key: str
    title_key: str
    track_id: str | None = None
    audio: AudioTrack | None = None


@dataclass
class Transcript:
    kind: str  # "abc-file" | "tar" | "json"
    path: Path | None
    member: str | None
    text: str | None
    artist: str
    album: str
    title: str
    track_no: int | None
    album_key: str
    title_key: str
    source_rel: str | None = None
    audio: AudioTrack | None = None

    def read_bytes(self) -> bytes:
        if self.kind == "abc-file" and self.path is not None:
            return self.path.read_bytes()

        if self.kind == "tar" and self.path is not None:
            try:
                with tarfile.open(self.path, "r:*") as archive:
                    member = self.member
                    handle = None
                    if member:
                        try:
                            handle = archive.extractfile(member)
                        except KeyError:
                            handle = None
                    if handle is None:
                        # Be tolerant of archives whose member names changed,
                        # but never grab another track's score.abc.
                        wanted = Path(member or "")
                        for info in archive.getmembers():
                            candidate = Path(info.name)
                            if (
                                info.isfile()
                                and candidate.name == (wanted.name or "score.abc")
                                and candidate.parent.name == wanted.parent.name
                            ):
                                handle = archive.extractfile(info)
                                if handle is not None:
                                    break
                    if handle is not None:
                        return handle.read()
            except (OSError, tarfile.TarError):
                pass

        if self.text:
            return self.text.encode("utf-8")
        raise FileNotFoundError(
            f"transcript is not readable: {self.path} ({self.kind})"
        )


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------


def _artist_for_audio(rel_parts: Sequence[str], root: Path) -> str:
    raw = rel_parts[0] if rel_parts else root.name
    if re.search(r"discograph|compilation|full album", raw, re.I):
        # The root itself may already point at the artist directory.
        candidates = [root.name, root.parent.name, root.parent.parent.name]
        for candidate in candidates:
            if candidate and not re.search(
                r"^(content|discography|data|audio|music)$", candidate, re.I
            ):
                raw = candidate
                break
    return artist_from_dir(raw)


def index_audio(root: Path) -> list[AudioTrack]:
    if not root.is_dir():
        raise FileNotFoundError(f"audio root does not exist: {root}")

    tracks: list[AudioTrack] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in AUDIO_EXTS:
            continue
        rel = path.relative_to(root)
        parts = rel.parts
        album_raw = parts[-2] if len(parts) >= 2 else ""
        track_no, title = split_track(path.stem)
        album = album_title_from_dir(album_raw) or clean_title(album_raw)
        artist = _artist_for_audio(parts, root)
        tracks.append(
            AudioTrack(
                path=path,
                rel_key=rel_key(rel),
                artist=artist,
                album=album,
                title=title or path.stem,
                track_no=track_no,
                year=extract_year(album_raw),
                artist_key=norm(artist),
                album_key=norm(album),
                title_key=norm(title or path.stem),
            )
        )
    return tracks


def _manifest_hints(srt_root: Path) -> dict[tuple[str, str], str]:
    """Map (run_dir, filename) -> track_id from SRT manifest.jsonl files."""
    hints: dict[tuple[str, str], str] = {}
    for manifest in srt_root.rglob("*.jsonl"):
        try:
            lines = manifest.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            track_id = row.get("track_id")
            if not track_id:
                continue
            name = row.get("name") or row.get("file") or Path(
                str(row.get("uri", ""))
            ).name
            if name:
                hints[(manifest.parent.name, name)] = str(track_id)
                hints.setdefault(("*", name), str(track_id))
    return hints


def _parse_srt_name(path: Path) -> tuple[str | None, str, str, int | None, int | None]:
    """Return (artist, album, title, track_no, year) from an SRT filename."""
    stem = path.stem
    artist: str | None = None

    if stem.startswith("Elissa_-_"):
        artist = "Elissa"
        body = stem[len("Elissa_-_") :]
        if "_-_" not in body:
            return None, "", clean_title(stem), None, None
        album_part, track_part = body.split("_-_", 1)
    elif "_-_" in stem:
        album_part, track_part = stem.split("_-_", 1)
    else:
        album_part, track_part = "", stem

    year = extract_year(album_part)
    album = album_title_from_dir(album_part) or clean_title(album_part)
    track_no, title = split_track(track_part)
    return artist, album, title, track_no, year


def index_srts(root: Path, audio: Sequence[AudioTrack]) -> list[SrtFile]:
    if not root.is_dir():
        raise FileNotFoundError(f"SRT root does not exist: {root}")

    hints = _manifest_hints(root)
    by_rel = {track.rel_key: track for track in audio}
    by_album_track: dict[tuple[str, int], list[AudioTrack]] = {}
    by_album_title: dict[tuple[str, str], list[AudioTrack]] = {}
    by_title_track: dict[tuple[str, int], list[AudioTrack]] = {}
    for track in audio:
        if track.track_no is not None:
            by_album_track.setdefault((track.album_key, track.track_no), []).append(track)
            by_title_track.setdefault((track.title_key, track.track_no), []).append(track)
        by_album_title.setdefault((track.album_key, track.title_key), []).append(track)

    srts: list[SrtFile] = []
    for path in sorted(root.rglob("*.srt")):
        run = path.parent.name
        track_id = hints.get((run, path.name)) or hints.get(("*", path.name))
        artist = album = title = ""
        track_no = year = None

        if track_id and "/" in track_id:
            parts = track_id.split("/")
            if len(parts) >= 3:
                artist = artist_from_dir(parts[0])
                album = album_title_from_dir(parts[-2]) or clean_title(parts[-2])
                track_no, title = split_track(strip_extension(parts[-1]))
                year = extract_year(parts[-2])

        if not title:
            artist_guess, album_guess, title_guess, no_guess, year_guess = (
                _parse_srt_name(path)
            )
            artist = artist or (artist_guess or "")
            album = album or album_guess
            title = title_guess
            track_no = no_guess if track_no is None else track_no
            year = year if year is not None else year_guess

        srt = SrtFile(
            path=path,
            run=run,
            artist=artist,
            album=album,
            title=title,
            track_no=track_no,
            year=year,
            album_key=norm(album),
            title_key=norm(title),
            track_id=track_id,
        )

        # --- link the SRT to one audio track --------------------------------
        linked: AudioTrack | None = None
        if track_id:
            linked = by_rel.get(rel_key(track_id))
            if linked is None and track_id.startswith("DISCOGRAPHY/"):
                linked = by_rel.get(rel_key(track_id[len("DISCOGRAPHY/") :]))

        if linked is None and srt.album_key and track_no is not None:
            candidates = by_album_track.get((srt.album_key, track_no), [])
            if len(candidates) == 1:
                linked = candidates[0]

        if linked is None and srt.album_key and srt.title_key:
            candidates = by_album_title.get((srt.album_key, srt.title_key), [])
            if len(candidates) == 1:
                linked = candidates[0]

        if linked is None and srt.title_key and track_no is not None:
            candidates = by_title_track.get((srt.title_key, track_no), [])
            if len(candidates) == 1:
                linked = candidates[0]

        if linked is None and srt.album_key and srt.title_key:
            best: tuple[float, AudioTrack] | None = None
            for track in audio:
                if track.album_key != srt.album_key:
                    continue
                score = SequenceMatcher(None, srt.title_key, track.title_key).ratio()
                if best is None or score > best[0]:
                    best = (score, track)
            if best and best[0] >= 0.82:
                linked = best[1]

        srt.audio = linked
        srts.append(srt)
    return srts


def _source_uri_to_rel(uri: str | None) -> str | None:
    if not uri:
        return None
    text = str(uri)
    if "://" in text:
        text = text.split("://", 1)[1]
        # Drop the bucket name.
        if "/" in text:
            text = text.split("/", 1)[1]
    text = text.replace("\\", "/")
    if text.startswith("DISCOGRAPHY/"):
        text = text[len("DISCOGRAPHY/") :]
    return text or None


def index_transcripts(
    root: Path, audio: Sequence[AudioTrack]
) -> tuple[list[Transcript], dict[Path, list[Transcript]]]:
    if not root.is_dir():
        raise FileNotFoundError(f"transcript root does not exist: {root}")

    by_rel = {track.rel_key: track for track in audio}
    by_album_track: dict[tuple[str, int], list[AudioTrack]] = {}
    by_album_title: dict[tuple[str, str], list[AudioTrack]] = {}
    by_title_track: dict[tuple[str, int], list[AudioTrack]] = {}
    for track in audio:
        if track.track_no is not None:
            by_album_track.setdefault((track.album_key, track.track_no), []).append(track)
            by_title_track.setdefault((track.title_key, track.track_no), []).append(track)
        by_album_title.setdefault((track.album_key, track.title_key), []).append(track)

    transcripts: list[Transcript] = []
    tar_members: dict[Path, set[str]] = {}

    def tar_has_member(tar_path: Path, member: str) -> bool:
        if tar_path not in tar_members:
            try:
                with tarfile.open(tar_path, "r:*") as archive:
                    tar_members[tar_path] = {
                        str(Path(info.name)) for info in archive.getmembers() if info.isfile()
                    }
            except (OSError, tarfile.TarError):
                tar_members[tar_path] = set()
        return str(Path(member)) in tar_members[tar_path]

    def link(item: Transcript) -> AudioTrack | None:
        if item.source_rel:
            found = by_rel.get(rel_key(item.source_rel))
            if found is not None:
                return found
            # Some manifests use DISCOGRAPHY/... while the local root already
            # starts at the artist directory.
            parts = item.source_rel.split("/")
            for index in range(1, len(parts)):
                found = by_rel.get(rel_key("/".join(parts[index:])))
                if found is not None:
                    return found

        if item.album_key and item.track_no is not None:
            candidates = by_album_track.get((item.album_key, item.track_no), [])
            if len(candidates) == 1:
                return candidates[0]
        if item.album_key and item.title_key:
            candidates = by_album_title.get((item.album_key, item.title_key), [])
            if len(candidates) == 1:
                return candidates[0]
        if item.title_key and item.track_no is not None:
            candidates = by_title_track.get((item.title_key, item.track_no), [])
            if len(candidates) == 1:
                return candidates[0]
        if item.album_key and item.title_key:
            best: tuple[float, AudioTrack] | None = None
            for track in audio:
                if track.album_key != item.album_key:
                    continue
                score = SequenceMatcher(None, item.title_key, track.title_key).ratio()
                if best is None or score > best[0]:
                    best = (score, track)
            if best and best[0] >= 0.82:
                return best[1]
        return None

    # 1. Album-level ABC JSON files: TRANSCRIPTS/<artist>/abc/<album>.json
    for json_path in sorted(root.rglob("*.json")):
        if json_path.parent.name != "abc":
            continue
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        album_raw = str(payload.get("album") or json_path.stem)
        album = album_title_from_dir(album_raw) or clean_title(album_raw)
        artist = str(payload.get("artist") or json_path.parent.parent.name)
        artist = artist_from_dir(artist)
        album_key = norm(album)

        tar_path = json_path.parent.parent / f"{album_raw}.tar.gz"
        if not tar_path.exists():
            tar_path = json_path.parent.parent / f"{json_path.stem}.tar.gz"

        for track in payload.get("tracks") or []:
            file_name = str(track.get("file") or "")
            title = clean_title(track.get("title") or strip_extension(file_name))
            track_no = track.get("track_no")
            if track_no is None:
                track_no, _ = split_track(strip_extension(file_name))
            stem = str(track.get("stem") or strip_extension(file_name))
            abc_text = track.get("abc")
            source_rel = _source_uri_to_rel(track.get("source_uri"))

            if abc_text:
                kind = "json"
                path = json_path
                member = None
            elif tar_path.exists():
                member = f"./{stem}/score.abc"
                if not tar_has_member(tar_path, member):
                    continue
                kind = "tar"
                path = tar_path
            else:
                continue

            item = Transcript(
                kind=kind,
                path=path,
                member=member,
                text=str(abc_text) if abc_text else None,
                artist=artist,
                album=album,
                title=title,
                track_no=int(track_no) if track_no is not None else None,
                album_key=album_key,
                title_key=norm(title),
                source_rel=source_rel,
            )
            item.audio = link(item)
            transcripts.append(item)

    # 2. Already-extracted score.abc files (optional in a local tree).
    for abc_path in sorted(root.rglob("*.abc")):
        track_dir = abc_path.parent
        album_raw = track_dir.parent.name
        album = album_title_from_dir(album_raw) or clean_title(album_raw)
        artist = artist_from_dir(track_dir.parent.parent.name)
        track_no, title = split_track(track_dir.name)
        item = Transcript(
            kind="abc-file",
            path=abc_path,
            member=None,
            text=None,
            artist=artist,
            album=album,
            title=title,
            track_no=track_no,
            album_key=norm(album),
            title_key=norm(title),
        )
        item.audio = link(item)
        transcripts.append(item)

    by_audio: dict[Path, list[Transcript]] = {}
    for item in transcripts:
        if item.audio is not None:
            by_audio.setdefault(item.audio.path, []).append(item)
    return transcripts, by_audio


# ---------------------------------------------------------------------------
# Search and selection
# ---------------------------------------------------------------------------


def search_audio(audio: Sequence[AudioTrack], query: str) -> list[tuple[float, AudioTrack]]:
    q = norm(query)
    if not q:
        return [(1.0, track) for track in audio]
    if len(q) < 2:
        return []

    # An exact artist query (e.g. "Elissa") is more useful as that artist's
    # catalogue than as the one track that happens to share the name.
    artist_hits = [track for track in audio if track.artist_key == q]
    if len(artist_hits) > 1:
        results = [
            (3.0 if track.title_key == q else 1.0, track) for track in artist_hits
        ]
        results.sort(key=lambda pair: (-pair[0], pair[1].album, pair[1].title))
        return results

    scored: list[tuple[float, AudioTrack]] = []
    q_tokens = q.split()
    for track in audio:
        haystack = track.haystack
        title_score = SequenceMatcher(None, q, track.title_key).ratio()
        full_score = SequenceMatcher(None, q, haystack).ratio()
        token_hits = sum(1 for token in q_tokens if token in haystack)
        coverage = token_hits / len(q_tokens) if q_tokens else 0.0

        score = max(title_score * 1.15, full_score * 0.65) + coverage * 0.45
        if q == track.title_key:
            score += 2.0
        elif q in track.title_key:
            score += 0.9
        elif track.title_key and track.title_key in q:
            score += 0.5
        if q in haystack:
            score += 0.35

        if score >= 0.42:
            scored.append((round(score, 4), track))

    scored.sort(key=lambda pair: (-pair[0], pair[1].artist, pair[1].album, pair[1].title))
    if len(scored) > 1:
        # Keep the result set useful: a song-title query should not return an
        # entire album just because the album name shares a word.
        cutoff = max(0.42, scored[0][0] * 0.55)
        scored = [pair for pair in scored if pair[0] >= cutoff]
    return scored


def srt_priority(srt: SrtFile) -> tuple[int, float, str]:
    run = srt.run.lower()
    for index, marker in enumerate(("ready", "cleanup", "corpus", "exp")):
        if marker in run:
            return (index, -srt.path.stat().st_mtime, srt.path.name)
    return (9, -srt.path.stat().st_mtime, srt.path.name)


def pick_srt(
    audio: AudioTrack, srts: Sequence[SrtFile], run_filter: str | None
) -> SrtFile | None:
    candidates = [srt for srt in srts if srt.audio and srt.audio.path == audio.path]
    if run_filter:
        candidates = [
            srt for srt in candidates if run_filter.lower() in srt.run.lower()
        ]
    if not candidates:
        return None
    return sorted(candidates, key=srt_priority)[0]


def pick_transcript(
    audio: AudioTrack, by_audio: dict[Path, list[Transcript]]
) -> Transcript | None:
    candidates = by_audio.get(audio.path) or []
    if not candidates:
        return None
    priority = {"abc-file": 0, "tar": 1, "json": 2}
    return sorted(candidates, key=lambda item: (priority.get(item.kind, 9), str(item.path)))[0]


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------


def print_matches(
    matches: Sequence[tuple[float, AudioTrack]],
    by_audio: dict[Path, list[Transcript]],
    srts: Sequence[SrtFile],
    limit: int = 20,
) -> None:
    print()
    for index, (_, track) in enumerate(matches[:limit], start=1):
        no = f"{track.track_no:02d}." if track.track_no is not None else ""
        year = f" ({track.year})" if track.year else ""
        transcript_mark = "ABC" if by_audio.get(track.path) else "no-ABC"
        srt_mark = "SRT" if any(s.audio and s.audio.path == track.path for s in srts) else "no-SRT"
        print(f"  {index:2d}. {no} {track.title}  —  {track.artist} / {track.album}{year}")
        print(f"      [{transcript_mark}] [{srt_mark}] {track.path}")
    if len(matches) > limit:
        print(f"  ... {len(matches) - limit} more; refine the search to narrow it down.")
    print()


def choose_match(
    matches: Sequence[tuple[float, AudioTrack]], interactive: bool, pick: int | None
) -> AudioTrack | None:
    if not matches:
        return None
    if pick is not None:
        if not 1 <= pick <= len(matches):
            raise ValueError(f"--pick must be between 1 and {len(matches)}")
        return matches[pick - 1][1]
    if len(matches) == 1:
        return matches[0][1]

    if interactive and sys.stdin.isatty():
        while True:
            answer = input(
                f"Select 1-{len(matches)} (Enter=1, q=quit) [1]: "
            ).strip()
            if answer.lower() in {"q", "quit", "exit"}:
                print()
                return None
            if not answer:
                return matches[0][1]
            if answer.isdigit() and 1 <= int(answer) <= len(matches):
                return matches[int(answer) - 1][1]
            print("Please enter a number from the list.")

    print("Multiple matches; using the first. Use --pick N to choose another.")
    return matches[0][1]


def print_plan(
    audio: AudioTrack,
    transcript: Transcript | None,
    srt: SrtFile | None,
    dest: Path,
    layout: str,
) -> None:
    print(f"\nSong: {audio.title}")
    print(f"  artist : {audio.artist}")
    print(f"  album  : {audio.album}" + (f" ({audio.year})" if audio.year else ""))
    print(f"  audio  : {audio.path}")
    print(f"  ABC    : {transcript.path if transcript else 'MISSING'}")
    if transcript and transcript.kind == "tar":
        print(f"           member {transcript.member}")
    print(f"  SRT    : {srt.path if srt else 'MISSING'}")
    print(f"  dest   : {dest if layout == 'flat' else dest / '<song folder>'}")


# ---------------------------------------------------------------------------
# Copying
# ---------------------------------------------------------------------------


def _prepare_destination(path: Path, force: bool, dry_run: bool) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"destination already exists: {path}")
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)


def copy_bytes(data: bytes, dest: Path, force: bool, dry_run: bool) -> None:
    _prepare_destination(dest, force, dry_run)
    print(f"  + {dest}")
    if not dry_run:
        dest.write_bytes(data)


def copy_file(src: Path, dest: Path, force: bool, dry_run: bool) -> None:
    _prepare_destination(dest, force, dry_run)
    print(f"  + {dest}")
    if not dry_run:
        shutil.copy2(src, dest)


def export_song(
    audio: AudioTrack,
    transcript: Transcript | None,
    srt: SrtFile | None,
    dest_root: Path,
    *,
    layout: str,
    force: bool,
    dry_run: bool,
    keep_names: bool,
    with_extras: bool,
) -> list[Path]:
    if layout == "bundle":
        no = f"{audio.track_no:02d}. " if audio.track_no is not None else ""
        folder = sanitize_filename(f"{audio.artist} - {audio.album} - {no}{audio.title}")
        dest = dest_root / folder
    else:
        dest = dest_root

    created: list[Path] = []
    audio_dest = dest / audio.path.name
    copy_file(audio.path, audio_dest, force, dry_run)
    created.append(audio_dest)

    base = audio.path.stem
    if transcript is not None:
        if keep_names:
            transcript_name = (
                transcript.path.name
                if transcript.kind == "abc-file" and transcript.path
                else "score.abc"
            )
        else:
            transcript_name = f"{base}.abc"
        transcript_dest = dest / transcript_name
        copy_bytes(transcript.read_bytes(), transcript_dest, force, dry_run)
        created.append(transcript_dest)

    if srt is not None:
        srt_name = srt.path.name if keep_names else f"{base}.srt"
        srt_dest = dest / srt_name
        copy_file(srt.path, srt_dest, force, dry_run)
        created.append(srt_dest)

        if with_extras:
            for extra_suffix in (".vtt", ".words.json"):
                extra_src = srt.path.with_name(srt.path.stem + extra_suffix)
                if not extra_src.exists():
                    continue
                extra_name = (
                    extra_src.name if keep_names else f"{base}{extra_suffix}"
                )
                extra_dest = dest / extra_name
                copy_file(extra_src, extra_dest, force, dry_run)
                created.append(extra_dest)

    return created


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Copy a song, its ABC transcript and its SRT to an output directory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Environment variables: SONG_AUDIO_ROOT, SONG_SRT_ROOT, "
            "SONG_TRANSCRIPT_ROOT, SONG_DEST."
        ),
    )
    parser.add_argument("query", nargs="?", help="song query (same as --song)")
    parser.add_argument("-s", "--song", help="song query (artist, album or title)")
    parser.add_argument("-d", "--dest", type=Path, default=None,
                        help=f"output directory (default: {DEFAULT_DEST})")
    parser.add_argument("--audio-root", type=Path, default=DEFAULT_AUDIO_ROOT,
                        help=f"audio tree (default: {DEFAULT_AUDIO_ROOT})")
    parser.add_argument("--srt-root", type=Path, default=DEFAULT_SRT_ROOT,
                        help=f"SRT tree (default: {DEFAULT_SRT_ROOT})")
    parser.add_argument("--transcript-root", type=Path,
                        default=DEFAULT_TRANSCRIPT_ROOT,
                        help=f"transcript tree (default: {DEFAULT_TRANSCRIPT_ROOT})")
    parser.add_argument("-l", "--list", action="store_true",
                        help="list matching songs and exit")
    parser.add_argument("-p", "--pick", type=int,
                        help="choose match N from the numbered list (1-based)")
    parser.add_argument("--srt-run", help="prefer SRT only from runs matching this text")
    parser.add_argument("--layout", choices=("flat", "bundle"), default="flat",
                        help="flat files in --dest, or one folder per song (default: flat)")
    parser.add_argument("--keep-names", action="store_true",
                        help="keep source SRT/transcript names instead of renaming to the audio stem")
    parser.add_argument("--with-extras", action="store_true",
                        help="also copy matching .vtt and .words.json files")
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be copied, without writing")
    parser.add_argument("--strict", action="store_true",
                        help="fail unless audio, transcript and SRT all exist")
    parser.add_argument("--limit", type=int, default=20,
                        help="maximum matches to display (default: 20)")
    return parser


def interactive_loop(
    audio: Sequence[AudioTrack],
    srts: Sequence[SrtFile],
    by_audio: dict[Path, list[Transcript]],
    dest_root: Path,
    args: argparse.Namespace,
    ask_dest: bool,
) -> int:
    print(
        f"Indexed {len(audio)} audio tracks, {len(srts)} SRT files, "
        f"{sum(len(v) for v in by_audio.values())} transcripts."
    )
    print(f"Audio: {args.audio_root}")
    print(f"SRT:   {args.srt_root}")
    print(f"ABC:   {args.transcript_root}")

    if ask_dest:
        try:
            answer = input(f"\nDestination directory [{dest_root}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if answer.lower() in {"q", "quit", "exit"}:
            return 0
        if answer:
            dest_root = Path(answer).expanduser()

    print("\nType a song/artist/album, or 'q' to quit.\n")

    while True:
        try:
            query = input("Song: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if query.lower() in {"q", "quit", "exit"}:
            print()
            return 0
        if not query:
            continue

        matches = search_audio(audio, query)
        if not matches:
            print("No matches. Try a shorter title or an artist/album name.\n")
            continue

        print_matches(matches, by_audio, srts, limit=args.limit)
        selected = choose_match(matches, interactive=True, pick=None)
        if selected is None:
            return 0

        transcript = pick_transcript(selected, by_audio)
        srt = pick_srt(selected, srts, args.srt_run)
        if args.strict and (transcript is None or srt is None):
            print("Strict mode: this song is missing an ABC transcript or SRT.\n")
            continue

        print_plan(selected, transcript, srt, dest_root, args.layout)
        try:
            answer = input("\nCopy these files? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if answer in {"n", "no"}:
            print("Skipped.\n")
            continue

        try:
            created = export_song(
                selected,
                transcript,
                srt,
                dest_root,
                layout=args.layout,
                force=args.force,
                dry_run=args.dry_run,
                keep_names=args.keep_names,
                with_extras=args.with_extras,
            )
        except (FileExistsError, FileNotFoundError, OSError) as exc:
            print(f"ERROR: {exc}\n", file=sys.stderr)
            continue
        print(f"Done: {len(created)} file(s){' (dry run)' if args.dry_run else ''}.\n")
        if transcript is None:
            print("WARNING: no ABC transcript was found for this song.\n")
        if srt is None:
            print("WARNING: no SRT was found for this song.\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.query and args.song:
        parser.error("use either a positional query or --song, not both")
    if args.query and not args.song:
        args.song = args.query

    try:
        audio = index_audio(args.audio_root.expanduser())
        srts = index_srts(args.srt_root.expanduser(), audio)
        _, by_audio = index_transcripts(args.transcript_root.expanduser(), audio)
    except FileNotFoundError as exc:
        parser.error(str(exc))

    dest_root = (args.dest or DEFAULT_DEST).expanduser()

    if args.song is None and args.list:
        matches = search_audio(audio, "")
        print_matches(matches, by_audio, srts, limit=args.limit)
        return 0

    if args.song is None:
        return interactive_loop(
            audio, srts, by_audio, dest_root, args, ask_dest=args.dest is None
        )

    matches = search_audio(audio, args.song)
    if not matches:
        print(f"No song matched {args.song!r}.", file=sys.stderr)
        return 2

    print_matches(matches, by_audio, srts, limit=args.limit)
    if args.list:
        return 0

    try:
        selected = choose_match(
            matches,
            interactive=sys.stdin.isatty(),
            pick=args.pick,
        )
    except ValueError as exc:
        parser.error(str(exc))
    if selected is None:
        return 0

    transcript = pick_transcript(selected, by_audio)
    srt = pick_srt(selected, srts, args.srt_run)
    print_plan(selected, transcript, srt, dest_root, args.layout)

    if args.strict and (transcript is None or srt is None):
        sys.stdout.flush()
        print(
            "ERROR: --strict requires an ABC transcript and an SRT for this song.",
            file=sys.stderr,
        )
        return 3

    try:
        created = export_song(
            selected,
            transcript,
            srt,
            dest_root,
            layout=args.layout,
            force=args.force,
            dry_run=args.dry_run,
            keep_names=args.keep_names,
            with_extras=args.with_extras,
        )
    except FileExistsError as exc:
        sys.stdout.flush()
        print(f"ERROR: {exc} (use --force to overwrite)", file=sys.stderr)
        return 4
    except (FileNotFoundError, OSError, tarfile.TarError) as exc:
        sys.stdout.flush()
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"\nDone: {len(created)} file(s){' (dry run)' if args.dry_run else ''}.")
    sys.stdout.flush()
    if transcript is None:
        print("WARNING: no ABC transcript was found for this song.", file=sys.stderr)
    if srt is None:
        print("WARNING: no SRT was found for this song.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
