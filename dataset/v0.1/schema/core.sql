-- Arabic Music Discography Graph — core schema (DuckDB-compatible)
-- Design notes: see ../DESIGN.md
--
-- Principles:
--   * work     = the song as a text/composition identity
--   * recording = one specific audio performance/file
--   * artifact = lyrics, timings, ABC transcript, caption
--   * IDs are immutable; source-specific IDs live in legacy_ids
--   * every artifact stores provenance + model revision + artifact hash

CREATE TABLE artists (
    artist_id        VARCHAR PRIMARY KEY,      -- art_<slug>
    name_lat         VARCHAR NOT NULL,
    name_ar          VARCHAR,
    aliases          VARCHAR[],                -- latin + arabic + transliterations
    country          VARCHAR,
    source           VARCHAR,
    created_at       TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE albums (
    album_id         VARCHAR PRIMARY KEY,      -- alb_<hash>
    artist_id        VARCHAR NOT NULL REFERENCES artists(artist_id),
    title            VARCHAR NOT NULL,
    title_norm       VARCHAR,
    year             INTEGER,
    album_type       VARCHAR,                  -- studio | live | compilation | single | playlist
    source_collection VARCHAR,                 -- discography_album | ...
    source_uri       VARCHAR,
    created_at       TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE works (
    work_id          VARCHAR PRIMARY KEY,      -- w_<hash>; immutable once assigned
    artist_id        VARCHAR NOT NULL REFERENCES artists(artist_id),
    title_lat        VARCHAR,
    title_ar         VARCHAR,
    title_norm       VARCHAR NOT NULL,         -- match key (no diacritics/punctuation)
    year             INTEGER,
    language         VARCHAR,                  -- ar | en | ...
    dialect          VARCHAR,                  -- egyptian | levantine | ...
    genre            VARCHAR,
    maqam            VARCHAR,                  -- optional, from MAQAM_ANALYSIS
    is_medley        BOOLEAN DEFAULT FALSE,
    created_at       TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE recordings (
    recording_id     VARCHAR PRIMARY KEY,      -- rec_<sha256[:16]> when audio hash exists
    work_id          VARCHAR REFERENCES works(work_id),
    artist_id        VARCHAR NOT NULL REFERENCES artists(artist_id),
    album_id         VARCHAR REFERENCES albums(album_id),
    collection       VARCHAR NOT NULL,         -- discography_album | spotify_p0 | spotify_pd
                                               -- | youtube_ref | generated_arabmaqamrock
    legacy_track_id  VARCHAR,                  -- e.g. "Elissa/Album/01. Track.mp3"
    disc_no          INTEGER,
    track_no         INTEGER,
    title_lat        VARCHAR,
    title_ar         VARCHAR,
    duration_s       DOUBLE,
    audio_sha256     VARCHAR,
    audio_md5        VARCHAR,
    audio_crc32c     VARCHAR,
    audio_bucket     VARCHAR,                  -- e.g. akbar-december-2024-backup (optional)
    audio_path       VARCHAR NOT NULL,         -- RELATIVE to bucket root; no gs://, no bytes
                                               -- e.g. DISCOGRAPHY/ELISSA/.../01. Track.mp3
    codec            VARCHAR,
    bitrate_kbps     INTEGER,
    bytes            BIGINT,
    is_instrumental  BOOLEAN DEFAULT FALSE,
    is_generated     BOOLEAN DEFAULT FALSE,
    version_type     VARCHAR,                  -- studio | live | remix | instrumental | generated
    recording_group_id VARCHAR,                -- optional cluster for re-encodes
    audio_fingerprint VARCHAR,                 -- optional Chromaprint
    source_license   VARCHAR,
    created_at       TIMESTAMP DEFAULT current_timestamp
);

-- Source-specific identifiers. Do not try to force them into one canonical string.
CREATE TABLE legacy_ids (
    namespace        VARCHAR NOT NULL,         -- yue2 | mcap | srt | abc | youtube | gcs | spotify
    value            VARCHAR NOT NULL,
    recording_id     VARCHAR REFERENCES recordings(recording_id),
    is_primary       BOOLEAN DEFAULT FALSE,
    note             VARCHAR,
    PRIMARY KEY (namespace, value)
);

-- Medleys / tracks containing more than one work.
CREATE TABLE recording_works (
    recording_id     VARCHAR NOT NULL REFERENCES recordings(recording_id),
    work_id          VARCHAR NOT NULL REFERENCES works(work_id),
    position         INTEGER,
    is_medley        BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (recording_id, work_id)
);

CREATE TABLE lyrics (
    lyric_id         VARCHAR PRIMARY KEY,      -- lyr_<hash(source, source_id)>
    work_id          VARCHAR REFERENCES works(work_id),
    recording_id     VARCHAR REFERENCES recordings(recording_id), -- only if version-specific
    artist_name_raw  VARCHAR,
    title_raw        VARCHAR,
    title_ar         VARCHAR,
    source           VARCHAR NOT NULL,         -- genius | fnanen | kalimataghani | spotify_960k | ...
    source_id        VARCHAR,
    source_url       VARCHAR,
    text             TEXT,
    text_match       TEXT,                     -- normalized copy for matching
    n_lines          INTEGER,
    n_words          INTEGER,
    arabic_ratio     DOUBLE,
    harakat_ratio    DOUBLE,
    has_translation  BOOLEAN,
    is_primary       BOOLEAN DEFAULT FALSE,
    completeness     VARCHAR,                  -- full | partial | needs_expansion | missing
    source_license   VARCHAR,
    retrieved_at     TIMESTAMP
);

CREATE TABLE timings (
    timing_id        VARCHAR PRIMARY KEY,      -- tim_<hash>
    recording_id     VARCHAR NOT NULL REFERENCES recordings(recording_id),
    run_id           VARCHAR NOT NULL,         -- e.g. cleanup3-2026-10-28
    srt_path         VARCHAR,                  -- RELATIVE to bucket root
    vtt_path         VARCHAR,                  -- RELATIVE to bucket root
    words_path       VARCHAR,                  -- RELATIVE to bucket root; .words.json
    n_words          INTEGER,
    duration_s       DOUBLE,
    model            VARCHAR,                  -- e.g. akbargherbal/wav2vec2-large-xlsr-53-arabic
    model_revision   VARCHAR,
    qa               JSON,                     -- monotone, overlaps, coverage, gaps...
    is_preferred     BOOLEAN DEFAULT FALSE,
    artifact_sha256  VARCHAR,
    created_at       TIMESTAMP
);

CREATE TABLE transcripts (
    transcript_id    VARCHAR PRIMARY KEY,      -- trn_<hash>
    recording_id     VARCHAR NOT NULL REFERENCES recordings(recording_id),
    album_id         VARCHAR REFERENCES albums(album_id),
    abc_text         TEXT,                     -- inline text; no external file needed
    abc_path         VARCHAR,                  -- RELATIVE to bucket root (provenance)
    status           VARCHAR,                  -- ok | flagged | failed
    model            VARCHAR,                  -- e.g. m-a-p/SheetSage2
    model_revision   VARCHAR,
    duration_s       DOUBLE,
    gpu_seconds      DOUBLE,
    rtf              DOUBLE,
    artifact_sha256  VARCHAR,
    created_at       TIMESTAMP
);

CREATE TABLE captions (
    caption_id       VARCHAR PRIMARY KEY,      -- cap_<hash>
    recording_id     VARCHAR NOT NULL REFERENCES recordings(recording_id),
    model            VARCHAR,                  -- e.g. ACE-Step/acestep-captioner
    model_revision   VARCHAR,
    instruments      VARCHAR[],
    vocals           VARCHAR[],
    production       VARCHAR[],
    caption_text     TEXT,
    suno_prompt      TEXT,
    caption_seconds  DOUBLE,
    is_generated     BOOLEAN DEFAULT FALSE,
    artifact_sha256  VARCHAR,
    created_at       TIMESTAMP
);

-- Cross-source / derived assertions. Typed FKs remain the primary model;
-- links is the auditable edge list and the graph export source.
CREATE TABLE links (
    link_id          VARCHAR PRIMARY KEY,      -- lnk_<hash>
    src_type         VARCHAR NOT NULL,         -- recording | work | lyric | timing | ...
    src_id           VARCHAR NOT NULL,
    dst_type         VARCHAR NOT NULL,
    dst_id           VARCHAR NOT NULL,
    relation         VARCHAR NOT NULL,         -- has_lyrics | has_timing | has_transcript
                                               -- | has_caption | duplicate_of | same_work
                                               -- | derived_from | references_youtube
    method           VARCHAR NOT NULL,         -- sha256_exact | source_uri | legacy_track_id
                                               -- | youtube_id | filename_album | title_fuzzy | manual
    confidence       DOUBLE NOT NULL,          -- 0..1
    evidence         JSON,                     -- matched fields, durations, scores, URIs
    is_gold          BOOLEAN DEFAULT FALSE,    -- confidence >= threshold and reviewed
    created_at       TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE files (
    file_id          VARCHAR PRIMARY KEY,      -- fil_<hash>
    source           VARCHAR NOT NULL,         -- gcs | github
    bucket           VARCHAR,                  -- GCS bucket; NULL for GitHub
    repo             VARCHAR,                  -- GitHub repo; NULL for GCS
    commit_sha       VARCHAR,                  -- GitHub commit; NULL for GCS
    relative_path    VARCHAR NOT NULL,         -- GCS: relative to bucket root
                                               -- GitHub: relative to repo root
    uri              VARCHAR,                  -- optional full resolver/provenance only
    bytes            BIGINT,
    sha256           VARCHAR,
    md5              VARCHAR,
    crc32c           VARCHAR,
    content_type     VARCHAR,
    generation       VARCHAR,                  -- GCS generation, for change detection
    etag             VARCHAR,
    mtime            TIMESTAMP,
    scanned_at       TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE coverage (
    recording_id     VARCHAR PRIMARY KEY REFERENCES recordings(recording_id),
    work_id          VARCHAR,
    collection       VARCHAR,
    has_lyrics       BOOLEAN,
    has_timing       BOOLEAN,
    has_transcript   BOOLEAN,
    has_caption      BOOLEAN,
    n_lyrics         INTEGER,
    n_timings        INTEGER,
    min_link_confidence DOUBLE,
    unresolved_reason VARCHAR                  -- no_metadata | missing_lyrics | ambiguous ...
);

CREATE TABLE splits (
    work_id          VARCHAR PRIMARY KEY REFERENCES works(work_id),
    split            VARCHAR NOT NULL          -- train | val | test | generated
);
