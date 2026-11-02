# تصوّر داتاسِت موحّد: Arabic Music Discography Graph

> الهدف: تحويل البيانات المبعثرة بين GCS وGitHub إلى **داتاسِت واحد قابل للاستعلام والتدريب**،
> مع الحفاظ على أصل كل معلومة (provenance) وعدم الاعتماد على أسماء الملفات وحدها في الربط.

---

## 0) الخلاصة

المشكلة ليست "تجميع ملفات"، بل **توحيد الهوية**. عندنا:

- تسجيلات صوتية بأسماء مسارات وألبومات مختلفة.
- نصوص أغانٍ (lyrics) من مصادر متعددة.
- توقيتات كلمات (SRT/word timings).
- نسخ لحنية (ABC/SheetSage2).
- أوصاف صوتية (music captions).
- أدوات ومخرجات من مستودعات GitHub.

الحل المقترح:

1. **فصل المفهوم إلى ثلاث طبقات**: `work` (الأغنية كعمل) → `recording` (تسجيل/أداء محدد) → `artifact` (نص، توقيت، نسخة لحنية، وصف).
2. **معرّفات ثابتة**، أهمها `recording_id` مبني على بصمة الصوت (SHA-256)، و`work_id` مبني على هوية الأغنية مع جدول أسماء بديلة.
3. **جدول `links`** لكل علاقة مشتقة أو عبر المصادر، مع `method` و`confidence` و`evidence`.
4. **طبقة Bronze/Silver/Gold** فوق GCS، ومخرجات Parquet + JSONL، مع تقرير تغطية وطابور "غير محلول".

النتيجة الأولى المتوقعة (v0.1): حوالي **329 تسجيلًا فريدًا**، منها **236 مسار ألبوم**، مع **167 توقيتًا**، **236 نسخة ABC**، **230 وصفًا صوتيًا**، و**174 نصًا مرتبطًا بثقة عالية**.

### 0.1) قاعدة أساسية: الداتاسِت نصي فقط

الداتاسِت **لا يحتوي أي ملف صوت أو صورة**. يحتوي فقط على:

- بيانات وصفية ونصوص (lyrics, SRT, ABC, captions, manifests).
- **مسار نسبي** لكل ملف صوتي، من جذر الـ bucket:

```text
audio_path = "DISCOGRAPHY/ELISSA/Elissa - 1998 Baddy Doub/01. Baddy Doub.mp3"
```

بدل `gs://akbar-december-2024-backup/...`. حقل `audio_bucket` منفصل (اختياري) يحدد الـ bucket، وبذلك يمكن نقل الداتاسِت أو تغيير الـ bucket دون تعديل الصفوف.

- لا نضع signed URLs، ولا نضمّن الصوت، ولا ننسخ الأغلفة.
- نفس القاعدة تنطبق على GitHub: `repo + commit + relative_path` بدل نسخ المحتوى.
- كل حقول `*_path` في المخطط **نسبية** إلى جذر الـ bucket أو المستودع. النصوص الصغيرة (lyrics / ABC / captions) تُضمَّن داخل Parquet/JSONL، أما الملفات الكبيرة فتُشار إليها بالمسار فقط.
- الحجم المتوقع للداتاسِت: **عشرات الميغابايت** (Parquet/JSONL)، وليس غيغابايتات.

---

## 1) المبدأ: Work / Recording / Artifact

هذا الفصل هو أهم قرار في التصميم، لأنه يمنع الأخطاء التالية:

- اعتبار النسخة الحية (live) والاستوديو نفس "التسجيل".
- ربط كلمات الأغنية بتسجيل محدد بالخطأ.
- تكرار نفس الصوت في `p0` و`pd` كأنه أغنيتان.
- خلط الأغاني المولّدة (generated) مع الديسكوغرافي الحقيقي.

```
Artist 1 ─── * Album 1 ─── * Recording * ─── 1 Work
                                  │
              ┌───────────────────┼───────────────────┐
          Timing              Transcript           Caption
        (SRT/words)            (ABC)              (tags/prose)
                                  │
                               Lyrics * ─── 1 Work
```

