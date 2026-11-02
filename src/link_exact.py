"""Phase 3 — exact links for ABC transcripts, SRT timings and captions.

Every link records `method`, `confidence` and `evidence`, following
DESIGN.md §4.  Links at or above the gold threshold are flagged `is_gold`.

Usage:
    python -m src.link_exact --core build/core --out build/links
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd

from .common import (
    Config,
    caption_id,
    clean_title,
    conform_table,
    ensure_dir,
    extract_youtube_id,
    gcs_uri_to_relative,
    link_id,
    load_abc_tracks,
    load_config,
    load_mcap,
    load_srt_corpus,
    load_yue2,
    normalize_text,
    read_gcs_json,
    read_gcs_text,
    recording_id,
    sha256_text,
    timing_id,
    transcript_id,
    utcnow,
    write_json,
    write_parquet,
)
from .common import json_dumps


def _key(album: str | None, file: str | None) -> str:
    return normalize_text(album) + "|" + normalize_text(file)


def _recordings_index(recordings: pd.DataFrame) -> tuple[dict, dict, dict]:
    by_sha: dict[str, dict] = {}
    by_path: dict[str, dict] = {}
    by_key: dict[str, dict] = {}
    for _, r in recordings.iterrows():
        d = r.to_dict()
        by_sha[str(r["audio_sha256"])] = d
        by_path[str(r["audio_path"])] = d
        parts = str(r["audio_path"]).split("/")
        if len(parts) >= 2:
            by_key[_key(parts[-2], parts[-1])] = d
    return by_sha, by_path, by_key


def _link(src_type: str, src_id: str, dst_type: str, dst_id: str,
          relation: str, method: str, confidence: float,
          evidence: dict, is_gold: bool, now) -> dict:
    return {
        "link_id": link_id(src_type, src_id, dst_type, dst_id, relation, method),
        "src_type": src_type,
        "src_id": src_id,
        "dst_type": dst_type,
        "dst_id": dst_id,
        "relation": relation,
        "method": method,
        "confidence": float(confidence),
        "evidence": json_dumps(evidence),
        "is_gold": bool(is_gold and confidence >= 0.95),
        "created_at": now,
    }


def _timing_qa(words: dict, duration: float | None) -> dict:
    ws = words.get("words", []) or []
    starts = [float(w["start"]) for w in ws if w.get("start") is not None]
    ends = [float(w["end"]) for w in ws if w.get("end") is not None]
    monotone = all(starts[i] >= starts[i - 1] for i in range(1, len(starts))) and all(
        ends[i] >= ends[i - 1] for i in range(1, len(ends))
    )
    overlaps = sum(1 for i in range(1, len(starts)) if starts[i] < ends[i - 1])
    gaps = [max(0.0, starts[i] - ends[i - 1]) for i in range(1, len(starts))]
    qa = dict(words.get("qa", {}) or {})
    qa.update({
        "monotone": bool(monotone),
        "overlaps": int(overlaps),
        "max_gap_s": round(max(gaps), 3) if gaps else 0.0,
        "n_words": len(ws),
        "n_words_aligned": words.get("n_words_aligned"),
        "first_start_s": starts[0] if starts else None,
        "last_end_s": ends[-1] if ends else None,
        "warnings": words.get("warnings", []),
    })
    if duration and starts and ends:
        qa["coverage"] = round(max(0.0, ends[-1] - starts[0]) / float(duration), 4)
        qa["duration_ok"] = bool(ends[-1] <= float(duration) + 2.0)
    return qa


def _qa_score(qa: dict, readiness: str | None) -> float:
    score = 0.0
    score += 0.4 if qa.get("monotone") else 0.0
    score += 0.2 if not qa.get("overlaps") else 0.0
    score += 0.2 if qa.get("duration_ok") else 0.0
    score += {"ready": 0.2, "needs_cleanup": 0.1, "needs_expansion": 0.0}.get(
        readiness or "", 0.0
    )
    return round(score, 4)


def link(
    config: Config,
    core_dir: Path,
    out_dir: Path,
    manifest_dir: Path,
    sources_dir: Path | None = None,
    cache_dir: Path | None = None,
) -> dict:
    ensure_dir(out_dir)
    now = utcnow()
    recordings = pd.read_parquet(core_dir / "recordings.parquet")
    works = pd.read_parquet(core_dir / "works.parquet")
    legacy = pd.read_parquet(core_dir / "legacy_ids.parquet")
    files = pd.read_parquet(manifest_dir / "files.parquet")
    by_sha, by_path, by_key = _recordings_index(recordings)
    work_ids = set(works["work_id"].astype(str))

    sources = Path(sources_dir) if sources_dir else config.sources_dir
    yue2 = load_yue2(sources)
    inventory = yue2["inventory"]
    title_map = yue2["title_map"]
    corpus = yue2["corpus"]
    _, mcap_albums = load_mcap(sources)
    abc_tracks = load_abc_tracks(config, files, cache_dir=cache_dir)

    links: list[dict] = []
    unresolved: list[dict] = []
    gold_threshold = float(config.thresholds["gold"])

    # ------------------------------------------------------------------
    # ABC transcripts
    # ------------------------------------------------------------------
    transcripts: list[dict] = []
    seen_transcripts: set[str] = set()
    for t in abc_tracks:
        rec = None
        method, confidence = None, None
        if t.get("audio_sha256") and str(t["audio_sha256"]) in by_sha:
            rec = by_sha[str(t["audio_sha256"])]
            method, confidence = "sha256_exact", 1.00
        else:
            rel = gcs_uri_to_relative(t.get("source_uri"))
            if rel and rel in by_path:
                rec = by_path[rel]
                method, confidence = "source_uri", 0.99
            else:
                parts = str(t.get("track_id", "")).split("/")
                if len(parts) >= 2:
                    rec = by_key.get(_key(parts[-2].split("-")[0], None))
        if rec is None:
            unresolved.append({
                "entity_type": "abc_track",
                "entity_id": t.get("track_id") or t.get("source_uri"),
                "collection": "discography_album",
                "title": t.get("title"),
                "reason": "abc_unmatched",
                "details": json_dumps({"source_uri": t.get("source_uri"),
                                       "audio_sha256": t.get("audio_sha256")}),
            })
            continue
        abc_text = t.get("abc") or ""
        artifact_sha = sha256_text(abc_text)
        tr_id = transcript_id(rec["recording_id"], str(t.get("model") or ""),
                              str(t.get("model_revision") or ""), artifact_sha)
        if tr_id in seen_transcripts:
            continue
        seen_transcripts.add(tr_id)
        raw_status = str(t.get("transcribe_status") or t.get("status") or "ok")
        # The source uses `no-abc` for tracks where SheetSage2 produced no
        # transcription.  Keep it in the schema's `flagged` bucket while the
        # raw value remains in the link evidence.
        status = "ok" if raw_status == "ok" else "flagged"
        transcripts.append({
            "transcript_id": tr_id,
            "recording_id": rec["recording_id"],
            "album_id": rec.get("album_id"),
            "abc_text": abc_text,
            "abc_path": t.get("abc_path"),
            "status": status,
            "model": t.get("model"),
            "model_revision": t.get("model_revision"),
            "duration_s": t.get("duration_s") or rec.get("duration_s"),
            "gpu_seconds": t.get("gpu_seconds"),
            "rtf": t.get("rtf"),
            "artifact_sha256": artifact_sha,
            "created_at": now,
        })
        links.append(_link("recording", rec["recording_id"], "transcript", tr_id,
                           "has_transcript", method or "filename_album",
                           confidence or 0.95,
                           {"abc_path": t.get("abc_path"),
                            "audio_sha256": t.get("audio_sha256"),
                            "source_uri": t.get("source_uri"),
                            "status": status},
                           (confidence or 0.95) >= gold_threshold, now))

    # ------------------------------------------------------------------
    # SRT timings
    # ------------------------------------------------------------------
    # Model/revision per run from manifest.jsonl.
    run_meta: dict[str, dict] = {}
    for _, f in files[files["relative_path"].str.match(r"^SRT/v1/[^/]+/manifest\.jsonl$")].iterrows():
        rel = f["relative_path"]
        try:
            text = read_gcs_text(config, rel, cache_dir=cache_dir)
        except Exception:
            continue
        run = rel.split("/")[2]
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = gcs_uri_to_relative(row.get("uri")) or row.get("name")
            run_meta[str(key)] = row

    timings: list[dict] = []
    timing_candidates: list[dict] = []
    for row in corpus:
        parts = str(row.get("track_id", "")).split("/")
        rec = by_key.get(_key(parts[-2], parts[-1])) if len(parts) >= 2 else None
        if rec is None:
            unresolved.append({
                "entity_type": "srt_track",
                "entity_id": row.get("track_id"),
                "collection": "discography_album",
                "title": row.get("lyric_title"),
                "reason": "srt_unmatched",
                "details": json_dumps({"srt": row.get("srt")}),
            })
            continue
        srt_rel = gcs_uri_to_relative(row.get("srt"))
        vtt_rel = gcs_uri_to_relative(row.get("vtt"))
        words_rel = gcs_uri_to_relative(row.get("words_json"))
        words_payload = {}
        if words_rel:
            try:
                words_payload = read_gcs_json(config, words_rel, cache_dir=cache_dir)
            except Exception:
                words_payload = {}
        duration = None
        try:
            duration = float(row.get("duration_s")) if row.get("duration_s") not in (None, "") else rec.get("duration_s")
        except (TypeError, ValueError):
            duration = rec.get("duration_s")
        qa = _timing_qa(words_payload, duration)
        meta = run_meta.get(str(srt_rel), {})
        run_id = str(row.get("run") or meta.get("run") or "unknown")
        artifact_sha = None
        if srt_rel:
            m = files[files["relative_path"] == srt_rel]
            if not m.empty:
                artifact_sha = m.iloc[0].get("sha256")
        if not artifact_sha and srt_rel:
            try:
                artifact_sha = sha256_text(read_gcs_text(config, srt_rel, cache_dir=cache_dir))
            except Exception:
                artifact_sha = sha256_text(str(row.get("track_id")))
        tm_id = timing_id(rec["recording_id"], run_id, artifact_sha)
        candidate = {
            "timing_id": tm_id,
            "recording_id": rec["recording_id"],
            "run_id": run_id,
            "srt_path": srt_rel,
            "vtt_path": vtt_rel,
            "words_path": words_rel,
            "n_words": qa.get("n_words") or words_payload.get("n_words_aligned"),
            "duration_s": duration,
            "model": meta.get("model"),
            "model_revision": meta.get("revision"),
            "qa": json_dumps(qa),
            "is_preferred": False,
            "artifact_sha256": artifact_sha,
            "created_at": now,
            "_readiness": row.get("readiness"),
            "_qa_score": _qa_score(qa, row.get("readiness")),
        }
        timing_candidates.append(candidate)

    # One preferred timing per recording: best QA, then most words.
    order = pd.DataFrame(timing_candidates)
    preferred: set[str] = set()
    if not order.empty:
        order = order.sort_values(
            ["recording_id", "_qa_score", "n_words"],
            ascending=[True, False, False],
        )
        preferred = set(order.groupby("recording_id", sort=False).head(1)["timing_id"])
    for t in timing_candidates:
        t["is_preferred"] = t["timing_id"] in preferred
        timings.append(t)
        links.append(_link("recording", t["recording_id"], "timing", t["timing_id"],
                           "has_timing", "legacy_track_id", 0.98,
                           {"run_id": t["run_id"], "srt_path": t["srt_path"],
                            "qa_score": t["_qa_score"], "is_preferred": t["is_preferred"]},
                           True, now))

    # ------------------------------------------------------------------
    # Captions
    # ------------------------------------------------------------------
    captions: list[dict] = []
    seen_captions: set[str] = set()
    for album_key, payload in mcap_albums.items():
        parts = album_key.split("/")
        if len(parts) < 2 or parts[0] == "ARAB_MAQAM_ROCK":
            continue
        for t in (payload or {}).get("tracks", []) or []:
            rec = by_key.get(_key(parts[1], t.get("file")))
            if rec is None:
                unresolved.append({
                    "entity_type": "caption_track",
                    "entity_id": f"{album_key}/{t.get('file')}",
                    "collection": "discography_album",
                    "title": t.get("file"),
                    "reason": "caption_unmatched",
                    "details": json_dumps({"album_key": album_key}),
                })
                continue
            artifact_sha = sha256_text(json_dumps(t))
            cap_id = caption_id(rec["recording_id"],
                                "ACE-Step/acestep-captioner", "v1", artifact_sha)
            if cap_id in seen_captions:
                continue
            seen_captions.add(cap_id)
            captions.append({
                "caption_id": cap_id,
                "recording_id": rec["recording_id"],
                "model": "ACE-Step/acestep-captioner",
                "model_revision": "v1",
                "instruments": t.get("instruments") or [],
                "vocals": t.get("vocals") or [],
                "production": t.get("production") or [],
                "caption_text": t.get("caption"),
                "suno_prompt": t.get("suno_prompt") or payload.get("suno_prompt"),
                "caption_seconds": t.get("caption_seconds"),
                "is_generated": False,
                "artifact_sha256": artifact_sha,
                "created_at": now,
            })
            links.append(_link("recording", rec["recording_id"], "caption", cap_id,
                               "has_caption", "legacy_track_id", 0.98,
                               {"album_key": album_key, "file": t.get("file"),
                                "caption_seconds": t.get("caption_seconds")},
                               True, now))

    # ------------------------------------------------------------------
    # yue2 existing index as a prior (not ground truth)
    # ------------------------------------------------------------------
    tm_by_track = {str(r["track_id"]): r.to_dict() for _, r in title_map.iterrows()}
    for _, inv in inventory.iterrows():
        parts = str(inv["track_id"]).split("/")
        rec = by_key.get(_key(parts[-2], parts[-1])) if len(parts) >= 2 else None
        if rec is None or pd.isna(rec.get("work_id")):
            continue
        tm = tm_by_track.get(str(inv["track_id"]), {})
        if tm.get("decision") == "match":
            links.append(_link("recording", rec["recording_id"], "work", rec["work_id"],
                               "same_work", "existing_index", 0.90,
                               {"track_id": inv["track_id"],
                                "lyric_title": tm.get("lyric_title"),
                                "lyric_source": tm.get("lyric_source"),
                                "strict_score": tm.get("strict_score")},
                               False, now))

    # ------------------------------------------------------------------
    # YouTube ids and REF maqam evidence (link only; analysis is v0.2)
    # ------------------------------------------------------------------
    def _evidence_youtube_id(name: str) -> str | None:
        stem = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", name)
        m = re.search(r"(?:__|_)([A-Za-z0-9_-]{11})(?:_(?:MIDI|INS))?$", stem)
        return m.group(1) if m else extract_youtube_id(name)

    ref_evidence: dict[str, str] = {}
    for _, f in files[files["relative_path"].str.startswith("TRANSCRIPTS/REF/")].iterrows():
        rel = str(f["relative_path"])
        if rel.endswith(".json"):
            yid = _evidence_youtube_id(rel.rsplit("/", 1)[-1])
            if yid:
                ref_evidence.setdefault(yid, rel)
    ref_labels: dict[str, dict] = {}
    label_files = files[files["relative_path"].str.endswith("REF_maqam_labels.csv")]
    for _, f in label_files.iterrows():
        try:
            import io

            csv_text = read_gcs_text(config, str(f["relative_path"]), cache_dir=cache_dir)
            df = pd.read_csv(io.StringIO(csv_text))
        except Exception:
            continue
        for _, r in df.iterrows():
            yid = extract_youtube_id(str(r.get("song_id") or r.get("stem") or ""))
            if yid:
                ref_labels[yid] = r.to_dict()

    for _, rec in recordings.iterrows():
        yid = extract_youtube_id(rec.get("audio_path"))
        if not yid:
            continue
        ev = ref_evidence.get(yid)
        label = ref_labels.get(yid, {})
        evidence = {
            "youtube_id": yid,
            "ref_evidence_path": ev,
            "maqam": label.get("maqam") if isinstance(label, dict) else None,
            "maqam_status": label.get("maqam_status") if isinstance(label, dict) else None,
        }
        confidence = 0.99 if (ev or label) else 0.95
        if pd.notna(rec.get("work_id")):
            links.append(_link("recording", rec["recording_id"], "work", rec["work_id"],
                               "references_youtube", "youtube_id", confidence,
                               evidence, confidence >= gold_threshold, now))

    # ------------------------------------------------------------------
    # Write outputs
    # ------------------------------------------------------------------
    timings_df = pd.DataFrame(timings)
    for col in ("_readiness", "_qa_score"):
        if col in timings_df.columns:
            timings_df = timings_df.drop(columns=[col])
    transcripts_df = pd.DataFrame(transcripts)
    captions_df = pd.DataFrame(captions)
    links_df = pd.DataFrame(links).drop_duplicates("link_id", keep="first")
    unresolved_df = pd.DataFrame(unresolved)

    write_parquet(conform_table(transcripts_df, "transcripts"), out_dir / "transcripts.parquet")
    write_parquet(conform_table(timings_df, "timings"), out_dir / "timings.parquet")
    write_parquet(conform_table(captions_df, "captions"), out_dir / "captions.parquet")
    write_parquet(conform_table(links_df, "links"), out_dir / "links.parquet")
    write_parquet(unresolved_df, out_dir / "unresolved.parquet")

    # Any link to a work must point at a real work.
    bad = links_df[(links_df["dst_type"] == "work") &
                   (~links_df["dst_id"].isin(work_ids))]
    if not bad.empty:
        raise RuntimeError(f"{len(bad)} links point at unknown works")

    summary = {
        "counts": {
            "transcripts": int(len(transcripts_df)),
            "timings": int(len(timings_df)),
            "captions": int(len(captions_df)),
            "links": int(len(links_df)),
            "gold_links": int(links_df["is_gold"].fillna(False).sum()),
            "unresolved": int(len(unresolved_df)),
        },
        "transcript_status": transcripts_df["status"].value_counts().to_dict() if not transcripts_df.empty else {},
        "timing_runs": timings_df["run_id"].value_counts().to_dict() if not timings_df.empty else {},
        "links_by_method": links_df["method"].value_counts().to_dict() if not links_df.empty else {},
        "links_by_relation": links_df["relation"].value_counts().to_dict() if not links_df.empty else {},
        "gold_threshold": gold_threshold,
        "pipeline_version": config.pipeline_version,
    }
    write_json(summary, out_dir / "summary.json")
    print(
        f"[link_exact] transcripts={len(transcripts_df)} timings={len(timings_df)} "
        f"captions={len(captions_df)} links={len(links_df)} "
        f"gold={summary['counts']['gold_links']} unresolved={len(unresolved_df)}",
        flush=True,
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config/settings.yaml")
    ap.add_argument("--core", default="build/core")
    ap.add_argument("--out", default="build/links")
    ap.add_argument("--manifest", default="build/manifest")
    ap.add_argument("--sources", default=None)
    ap.add_argument("--cache-dir", default="build/cache/gcs")
    args = ap.parse_args(argv)
    config = load_config(args.config)
    link(
        config,
        core_dir=Path(args.core),
        out_dir=Path(args.out),
        manifest_dir=Path(args.manifest),
        sources_dir=Path(args.sources) if args.sources else None,
        cache_dir=Path(args.cache_dir) if args.cache_dir else None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
