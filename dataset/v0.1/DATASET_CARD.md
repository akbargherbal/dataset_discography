# Arabic Music Discography Graph — v0.1

**Snapshot:** `snap_83c2acbb6a0a4466f3e5fd2d`  
**Pipeline:** `0.1.0`  
**Generated:** 2026-11-02T11:27:55.607535  
**Source bucket:** `akbar-december-2024-backup`

## What this is

A text-only catalog and artifact graph for Arabic music. It links songs (works), specific audio performances (recordings), and derived text (lyrics, SRT timings, ABC transcriptions, audio captions) without shipping any audio or images.

Audio is referenced by **bucket + relative path + SHA-256** only:

```python
path = f"gs://{row.audio_bucket}/{row.audio_path}"
```

## Counts

| Item | Count |
|---|---:|
| Unique recordings | 328 |
| Album tracks | 236 |
| Artists | 14 |
| Albums | 23 |
| Works | 224 |
| SRT timings | 167 |
| ABC transcripts | 236 |
| Captions | 230 |
| Gold lyric–work links | 177 |
| Unresolved entities | 66 |
| Dataset size | 3.47 MiB |

## Sources

| Source | What was used |
|---|---|
| GCS `DISCOGRAPHY/` | audio manifest, hashes, ffprobe metadata (no audio bytes) |
| GCS `SRT/v1/` | SRT/VTT/words.json timings (167 tracks, 5 runs) |
| GCS `TRANSCRIPTS/` | ABC/SheetSage2 JSON (236 tracks; 23 albums) |
| `music_captioning_oct_2026` | ACE-Step captions and Suno prompts (230 real tracks) |
| `yue2-karaoke-align` | legacy track ids, title map and prior links |
| `arabic-music-lyrics` | public lyrics corpus + fnanen/kalimataghani scrapes |
| `REF/maqam_analysis` | YouTube-id evidence only (full analysis deferred to v0.2) |

Pinned commits:

```json
{
  "yue2": "ad17c8fe6fc127456f0af4f79c68d4480c10ac8b",
  "mcap": "dd7b7d2035a2d625d1db79d4926ffab71c9ad4d6",
  "lyrics": "74405c5755e3ba8ea53a4272a338f6d35c62b21c"
}
```

## Identity model

* `recording_id` is content-addressed: `rec_<sha256[:16]>`.
* `work_id` groups performances of the same song; transliterations are resolved through a title map and an alias table.
* Source-specific ids (yue2, mcap, SRT, ABC, YouTube, Spotify, GCS) are kept in `catalog/legacy_ids.parquet`.
* `links` is the auditable edge list; every row has `method`, `confidence` and JSON `evidence`.

## Coverage

* `has_lyrics`: 54.0% of recordings
* `has_timing`: 50.9% of recordings
* `has_transcript`: 72.0% of recordings
* `has_caption`: 70.1% of recordings

Unresolved reasons:

```json
{
  "missing_lyrics": 49,
  "no_lyrics_match": 5,
  "ambiguous_lyrics": 5,
  "missing_lyrics_text": 4,
  "instrumental_no_lyrics": 3
}
```

## Known limitations (v0.1)

* Audio and cover images are **not** included; only relative paths and hashes.
* The 343 audio objects contain 15 byte-identical duplicates (14 p0/pd + 1 pd/TODO), so content-addressed identity yields 328 unique recordings.
* 79 unique `p0`/`pd` Spotify recordings have no metadata: `work_id = NULL`, `unresolved_reason = no_metadata`. One additional `pd` file is a byte duplicate of a TODO reference and is canonicalised to it.
* 4 title-map matches have no lyric text in the pinned lyrics sources; they are kept as title-level rows (`4` rows with missing text).
* SRT artifacts come from several runs; `is_preferred` selects one per recording.
* The ABC source produced 227 ok tracks and 9 `no-abc` tracks; the latter are kept as `flagged` transcripts with no ABC text.
* Generated `arabmaqamrock` material and full maqam analysis are deferred to v0.2.
* Transcript/caption model revisions are recorded from the source indexes.

## Rights and privacy

* No audio, cover art or other media is redistributed.
* Lyrics come from public web sources (fnanen, kalimataghani, Genius and the public `arabic-music-lyrics` corpus); the original source URL is kept on every lyrics row. Check each source's terms before redistributing the text in a commercial product. Set `package.lyrics_text_policy: hash` in `config/settings.yaml` to publish hashes and source URLs instead of text.
* ABC transcriptions and captions are model outputs; model names and revisions are stored in the corresponding tables.

## Usage

```sql
-- DuckDB
SELECT r.recording_id, w.title_ar, l.source, t.n_words, c.suno_prompt
FROM 'catalog/recordings.parquet' r
LEFT JOIN 'catalog/works.parquet' w USING (work_id)
LEFT JOIN 'artifacts/lyrics.parquet' l
  ON l.work_id = w.work_id AND l.is_primary
LEFT JOIN 'artifacts/timings.parquet' t
  ON t.recording_id = r.recording_id AND t.is_preferred
LEFT JOIN 'artifacts/captions.parquet' c
  ON c.recording_id = r.recording_id
WHERE r.collection = 'discography_album';
```

## Reproduction

```bash
make bootstrap
make build
make validate
```

The build is pinned by `manifest.json.snapshot_id` and the source commits above. No audio is downloaded permanently at any stage.
