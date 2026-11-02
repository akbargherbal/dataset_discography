"""Phase 4 — ingest lyrics and link them to works.

Sources:
  * arabic-music-lyrics/data/processed/arabic_lyrics.parquet
  * arabic-music-lyrics/lyrics/{fnanen,kalimataghani}/artists/*/*.jsonl
  * yue2 index/lyrics/title_map.csv as the gold seed

The matcher is deliberately conservative:
  * exact normalised title + artist alias -> 0.98
  * fuzzy title (>= 0.95) + artist          -> 0.96
  * fuzzy title (0.90-0.95)                -> 0.92, not gold
  * anything else                          -> unresolved

Usage:
    python -m src.ingest_lyrics --core build/core --out build/artifacts
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz

from .common import (
    Config,
    canonical_artist,
    conform_table,
    ensure_dir,
    json_dumps,
    link_id,
    load_arabic_lyrics_parquet,
    load_artist_aliases,
    load_config,
    load_lyrics_repo,
    load_yue2,
    lyric_id,
    normalize_text,
    sha256_text,
    stable_hash,
    strip_arabic_diacritics,
    utcnow,
    write_json,
    write_parquet,
)


SOURCE_PRIORITY = {
    "fnanen": 0,
    "kalimataghani": 1,
    "genius": 2,
    "spotify_960k": 3,
    "corpus:genius": 4,
    "corpus:wneen": 5,
}


def _track_link_id(lyric: str, work: str, method: str, track_id: str,
                   relation: str = "has_lyrics") -> str:
    """One auditable assertion per title-map track (not just per work)."""
    return "lnk_" + stable_hash("lyric", lyric, "work", work, relation, method, track_id)


def _arabic_ratio(text: str) -> float:
    if not text:
        return 0.0
    letters = [c for c in text if not c.isspace()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if "\u0600" <= c <= "\u06ff") / len(letters)


def _harakat_ratio(text: str) -> float:
    if not text:
        return 0.0
    letters = [c for c in text if "\u0600" <= c <= "\u06ff"]
    if not letters:
        return 0.0
    marks = len(re.findall(r"[\u064b-\u065f\u0670]", text))
    return marks / max(1, len(letters) + marks)


def _counts(text: str) -> tuple[int, int]:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    words = [w for w in re.split(r"\s+", text or "") if w]
    return len(lines), len(words)


def _lyric_row(
    source: str,
    source_id: str,
    title: str | None,
    artist: str | None,
    text: str | None,
    source_url: str | None = None,
    year: int | None = None,
    completeness: str = "full",
    has_translation: bool = False,
    retrieved_at=None,
) -> dict:
    text = text or ""
    n_lines, n_words = _counts(text)
    lid = lyric_id(source, source_id)
    return {
        "lyric_id": lid,
        "work_id": None,
        "recording_id": None,
        "artist_name_raw": artist,
        "title_raw": title,
        "title_ar": title,
        "source": source,
        "source_id": str(source_id),
        "source_url": source_url,
        "text": text if text else None,
        "text_match": normalize_text(text) if text else None,
        "n_lines": n_lines,
        "n_words": n_words,
        "arabic_ratio": round(_arabic_ratio(text), 4),
        "harakat_ratio": round(_harakat_ratio(text), 4),
        "has_translation": bool(has_translation),
        "is_primary": False,
        "completeness": completeness,
        "source_license": "unknown/public-web",
        "retrieved_at": retrieved_at,
        "year": year,
        "_text_hash": sha256_text(normalize_text(text)) if text else None,
    }


def _load_candidates(
    config: Config,
    sources_dir: Path,
    catalog_artists: set[str],
    aliases: dict[str, str],
) -> pd.DataFrame:
    rows: list[dict] = []

    # fnanen + kalimataghani
    for r in load_lyrics_repo(sources_dir, repo_key="lyrics"):
        text = r.get("lyrics") or ""
        if not text.strip():
            continue
        artist_raw = r.get("artist")
        canon = canonical_artist(artist_raw, aliases)
        if canon not in catalog_artists:
            continue
        source = r["_source"]
        source_id = (
            r.get("url")
            or f"{r.get('_artist_dir', 'unknown')}/{r.get('id', '')}"
        )
        rows.append(_lyric_row(
            source=source,
            source_id=source_id,
            title=r.get("title"),
            artist=artist_raw,
            text=text,
            source_url=r.get("url"),
            year=int(r["year"]) if str(r.get("year", "")).isdigit() else None,
            completeness="full",
            retrieved_at=None,
        ))

    # arabic_lyrics.parquet
    ap = load_arabic_lyrics_parquet(sources_dir, repo_key="lyrics")
    if not ap.empty:
        for _, r in ap.iterrows():
            text = r.get("lyrics") or ""
            if not str(text).strip():
                continue
            canon = canonical_artist(r.get("artist"), aliases)
            if canon not in catalog_artists:
                continue
            rows.append(_lyric_row(
                source=str(r.get("source") or "unknown"),
                source_id=str(r.get("source_id") or r.get("source_url") or ""),
                title=r.get("title"),
                artist=r.get("artist"),
                text=str(text),
                source_url=r.get("source_url"),
                year=int(r["year"]) if str(r.get("year", "")).isdigit() else None,
                completeness=str(r.get("lyrics_completeness") or "full"),
                has_translation=bool(r.get("has_translation")),
            ))

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["_source_prio"] = df["source"].map(lambda s: SOURCE_PRIORITY.get(str(s), 9))
    df = df.sort_values(["_source_prio", "lyric_id"]).reset_index(drop=True)
    return df


def _exact_title_index(df: pd.DataFrame) -> dict[str, list[int]]:
    idx: dict[str, list[int]] = {}
    for i, title in enumerate(df["title_ar"].fillna("")):
        key = normalize_text(str(title))
        if key:
            idx.setdefault(key, []).append(i)
    return idx


def ingest(
    config: Config,
    core_dir: Path,
    out_dir: Path,
    links_dir: Path | None = None,
    sources_dir: Path | None = None,
) -> dict:
    ensure_dir(out_dir)
    links_dir = links_dir or (out_dir.parent / "links")
    ensure_dir(links_dir)
    now = utcnow()
    gold_threshold = float(config.thresholds["gold"])

    recordings = pd.read_parquet(core_dir / "recordings.parquet")
    works = pd.read_parquet(core_dir / "works.parquet")
    artists = pd.read_parquet(core_dir / "artists.parquet")
    sources = Path(sources_dir) if sources_dir else config.sources_dir
    _, aliases = load_artist_aliases()
    yue2 = load_yue2(sources)
    title_map = yue2["title_map"]

    catalog_artists = set(artists["name_lat"].astype(str))
    candidates = _load_candidates(config, sources, catalog_artists, aliases)

    if candidates.empty:
        raise SystemExit("no lyrics candidates for catalog artists")

    # Map track_id -> recording/work.
    rec_by_key: dict[str, dict] = {}
    for _, r in recordings.iterrows():
        parts = str(r["audio_path"]).split("/")
        if len(parts) >= 2:
            rec_by_key[normalize_text(parts[-2]) + "|" + normalize_text(parts[-1])] = r.to_dict()
    work_by_id = {str(r["work_id"]): r.to_dict() for _, r in works.iterrows()}

    exact_index = _exact_title_index(candidates)
    candidate_titles = [normalize_text(str(t)) for t in candidates["title_ar"].fillna("")]
    candidate_artists = [
        canonical_artist(a, aliases) for a in candidates["artist_name_raw"].fillna("")
    ]

    def find_candidate(title_ar: str, source: str, artist: str) -> tuple[int | None, float, str]:
        key = normalize_text(title_ar)
        if not key:
            return None, 0.0, "none"
        hits = exact_index.get(key, [])
        if hits:
            # Prefer the same source, then same artist, then first.
            hits = sorted(
                hits,
                key=lambda i: (
                    candidates.iloc[i]["source"] != source,
                    candidate_artists[i] != artist,
                    SOURCE_PRIORITY.get(str(candidates.iloc[i]["source"]), 9),
                ),
            )
            return hits[0], 1.0, "exact"
        # Fuzzy only within the same canonical artist.
        best_i, best_score = None, 0.0
        for i, (ct, ca) in enumerate(zip(candidate_titles, candidate_artists)):
            if ca != artist or not ct:
                continue
            score = fuzz.token_set_ratio(key, ct) / 100.0
            if score > best_score:
                best_i, best_score = i, score
        if best_i is not None and best_score >= 0.90:
            return best_i, best_score, "fuzzy"
        return None, 0.0, "none"

    links: list[dict] = []
    unresolved: list[dict] = []
    linked_lyric_ids: set[str] = set()
    work_primary: dict[str, str] = {}
    gold_count = 0

    # ------------------------------------------------------------------
    # Gold seed: title_map
    # ------------------------------------------------------------------
    for _, tm in title_map.iterrows():
        parts = str(tm.get("track_id", "")).split("/")
        rec = rec_by_key.get(normalize_text(parts[-2]) + "|" + normalize_text(parts[-1])) if len(parts) >= 2 else None
        title_ar = tm.get("lyric_title")
        source = str(tm.get("lyric_source") or "")
        decision = str(tm.get("decision") or "")

        if decision != "match":
            reason = {
                "ambiguous": "ambiguous_lyrics",
                "no_match": "no_lyrics_match",
                "missing": "missing_lyrics",
            }.get(decision, "no_lyrics_match")
            if rec is not None and bool(rec.get("is_instrumental")):
                reason = "instrumental_no_lyrics"
            unresolved.append({
                "entity_type": "recording",
                "entity_id": rec.get("recording_id") if rec else tm.get("track_id"),
                "collection": rec.get("collection") if rec else "discography_album",
                "title": title_ar or tm.get("title_lat"),
                "reason": reason,
                "details": json_dumps({
                    "track_id": tm.get("track_id"),
                    "decision": decision,
                    "candidate_1": tm.get("candidate_1"),
                    "candidate_1_score": tm.get("candidate_1_score"),
                    "candidate_2": tm.get("candidate_2"),
                    "candidate_2_score": tm.get("candidate_2_score"),
                    "lyric_source": source,
                }),
            })
            continue

        if rec is None or pd.isna(rec.get("work_id")):
            unresolved.append({
                "entity_type": "recording",
                "entity_id": tm.get("track_id"),
                "collection": "discography_album",
                "title": title_ar,
                "reason": "work_missing_for_lyrics",
                "details": json_dumps({"track_id": tm.get("track_id")}),
            })
            continue

        work_id = str(rec["work_id"])
        artist = ""
        if rec.get("artist_id"):
            arow = artists[artists["artist_id"] == rec["artist_id"]]
            if not arow.empty:
                artist = str(arow.iloc[0]["name_lat"])

        idx, score, how = find_candidate(str(title_ar), source, artist)
        if idx is not None:
            cand = candidates.iloc[idx]
            lid = str(cand["lyric_id"])
            method = "title_map_gold" if how == "exact" else "title_fuzzy"
            confidence = 0.98 if how == "exact" else (0.96 if score >= 0.95 else 0.92)
            evidence = {
                "track_id": tm.get("track_id"),
                "lyric_title": title_ar,
                "lyric_source": source,
                "matched_lyric_id": lid,
                "match": how,
                "title_score": round(score, 4),
                "strict_score": tm.get("strict_score"),
                "year": cand.get("year"),
                "text_available": bool(cand.get("text")),
            }
            is_gold = confidence >= gold_threshold
            links.append({
                "link_id": _track_link_id(lid, work_id, method, str(tm.get("track_id"))),
                "src_type": "lyric",
                "src_id": lid,
                "dst_type": "work",
                "dst_id": work_id,
                "relation": "has_lyrics",
                "method": method,
                "confidence": float(confidence),
                "evidence": json_dumps(evidence),
                "is_gold": bool(is_gold),
                "created_at": now,
            })
            if is_gold:
                gold_count += 1
            linked_lyric_ids.add(lid)
            # Lyrics belong to the work; only a version-specific recording gets
            # a direct recording_id.
            if rec.get("version_type") in {"live", "remix", "instrumental"}:
                candidates.at[idx, "recording_id"] = rec["recording_id"]
                links.append({
                    "link_id": _track_link_id(
                        lid, rec["recording_id"], "version_specific_lyrics",
                        str(tm.get("track_id")), relation="version_specific_lyrics"
                    ),
                    "src_type": "lyric",
                    "src_id": lid,
                    "dst_type": "recording",
                    "dst_id": rec["recording_id"],
                    "relation": "version_specific_lyrics",
                    "method": "title_map_gold",
                    "confidence": float(confidence),
                    "evidence": json_dumps({"track_id": tm.get("track_id"),
                                            "version_type": rec.get("version_type")}),
                    "is_gold": bool(is_gold),
                    "created_at": now,
                })
            if work_id not in work_primary:
                work_primary[work_id] = lid
        else:
            # The title map is authoritative but the pinned text sources do
            # not contain this text.  Keep a title-level placeholder so the
            # gap is visible and countable.
            source_id = f"title_map/{tm.get('track_id')}"
            placeholder = _lyric_row(
                source="title_map",
                source_id=source_id,
                title=str(title_ar),
                artist=artist,
                text=None,
                source_url=tm.get("lyrics_url"),
                completeness="missing",
            )
            lid = placeholder["lyric_id"]
            candidates = pd.concat([candidates, pd.DataFrame([placeholder])], ignore_index=True)
            linked_lyric_ids.add(lid)
            links.append({
                "link_id": _track_link_id(
                    lid, work_id, "title_map_gold", str(tm.get("track_id"))
                ),
                "src_type": "lyric",
                "src_id": lid,
                "dst_type": "work",
                "dst_id": work_id,
                "relation": "has_lyrics",
                "method": "title_map_gold",
                "confidence": 0.98,
                "evidence": json_dumps({
                    "track_id": tm.get("track_id"),
                    "lyric_title": title_ar,
                    "lyric_source": source,
                    "text_available": False,
                    "note": "title map matched, text not present in pinned sources",
                }),
                "is_gold": True,
                "created_at": now,
            })
            gold_count += 1
            unresolved.append({
                "entity_type": "lyric",
                "entity_id": lid,
                "collection": rec.get("collection"),
                "title": title_ar,
                "reason": "missing_lyrics_text",
                "details": json_dumps({"track_id": tm.get("track_id"),
                                       "lyric_source": source}),
            })
            if work_id not in work_primary:
                work_primary[work_id] = lid

    # ------------------------------------------------------------------
    # Additional exact title matches for works not covered above.
    # ------------------------------------------------------------------
    covered_works = {l["dst_id"] for l in links if l["dst_type"] == "work"}
    for wid, w in work_by_id.items():
        if wid in covered_works:
            continue
        if not w.get("title_norm"):
            continue
        title = w.get("title_ar") or w.get("title_lat")
        artist_id_val = w.get("artist_id")
        arow = artists[artists["artist_id"] == artist_id_val]
        artist = str(arow.iloc[0]["name_lat"]) if not arow.empty else ""
        idx, score, how = find_candidate(str(title or ""), "", artist)
        if idx is None or how != "exact":
            continue
        cand = candidates.iloc[idx]
        lid = str(cand["lyric_id"])
        confidence = 0.98
        links.append({
            "link_id": _track_link_id(lid, wid, "title_exact", wid),
            "src_type": "lyric",
            "src_id": lid,
            "dst_type": "work",
            "dst_id": wid,
            "relation": "has_lyrics",
            "method": "title_exact",
            "confidence": confidence,
            "evidence": json_dumps({
                "work_title": title,
                "matched_lyric_id": lid,
                "title_score": 1.0,
                "text_available": bool(cand.get("text")),
            }),
            "is_gold": True,
            "created_at": now,
        })
        gold_count += 1
        linked_lyric_ids.add(lid)
        if wid not in work_primary:
            work_primary[wid] = lid

    # ------------------------------------------------------------------
    # Final lyrics table
    # ------------------------------------------------------------------
    candidates["work_id"] = None
    candidates["is_primary"] = False
    for l in links:
        if l["dst_type"] != "work":
            continue
        mask = candidates["lyric_id"] == l["src_id"]
        candidates.loc[mask, "work_id"] = l["dst_id"]
    for wid, lid in work_primary.items():
        candidates.loc[candidates["lyric_id"] == lid, "is_primary"] = True
    # Prefer a text-bearing lyric as primary when one exists.
    for wid, group in candidates[candidates["work_id"].notna()].groupby("work_id"):
        with_text = group[group["text"].notna()]
        if not with_text.empty:
            primary = with_text.sort_values(
                ["n_words", "_source_prio"], ascending=[False, True]
            ).iloc[0]["lyric_id"]
            candidates.loc[candidates["work_id"] == wid, "is_primary"] = False
            candidates.loc[candidates["lyric_id"] == primary, "is_primary"] = True

    # Deduplicate exact text per work, keeping the primary candidate first.
    lyrics = candidates.copy()
    if "_source_prio" in lyrics.columns:
        lyrics = lyrics.sort_values(
            ["is_primary", "_source_prio"], ascending=[False, True]
        ).reset_index(drop=True)
    if "_text_hash" in lyrics.columns:
        keep = []
        seen = set()
        for i, r in lyrics.iterrows():
            key = (r.get("work_id"), r.get("_text_hash"))
            if r.get("_text_hash") and pd.notna(r.get("work_id")) and key in seen:
                continue
            if r.get("_text_hash"):
                seen.add(key)
            keep.append(i)
        lyrics = lyrics.loc[keep].reset_index(drop=True)

    # Keep the schema-shape (drop helpers).
    for col in ("_text_hash", "_source_prio"):
        if col in lyrics.columns:
            lyrics = lyrics.drop(columns=[col])
    if "year" in lyrics.columns:
        lyrics = lyrics.drop(columns=["year"])

    links_df = pd.DataFrame(links).drop_duplicates("link_id", keep="first")
    existing_unresolved_path = links_dir / "unresolved.parquet"
    lyric_reasons = {
        "ambiguous_lyrics", "no_lyrics_match", "missing_lyrics",
        "missing_lyrics_text", "work_missing_for_lyrics",
        "instrumental_no_lyrics",
    }
    if existing_unresolved_path.exists():
        existing = pd.read_parquet(existing_unresolved_path)
        # Replace the previous lyric pass, keep ABC/SRT/caption unresolved rows.
        existing = existing[~existing["reason"].isin(lyric_reasons)]
        unresolved_df = pd.concat([existing, pd.DataFrame(unresolved)], ignore_index=True)
    else:
        unresolved_df = pd.DataFrame(unresolved)
    if not unresolved_df.empty:
        unresolved_df = unresolved_df.drop_duplicates(
            ["entity_type", "entity_id", "reason"], keep="first"
        )

    write_parquet(conform_table(lyrics, "lyrics"), out_dir / "lyrics.parquet")
    write_parquet(conform_table(links_df, "links"), links_dir / "lyrics_links.parquet")
    write_parquet(unresolved_df, links_dir / "unresolved.parquet")

    if not links_df.empty:
        gold_work = links_df[
            (links_df["src_type"] == "lyric")
            & (links_df["dst_type"] == "work")
            & links_df["is_gold"].fillna(False)
        ]
    else:
        gold_work = links_df
    summary = {
        "counts": {
            "lyric_rows": int(len(lyrics)),
            "lyric_candidates": int(len(candidates)),
            "lyrics_links": int(len(links_df)),
            "gold_lyrics_links": int(len(gold_work)),
            "works_with_lyrics": int(lyrics["work_id"].notna().sum()),
            "unresolved": int(len(unresolved_df)),
        },
        "sources": lyrics["source"].value_counts().to_dict(),
        "gold_threshold": gold_threshold,
        "pipeline_version": config.pipeline_version,
    }
    write_json(summary, out_dir / "summary.json")
    print(
        f"[ingest_lyrics] lyric_rows={len(lyrics)} links={len(links_df)} "
        f"gold={summary['counts']['gold_lyrics_links']} unresolved={len(unresolved_df)}",
        flush=True,
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config/settings.yaml")
    ap.add_argument("--core", default="build/core")
    ap.add_argument("--out", default="build/artifacts")
    ap.add_argument("--links", default="build/links")
    ap.add_argument("--sources", default=None)
    args = ap.parse_args(argv)
    config = load_config(args.config)
    ingest(
        config,
        core_dir=Path(args.core),
        out_dir=Path(args.out),
        links_dir=Path(args.links),
        sources_dir=Path(args.sources) if args.sources else None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
