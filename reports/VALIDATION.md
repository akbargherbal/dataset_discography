# Validation report

Dataset: `dataset/v0.1`

* checks: 44
* failed: 0
* warnings: 2

- **PASS** `required_files` — all present
- **PASS** `schema_artists` — missing=[] extra=[]
- **PASS** `schema_albums` — missing=[] extra=[]
- **PASS** `schema_works` — missing=[] extra=[]
- **PASS** `schema_recordings` — missing=[] extra=[]
- **PASS** `schema_legacy_ids` — missing=[] extra=[]
- **PASS** `schema_lyrics` — missing=[] extra=[]
- **PASS** `schema_timings` — missing=[] extra=[]
- **PASS** `schema_transcripts` — missing=[] extra=[]
- **PASS** `schema_captions` — missing=[] extra=[]
- **PASS** `schema_links` — missing=[] extra=[]
- **PASS** `schema_files` — missing=[] extra=[]
- **PASS** `schema_coverage` — missing=[] extra=[]
- **PASS** `schema_splits` — missing=[] extra=[]
- **PASS** `recording_id_unique`
- **PASS** `work_id_unique`
- **PASS** `album_id_unique`
- **PASS** `recording_work_fk` — 0 recordings point to missing works
- **PASS** `null_work_has_reason` — 0 recordings have NULL work_id and no reason
- **PASS** `timings_recording_fk` — 0 rows point to missing recordings
- **PASS** `transcripts_recording_fk` — 0 rows point to missing recordings
- **PASS** `captions_recording_fk` — 0 rows point to missing recordings
- **PASS** `lyrics_work_fk` — 0 lyrics point to missing works
- **PASS** `link_endpoints` — 0 dangling link endpoints
- **PASS** `audio_path_relative` — 0 non-relative audio paths
- **PASS** `no_audio_uri_column`
- **PASS** `timings_paths_relative` — 0 non-relative paths
- **PASS** `transcripts_paths_relative` — 0 non-relative paths
- **PASS** `no_media_files` — found []
- **PASS** `timings_monotonic` — 0 invalid timings
- **PASS** `abc_structure` — 0 invalid ABC rows; 9 flagged no-abc rows
- **PASS** `gold_confidence` — 0 gold links below threshold
- **PASS** `confidence_range` — 0 out-of-range confidence values
- **PASS** `links_have_evidence` — all links carry evidence
- **PASS** `split_unique_work`
- **PASS** `split_values` — values=['test', 'train', 'val']
- **PASS** `split_files_match` — split parquet files do not match splits.parquet
- **PASS** `split_no_leakage` — 0 works in more than one split
- **PASS** `manifest_hashes` — 0 mismatches
- **PASS** `dataset_size` — 3.48 MiB
- **PASS** `snapshot_id` — snap_83c2acbb6a0a4466f3e5fd2d
- **WARN** `expected_counts` — deltas={'recordings': -1, 'album_tracks': 0, 'timings': 0, 'transcripts': 0, 'captions': 0, 'lyrics_gold': 3}
- **WARN** `lyrics_gold_count` — deltas={'recordings': -1, 'album_tracks': 0, 'timings': 0, 'transcripts': 0, 'captions': 0, 'lyrics_gold': 3}
- **PASS** `unresolved_reasons` — every unresolved row has a reason