- **Work**: الأغنية كعمل نصي/لحني (مثل "Baddy Doub" / "بدي دوب").
- **Recording**: ملف صوتي محدد (استوديو، حي، ريمكس، مولّد، غلاف).
- **Artifact**: أي شيء مشتق من التسجيل أو مرتبط بالعمل (SRT، ABC، caption، lyrics).

---

## 2) ما وجدناه فعليًا في المصادر

| المصدر | المحتوى | الحجم | المفتاح الحالي | الملاحظات |
|---|---|---:|---|---|
| `DISCOGRAPHY/` | صوت + أغلفة | 343 ملف صوتي (251 mp3 + 92 m4a) | المسار `artist/album/file` | 236 مسار ألبوم (Elissa 96 + Fadl 140)، و107 خارج الألبومات (`p0` 44، `pd` 50، `TODO` 10، `ajam_major` 3)؛ يُخزَّن **المسار النسبي فقط** |
| `DISCOGRAPHY/p0` + `pd` | مقطوعات Spotify بأسماء UUID | 94 ملفًا → 80 فريدًا | لا يوجد | 14 ملفًا مكررًا بالبايت بين `p0` و`pd`، ولا توجد ID3 tags |
| `SRT/v1/` | srt + vtt + words.json | 167 أغنية فريدة | اسم الملف + `corpus.jsonl` | 5 تشغيلات مختلفة، 167×3 ملفًا + manifest |
| `TRANSCRIPTS/` | ABC/SheetSage2 | 23 ألبومًا، 236 مسارًا | `source_uri` + `audio_sha256` | 224 ok، 3 flagged؛ 23 tar.gz خام |
| `yue2-karaoke-align` | فهرسة lyrics + SRT corpus | 236 مسارًا | `track_id = Artist/Album/file` | 174 match، 5 ambiguous، 5 no_match، 52 missing |
| `music_captioning_oct_2026` | captions + Suno prompts | 407 مسارًا | `artist/album/file` | 230 حقيقي + 177 مولّد (Arab Maqam Rock) |
| `arabic-music-lyrics` | نصوص أغانٍ | 12,463 أغنية، 2,771 فنانًا | `source/source_id` | معظم الفنانين لديهم أغنية واحدة فقط |
| `TODO/` + `ajam_major/` | مراجع يوتيوب | 13 ملفًا | YouTube ID داخل الاسم | يمكن ربطها بـ `TRANSCRIPTS/REF/maqam_analysis/evidence` |

**استنتاج مهم:** كل مراجع الصوت في الداتاسِت ستكون `relative path` فقط. أما الملفات النصية المشتقة (SRT/ABC/captions/lyrics) فيمكن تضمينها كنصوص أو كمسارات نسبية، حسب حجمها وحقوقها.

مفاتيح الربط موجودة بالفعل، لكنها متعددة الأشكال:

- `FADL_SHAKER/06.Sidi Rohi.2003/01.Ya Habibi Ta'ala.mp3`
- `06.Sidi_Rohi.2003_-_01.Ya_Habibi_Ta_ala.srt`
- `06.Sidi Rohi.2003 - 01.Ya Habibi Ta_ala` (داخل words.json)
- `FADL_SHAKER/06.Sidi Rohi.2003/01-ya-habibi-ta-ala` (track_id في ABC)

لذلك لا نحاول توحيدها في نص واحد، بل نحفظها كلها في جدول `legacy_ids`.

---

## 3) الهوية والمعرّفات

### 3.1 قواعد المعرّفات

| الكيان | المعرّف | طريقة التكوين | الاستقرار |
|---|---|---|---|
| Artist | `art_...` | slug للاسم القانوني + جدول aliases | يدوي/شبه يدوي |
| Album | `alb_...` | `hash(artist_id, album_norm, year)` | ثابت |
| Work | `w_...` | `hash(artist_id, title_norm)` + aliases | ثابت؛ التصحيحات عبر alias لا إعادة توليد |
| Recording | `rec_...` | `sha256(audio_bytes)[:16]` | content-addressed |
| Lyric | `lyr_...` | `hash(source, source_id)` | حسب المصدر |
| Timing | `tim_...` | `hash(recording_id, run_id, artifact_sha256)` | ثابت |
| Transcript | `trn_...` | `hash(recording_id, model, revision)` | ثابت |
| Caption | `cap_...` | `hash(recording_id, model, revision)` | ثابت |

