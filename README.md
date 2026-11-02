# Arabic Music Discography Graph

A text-only dataset that links Arabic music **works**, **recordings** and derived
**artifacts** (lyrics, SRT timings, ABC transcriptions, audio captions) without
redistributing any audio or images.

- Design: [`DESIGN.md`](DESIGN.md)
- Implementation plan: [`PLAN.md`](PLAN.md)
- SQL schema: [`schema/core.sql`](schema/core.sql)
- Built dataset: [`dataset/v0.1/`](dataset/v0.1/) (published copy)
- Coverage report: [`reports/COVERAGE.md`](reports/COVERAGE.md)
- Validation report: [`reports/VALIDATION.md`](reports/VALIDATION.md)

## What is in v0.1

| Item | Count |
|---|---:|
| Unique recordings (content-addressed by SHA-256) | 328 |
| Album tracks | 236 |
| SRT timings | 167 |
| ABC transcripts | 236 (227 ok / 9 flagged no-abc) |
| Captions | 230 |
| Gold lyric–work links | 177 (174 seed + 3 extra exact) |

The 343 audio objects contain 15 byte-identical duplicates (14 `p0`/`pd` +
1 `pd`/TODO), so content-addressed identity yields 328 unique recordings.

All audio references are **relative paths** from the source bucket. The audio
itself is never copied into the dataset.

## Quick start

```bash
make bootstrap          # install Python deps, check ffprobe
make scan               # GCS + GitHub manifest (streams audio through SHA-256)
make core               # artists / albums / works / recordings / legacy ids
make links              # ABC, SRT, captions, yue2 priors, YouTube ids
make lyrics             # lyrics + gold lyric→work links
make package            # build/dataset/v0.1
make validate           # automated QA
make report             # coverage + manual review sample
make publish            # copy to dataset/v0.1 and upload to GCS (optional)
```

Or run the whole build:

```bash
make build
```

The build is pinned by `config/settings.yaml` (bucket, prefixes, GitHub commits).
The scan phase streams each audio object through SHA-256 and `ffprobe`; it never
stores audio. Text artifacts are cached under `build/cache/gcs/` for repeat runs.

## Using the dataset

```python
import duckdb

con = duckdb.connect()
rows = con.execute("""
SELECT r.recording_id, w.title_ar, a.name_lat, t.n_words, c.suno_prompt
FROM 'dataset/v0.1/catalog/recordings.parquet' r
LEFT JOIN 'dataset/v0.1/catalog/works.parquet' w USING (work_id)
LEFT JOIN 'dataset/v0.1/catalog/artists.parquet' a USING (artist_id)
LEFT JOIN 'dataset/v0.1/artifacts/timings.parquet' t
  ON t.recording_id = r.recording_id AND t.is_preferred
LEFT JOIN 'dataset/v0.1/artifacts/captions.parquet' c
  ON c.recording_id = r.recording_id
WHERE r.collection = 'discography_album'
LIMIT 10
""").fetchdf()
```

Resolve an audio row only if you have bucket access:

```python
path = f"gs://{row.audio_bucket}/{row.audio_path}"
```

## Repository layout

```text
src/
  scan_gcs.py          # GCS manifest + streaming hashes + ffprobe
  scan_github.py       # pinned source clones + file hashes
  build_core.py        # artists / albums / works / recordings / legacy_ids
  link_exact.py        # ABC / SRT / captions / yue2 / YouTube links
  ingest_lyrics.py     # lyrics + gold lyric→work links
  package_dataset.py   # Parquet + JSONL + coverage + splits + card
  validate.py          # automated QA
  report.py            # coverage report + manual review sample
  publish.py           # local copy + immutable GCS publish
config/
  settings.yaml        # bucket, prefixes, pinned commits, thresholds
  collections.yaml     # collection definitions
  artist_aliases.csv   # transliteration / Arabic aliases
  manual_links.csv     # reviewed manual overrides
tests/                 # unit + integration tests (skip when build/ is absent)
schema/core.sql        # DuckDB-compatible DDL
```

## Rights

No audio, cover art or other media is redistributed. Lyrics come from public web
sources; every row keeps its source URL. Set
`package.lyrics_text_policy: hash` in `config/settings.yaml` to publish only
hashes, statistics and source URLs instead of the text.

See [`dataset/v0.1/DATASET_CARD.md`](dataset/v0.1/DATASET_CARD.md) for details,
known limitations and the full coverage report.
