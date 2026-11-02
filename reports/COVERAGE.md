# Coverage report

Dataset: `dataset/v0.1`

## Overall

| Metric | Value |
|---|---:|
| Recordings | 328 |
| Works | 224 |
| Links | 997 |
| Unresolved | 66 |
| has_lyrics | 54.0% |
| has_timing | 50.9% |
| has_transcript | 72.0% |
| has_caption | 70.1% |

## By collection

| Collection | Recordings | Lyrics | Timing | Transcript | Caption |
|---|---:|---:|---:|---:|---:|
| discography_album | 236 | 75.0% | 70.8% | 100.0% | 97.5% |
| spotify_p0 | 44 | 0.0% | 0.0% | 0.0% | 0.0% |
| spotify_pd | 35 | 0.0% | 0.0% | 0.0% | 0.0% |
| youtube_ref | 13 | 0.0% | 0.0% | 0.0% | 0.0% |

## Unresolved reasons

| Reason | Count |
|---|---:|
| missing_lyrics | 49 |
| no_lyrics_match | 5 |
| ambiguous_lyrics | 5 |
| missing_lyrics_text | 4 |
| instrumental_no_lyrics | 3 |

## Manual review

A deterministic sample of 50 links is in `reports/manual_review_sample.csv`. Fill `verdict` with `correct` / `wrong` / `unsure`; the file is intentionally blank in v0.1.

Mean link confidence (automated proxy): **97.08%**. Gold threshold: 0.95.