**لماذا SHA-256 للصوت؟**

- يكشف التكرار الحقيقي حتى لو اختلف الاسم أو المجلد.
- يربط `TRANSCRIPTS` (فيه `audio_sha256`) بالديسكوغرافي مباشرة.
- يمنع عدّ `p0` و`pd` مرتين.

**ملاحظة:** نفس الأغنية بترميز مختلف (mp3 320 vs m4a) ستحصل على `recording_id` مختلف. لهذا نضيف:

- `recording_group_id` (اختياري) لتجميع النسخ المتقاربة.
- `audio_fingerprint` (Chromaprint) للنسخ المختلفة بايتًا ولكنها نفس الأداء.

### 3.2 الجداول الأساسية

- `artists`: الاسم اللاتيني، العربي، aliases، البلد.
- `albums`: العنوان، السنة، النوع (studio/live/compilation)، المصدر.
- `works`: العنوان القانوني (عربي/لاتيني)، `title_norm`، اللغة، اللهجة، النوع، المقام (اختياري).
- `recordings`: **`audio_bucket` + `audio_path` (نسبي)**، المدة، SHA-256/MD5، الألبوم، نوع النسخة، `is_generated`. لا يوجد عمود يحتوي على صوت أو bytes.
- `legacy_ids`: كل معرف قديم من أي مصدر (GCS full URI كـ legacy ID، yue2 track_id، mcap key، SRT basename، ABC track_id، YouTube ID).
- `lyrics`: النص الأصلي + `text_match` (بعد التطبيع)، المصدر، الترخيص، الجودة.
- `timings`: SRT/VTT/words.json (مسارات نسبية) + QA + النموذج + `is_preferred`.
- `transcripts`: نص ABC مضمّن + مسار نسبي + الحالة + النموذج + المدة.
- `captions`: الأدوات/الصوت/الإنتاج + `suno_prompt` + النموذج.
- `links`: علاقات مشتقة/عبر المصادر مع الثقة والدليل.
- `files`: بيان كل ملف (GCS/GitHub) مع `bucket/repo + relative_path` وSHA-256/ETag/commit؛ لا بايتات.
- `coverage`: صف لكل تسجيل يوضح ما هو متوفر وما هو ناقص.

DDL كامل في `schema/core.sql`.

---

## 4) قواعد الربط (بالترتيب)

نطبق سلسلة من القواعد، وكل قاعدة تسجل `method` و`confidence`، ولا تطمس القاعدة الأقوى.

| # | القاعدة | الثقة | مثال |
|---:|---|---:|---|
| 1 | تطابق `audio_sha256` | 1.00 | `TRANSCRIPTS.audio_sha256` ↔ `recordings.audio_sha256` |
| 2 | تطابق `source_uri` بعد التطبيع | 0.99 | `abc.source_uri` ↔ `audio_bucket + "/" + audio_path` |
| 3 | تطابق `legacy_track_id` | 0.98 | yue2 `track_id` / mcap `artist/album/file` |
| 4 | YouTube ID مستخرج من الاسم | 0.99 للربط بالمصدر، 0.80 للعمل | `_IKH1hS-JZT8` في `TODO` و`REF/evidence` |
| 5 | اسم ملف + ألبوم + مدة | 0.90–0.97 | SRT `06.Sidi_Rohi.2003_-_01...` |
| 6 | عنوان + فنان + مدة (fuzzy) | 0.60–0.90 | lyrics ↔ work |
| 7 | مراجعة يدوية | 1.00 | `manual_links.csv` |

### 4.1 التطبيع

- **Unicode**: NFKC.
- **العربية**: إزالة التشكيل في `text_match` فقط، توحيد `أإآ→ا`، `ى→ي`، إزالة التطويل.
- **اللاتينية**: lowercase، إزالة اللكنات، توحيد `'`/`’`، حذف الترقيم، دمج المسافات.
- **الألبومات**: حذف `^\d+[\.\-_ ]+`، السنة `(19|20)\d{2}`، `Full Discography.BY.RoMaNTiCPoET`.
- **الملفات**: حذف الامتداد، `NN.`/`NN -`، `[Remix]`، `(Official Video)`، لاحقة YouTube ID.
- **الفنانون**: جدول aliases يدوي: `Fadl Shaker / Fadel Chaker / فضل شاكر`، `Elissa / Elisa / إليسا`.

