"""Unit tests for deterministic normalisation, path parsing and IDs."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.common import (  # noqa: E402
    album_id,
    classify_collection,
    clean_title,
    extract_youtube_id,
    normalize_text,
    parse_audio_path,
    recording_id,
    work_id,
)


def test_normalize_arabic_and_latin():
    assert normalize_text("أهلاً بِكَ") == "اهلا بك"
    assert normalize_text("  Baddy   Doub!!! ") == "baddy doub"
    assert normalize_text("Fadl’s Song") == "fadl s song"


def test_clean_title_removes_prefix_and_youtube_id():
    assert clean_title("01. Baddy Doub.mp3") == "Baddy Doub"
    assert clean_title("Ya Habibi Ta'ala _IKH1hS-JZT8.mp3") == "Ya Habibi Ta'ala"


def test_extract_youtube_id():
    assert extract_youtube_id("Abdullah Al Rowaished - Elly Nasak _IKH1hS-JZT8.mp3") == "IKH1hS-JZT8"
    assert extract_youtube_id("Kelly_Clarkson_-_Since_U_Been_Gone_Official_Video__R7UrFYvl5TE.mp3") == "R7UrFYvl5TE"
    assert extract_youtube_id("No video id here.mp3") is None


def test_classify_collection():
    assert classify_collection("DISCOGRAPHY/ELISSA/Elissa - 1998 Baddy Doub/01. Baddy Doub.mp3") == "discography_album"
    assert classify_collection("DISCOGRAPHY/FADL_SHAKER/Fadl Shaker.Full Discography.BY.RoMaNTiCPoET/01.Walla Zaman.1998/01.Meta Habibi.mp3") == "discography_album"
    assert classify_collection("DISCOGRAPHY/p0/002-27667a8c-9d22-4540-b503-b43f4ee9745f.m4a") == "spotify_p0"
    assert classify_collection("DISCOGRAPHY/TODO/TODO_01/x.mp3") == "youtube_ref"


def test_parse_elissa_path():
    p = parse_audio_path("DISCOGRAPHY/ELISSA/Elissa - 1998 Baddy Doub/01. Baddy Doub.mp3")
    assert p["artist_name"] == "Elissa"
    assert p["album_title"] == "Baddy Doub"
    assert p["album_year"] == 1998
    assert p["track_no"] == 1
    assert p["title"] == "Baddy Doub"
    assert p["collection"] == "discography_album"


def test_parse_fadl_path():
    p = parse_audio_path(
        "DISCOGRAPHY/FADL_SHAKER/Fadl Shaker.Full Discography.BY.RoMaNTiCPoET/"
        "06.Sidi Rohi.2003/01.Ya Habibi Ta'ala.mp3"
    )
    assert p["artist_name"] == "Fadl Shaker"
    assert p["album_title"] == "Sidi Rohi"
    assert p["album_year"] == 2003
    assert p["disc_no"] == 6
    assert p["track_no"] == 1
    assert p["title"] == "Ya Habibi Ta'ala"


def test_parse_spotify_and_youtube():
    p = parse_audio_path("DISCOGRAPHY/p0/002-27667a8c-9d22-4540-b503-b43f4ee9745f.m4a")
    assert p["collection"] == "spotify_p0"
    assert p["title"] is None
    p = parse_audio_path(
        "DISCOGRAPHY/TODO/TODO_01/Abdullah Al Rowaished - Elly Nasak _IKH1hS-JZT8.mp3"
    )
    assert p["youtube_id"] == "IKH1hS-JZT8"
    assert p["artist_name"] == "Abdullah Al Rowaished"
    assert p["title"] == "Elly Nasak"


def test_ids_are_deterministic():
    assert work_id("art_elissa", "Baddy Doub") == work_id("art_elissa", "  baddy  doub ")
    assert album_id("art_elissa", "Baddy Doub", 1998) == album_id("art_elissa", "baddy doub", 1998)
    assert recording_id("a" * 64, "x") == "rec_" + "a" * 16