### 4.2 الحالات الخاصة

- **p0/pd**: لا tags، أسماء UUID. لا نحاول fuzzy matching على UUID. نستخدم:
  1. SHA-256/MD5 للكشف عن التكرار.
  2. `ffprobe`/ID3 إن وجد.
  3. AcoustID/Chromaprint أو خريطة `spotify_960k` إن توفرت.
  4. وإلا: `work_id = NULL` + `unresolved_reason = no_metadata`.
- **الميدلي**: `recording_works` many-to-many مع `position` و`is_medley`.
- **الآلاتي**: `is_instrumental = true`، وغياب lyrics ليس خطأ.
- **المولّد**: `is_generated = true` و`collection = arabmaqamrock`، ولا يخلط مع الحقيقي في splits.
- **نسخ متعددة من SRT**: كل تشغيل صف مستقل، مع `is_preferred` واحد لكل تسجيل.

---

## 5) خط الأنابيب

```
GCS + GitHub
    │
    ▼
[1] Scan / Manifest  ──► files.parquet (bucket/repo, relative_path, sha256, md5, generation, commit)
    │
    ▼
[2] Core Catalog     ──► artists / albums / works / recordings
    │
    ▼
[3] Exact Links      ──► SHA-256, source_uri, legacy_ids, YouTube IDs
    │
    ▼
[4] Text Links       ──► lyrics ↔ works (title_map + fuzzy + manual)
    │
    ▼
[5] Package          ──► Parquet + JSONL + coverage + unresolved
    │
    ▼
[6] Validate         ──► قيود الهوية، التكرار، التغطية، التسريبات
```

### المرحلة 1: Scan

- GCS: `list_objects` مع `generation`, `etag`, `md5_hash`, `crc32c`, `size`, `updated`.
- حساب SHA-256 مرة واحدة لكل ملف صوتي (streaming، بدون تنزيل دائم).
- GitHub: clone عند commit محدد، أو GitHub API، وحفظ `repo/commit/path/sha256`.
- الناتج: `manifest/files.parquet` + `snapshot_id`.

### المرحلة 2: Core

- تحليل مسارات `DISCOGRAPHY`:
  - `ELISSA/Elissa - 1998 Baddy Doub/01. Baddy Doub.mp3`
  - `FADL_SHAKER/Fadl Shaker.Full Discography.../06.Sidi Rohi.2003/01.Ya Habibi Ta'ala.mp3`
  - `p0/002-<uuid>.m4a`
- استخراج `ffprobe`: المدة، codec، bitrate، tags.
- بناء `artists/albums/works/recordings`.
- `legacy_ids` لكل مسار.

### المرحلة 3: Exact links

- `TRANSCRIPTS/*/abc/*.json`: `tracks[].source_uri` + `audio_sha256`.
- `SRT/v1/<run>/manifest.jsonl` + `results/corpus/corpus.jsonl`.
- `mcap/outputs/index.json` + per-album JSON.
- `yue2/index/lyrics/*.csv` كـ prior links، وليس مصدر حقيقة.
- `TODO`/`ajam_major`: استخراج YouTube ID ومطابقته مع `REF/maqam_analysis/evidence`.

### المرحلة 4: Text links

- تحميل `arabic-music-lyrics/data/processed/arabic_lyrics.parquet` (12,463 صفًا).
- فلترة أولية إلى فناني الكتالوج (Elissa/Fadl + المطربين في TODO/ajam).
- استخدام `yue2/index/lyrics/title_map.csv` كـ **gold seed** (174 match) وكتقييم للـ matcher.
- ربط `lyrics.work_id` فقط عند:
  - تطابق title_norm + artist alias، أو
  - تطابق نصي قوي مع دليل إضافي (سنة/ألبوم/مدة).
- الباقي في `unresolved.parquet` مع سبب واضح.

### المرحلة 5: Package

- Parquet للتحليل/التدريب.
- JSONL واحد لكل `recording` (نظام متداخل) للـ RAG/LLM.
- تصدير graph: `nodes.parquet` + `edges.parquet` (اختياري).

### المرحلة 6: Validate

- اختبارات آلية قبل النشر (انظر القسم 7).

---

## 6) شكل المخرجات

```
gs://akbar-december-2024-backup/DATASET/v0.1/
  _SUCCESS
  manifest.json                  # snapshot_id + counts + source commits
  DATASET_CARD.md
  schema/
    core.sql
    schema.json
  catalog/
    artists.parquet
    albums.parquet
    works.parquet
    recordings.parquet
  artifacts/
    lyrics.parquet
    timings.parquet
    transcripts.parquet
    captions.parquet
  links/
    links.parquet
    unresolved.parquet
  manifests/
    files.parquet                # metadata + hashes؛ لا يحتوي بايتات الصوت
    audio_index.parquet          # recording_id → bucket + relative_path + duration
    coverage.parquet
  jsonl/
    recordings.jsonl
  splits/
    train.parquet
    val.parquet
    test.parquet
```

### JSONL: صف واحد لكل تسجيل

```json
{
  "recording_id": "rec_...",
  "work": {"work_id": "w_...", "title_ar": "...", "title_lat": "..."},
  "artist": {"artist_id": "art_...", "name": "..."},
  "album": {"album_id": "alb_...", "title": "...", "year": 2008},
  "audio": {
    "bucket": "akbar-december-2024-backup",
    "path": "DISCOGRAPHY/ELISSA/Elissa - 1998 Baddy Doub/01. Baddy Doub.mp3",
    "sha256": "...",
    "duration_s": 279.7
  },
  "lyrics": [{"lyric_id": "lyr_...", "source": "fnanen", "text": "..."}],
  "timings": [{"timing_id": "tim_...", "srt_path": "SRT/v1/...", "n_words": 129}],
  "transcript": {"transcript_id": "trn_...", "abc_path": "TRANSCRIPTS/..."},
  "caption": {"caption_id": "cap_...", "suno_prompt": "..."},
  "provenance": [{"source": "gcs", "path": "DISCOGRAPHY/..."}]
}
```

> أي حقل مسار (`audio.path`, `srt_path`, `abc_path`, `provenance[].path`) هو **نسبي إلى جذر المصدر**، وليس رابطًا كاملًا.

### استعلام نموذجي (DuckDB)

```sql
SELECT r.recording_id, w.title_ar, l.source, t.n_words, c.suno_prompt
FROM recordings r
JOIN works w USING (work_id)
LEFT JOIN lyrics l
  ON l.work_id = w.work_id AND l.is_primary
LEFT JOIN timings t
  ON t.recording_id = r.recording_id AND t.is_preferred
LEFT JOIN captions c
  ON c.recording_id = r.recording_id
WHERE r.collection = 'discography_album';
```

---

## 7) الجودة والاختبارات

### 7.1 مقاييس التغطية

- `% recordings` التي لديها lyrics / timing / transcript / caption.
- `%` الروابط ذات ثقة ≥ 0.95.
- عدد المكرر (SHA-256) قبل/بعد.
- عدد `work_id = NULL` وأسبابها.
- فروق المدة بين المصادر (> 2s = flag).

### 7.2 اختبارات آلية

- `recording_id` فريد.
- كل `recording.work_id` موجود في `works` أو `NULL` مع سبب.
- كل `timings.recording_id` و`transcripts.recording_id` و`captions.recording_id` موجودة.
- التوقيتات monotonic داخل المدة.
- ABC يبدأ بـ `X:` ويحتوي `V: Vocal` / `V: Ins`.
- لا رابط gold بثقة < 0.95.
- لا تسريب بين splits: كل `work_id` في split واحد.
- `manifest` hash يطابق الملفات.

### 7.3 Splits

- التقسيم حسب `work_id`، وليس `recording_id`.
- 80/10/10 تقريبًا.
- المولّد (generated) في مجموعة منفصلة أو split مستقل.
- النسخ الحية/الريمكس لنفس العمل تبقى في نفس split.

---

## 8) الحقوق والخصوصية

- **الصوت**: لا يُنشر للعموم، ولا يُضمَّن في الداتاسِت إطلاقًا. الداتاسِت يحتوي **bucket + relative path + hash + metadata** فقط، والوصول إلى الصوت عبر bucket خاص.
- **Lyrics**: مصادرها (Genius/fnanen/kalimataghani) لها شروط استخدام؛ قد لا يُنشر النص كاملًا. البديل: روابط + إحصاءات + `text_match` hash.
- **ABC/Captions**: مشتقة من نماذج (SheetSage2 / ACE-Step / wav2vec2)؛ يجب ذكر النموذج والترخيص والإصدار.
- **Dataset Card**: المصادر، التراخيص، القيود، التغطية، طريقة الاستشهاد.

---

## 9) المخاطر والمعالجة

| الخطر | الأثر | المعالجة |
|---|---|---|
| `p0/pd` بلا metadata | 80 تسجيلًا غير مرتبط | SHA-256 + AcoustID/Chromaprint + طابور يدوي |
| تكرار بايتي | عدّ مزدوج | `recording_id` content-addressed |
| ترميزات مختلفة لنفس الأداء | تسجيلات متعددة | `recording_group_id` + fingerprint |
| اختلاف transliteration | ربط خاطئ | aliases + `title_map` + عتبة ثقة |
| تشغيلات SRT متعددة | تضارب | `is_preferred` + QA |
| نموذج/إصدار مختلف | اختلاف المخرجات | `model_revision` + artifact hash |
| lyrics ناقصة (57) | تغطية منخفضة | unresolved queue + مصادر إضافية |
| مولّد مقابل حقيقي | تسريب | `is_generated` + splits منفصلة |
| حقوق النشر | لا يمكن النشر | relative paths فقط + تحكم وصول |

---

## 10) MVP العملي (أول 1–2 يوم)

1. **Scan**: 343 ملفًا صوتيًا + مخرجات JSON/CSV → `files.parquet`. نحسب الـ hash ونسجّل `relative_path` فقط؛ لا ننسخ الصوت إلى الداتاسِت.
2. **Core**: 329 تسجيلًا فريدًا، 236 مسار ألبوم، 23 ألبومًا.
3. **Exact links**:
   - 236 ABC (224 ok / 3 flagged).
   - 167 SRT.
   - 230 caption (حقيقي).
   - 236 yue2 legacy IDs.
4. **Lyrics**: 174 match + 5 ambiguous من `title_map.csv`، والباقي unresolved.
5. **Report**: `COVERAGE.md` + `unresolved.parquet` + `manifest.json`.

هذا يعطي `v0.1` قابلة للاستخدام فورًا، ثم نوسّعها لاحقًا إلى:
- remaining 57 lyrics،
- p0/pd metadata،
- REF/maqam analysis،
- المفردات اللحنية/المقامات،
- splits وتدريب.

---

## 11) لماذا هذا التصميم وليس بديلًا أبسط؟

- **ليس جدولًا واحدًا**: لأن artifacts لها cardinality مختلفة (تسجيل واحد → عدة SRT/lyrics/captions).
- **ليس ربطًا بالاسم فقط**: لأن 14 ملفًا مكررًا و3 أنماط تسمية مختلفة تثبت هشاشة الاسم.
- **ليس graph خالصًا**: لأن ML يحتاج أعمدة typed، والـ graph export يبقى طبقة إضافية.
- **ليس نقلًا للبيانات**: لأن الصوت محمي؛ الأفضل catalog + **relative paths** + hashes.

---

## 12) الخطوة التالية

أقترح البدء بـ:

```bash
# 1) فحص + manifest
python -m src.scan_gcs --bucket akbar-december-2024-backup --out build/manifest

# 2) بناء الكتالوج
python -m src.build_core --manifest build/manifest --out build/core

# 3) الربط الدقيق
python -m src.link_exact --core build/core --out build/links

# 4) تقرير التغطية
python -m src.report --core build/core --links build/links --out build/report
```

ثم نشر `v0.1` إلى `gs://akbar-december-2024-backup/DATASET/v0.1/`.
