# خطة تنفيذ Dataset Discography — v0.1

> هذه خطة فقط. **لن يتم تنفيذ أي مرحلة الآن.**
> المرجع التصميمي: `DESIGN.md` — المخطط: `schema/core.sql`.

---

## 0) نطاق v0.1

### داخل النطاق

- `DISCOGRAPHY/` ككتالوج صوتي وصفي (relative paths فقط).
- `SRT/v1/` كتوقيتات كلمات.
- `TRANSCRIPTS/` كنسخ ABC.
- `music_captioning_oct_2026` كأوصاف صوتية.
- `yue2-karaoke-align` كـ crosswalk سابق وبيانات فهرسة.
- `arabic-music-lyrics` كنصوص أغانٍ.

### خارج النطاق (مؤجل إلى v0.2)

- ملفات الصوت نفسها، والأغلفة، وأي صور.
- إعادة تشغيل SRT/ABC/captions.
- `arabmaqamrock` المولّد (يمكن إضافته لاحقًا كـ split منفصل).
- `REF/maqam_analysis` بالكامل (نبدأ فقط بربط YouTube IDs).
- تدريب أي نموذج.

### الناتج المتوقع

| العنصر | العدد |
|---|---:|
| تسجيلات فريدة | ~329 |
| مسارات ألبومات | 236 |
| SRT timings | 167 |
| ABC transcripts | 236 |
| Captions حقيقية | 230 |
| Lyrics مرتبطة بثقة عالية | 174 |
| غير محلول | ~57 lyrics + 80 p0/pd metadata |

---

## 1) المرحلة 0 — التهيئة

**الهدف:** تجهيز المستودع والاعتماديات والصلاحيات دون لمس البيانات.

### المهام

- [ ] إنشاء الهيكل:
  ```text
  src/
    scan_gcs.py
    scan_github.py
    build_core.py
    link_exact.py
    ingest_lyrics.py
    package_dataset.py
    validate.py
    report.py
  config/
    settings.yaml
    collections.yaml
    artist_aliases.csv
    manual_links.csv
  tests/
  build/
  ```
- [ ] تثبيت الاعتماديات:
  - `google-cloud-storage`
  - `pandas`, `pyarrow`, `duckdb`
  - `ffmpeg`/`ffprobe`
  - `rapidfuzz`, `pyyaml`, `pytest`
- [ ] إعداد Service Account بصلاحية **قراءة فقط** على bucket، وكتابة فقط على `DATASET/`.
- [ ] تعريف `settings.yaml`:
  - bucket name
  - prefixes
  - GitHub repos + commits
  - output prefix
- [ ] تثبيت عقد الإخراج: أسماء الجداول، أنواع الحقول، `relative_path` فقط.

### معيار القبول

- [ ] `pytest` يعمل.
- [ ] قراءة listing من GCS تعمل.
- [ ] لا يتم تنزيل أي ملف صوتي.

**الوقت التقديري:** نصف يوم.

---

## 2) المرحلة 1 — Manifest / Scanner

**الهدف:** بيان كامل لكل ملف، مع hashes، دون نسخ المحتوى.

### المهام

- [ ] سكان GCS:
  - `DISCOGRAPHY/`
  - `SRT/`
  - `TRANSCRIPTS/`
  - اختياريًا: `arabmaqamrock/` (لاحقًا)
- [ ] لكل object تسجيل:
  - `bucket`
  - `relative_path`
  - `generation`, `etag`
  - `md5_hash`, `crc32c`
  - `size`, `content_type`, `updated`
- [ ] حساب `sha256` للملفات الصوتية عبر streaming:
  - بدون حفظ الملف.
  - تسجيل الناتج في `audio_index.parquet`.
- [ ] سكان GitHub:
  - clone عند commit محدد أو GitHub API.
  - تسجيل `repo`, `commit_sha`, `relative_path`, `sha256`.
- [ ] تحليل المسار إلى:
  - `collection`
  - `artist_dir`
  - `album_dir`
  - `file_name`
- [ ] كشف التكرار:
  - `sha256` أولًا.
  - `md5` كتحقق إضافي.
- [ ] بناء `snapshot_id`.

### المخرجات

```text
build/manifest/files.parquet
build/manifest/audio_index.parquet
build/manifest/snapshot.json
```

### معيار القبول

- [ ] 343 ملفًا صوتيًا.
- [ ] 329 تسجيلًا فريدًا.
- [ ] كشف 14 ملفًا مكررًا بين `p0` و`pd`.
- [ ] 0 بايت صوت في المخرجات.

**الوقت التقديري:** 1 يوم.

---

## 3) المرحلة 2 — Core Catalog

**الهدف:** بناء `artists / albums / works / recordings / legacy_ids`.

### المهام

- [ ] تحليل أنماط `DISCOGRAPHY`:
  - `ELISSA/Elissa - YYYY Album/NN. Title.mp3`
  - `FADL_SHAKER/.../NN.Album.YYYY/NN.Title.mp3`
  - `p0/pd/NNN-uuid.m4a`
  - `TODO/TODO_NN/...`
  - `ajam_major/...`
- [ ] استخراج `ffprobe`:
  - duration
  - codec
  - bitrate
  - tags إن وجدت
- [ ] بناء `artists`:
  - الاسم اللاتيني/العربي
  - aliases
- [ ] بناء `albums`:
  - العنوان
  - السنة
  - النوع
- [ ] بناء `works`:
  - title_lat / title_ar
  - title_norm
  - seed من `yue2/index/lyrics/inventory.csv` و`title_map.csv`
- [ ] بناء `recordings`:
  - `audio_bucket` + `audio_path`
  - `audio_sha256`/`md5`
  - `duration_s`
  - `collection`
  - `is_generated`
  - `version_type`
- [ ] بناء `legacy_ids`:
  - GCS relative path
  - yue2 track_id
  - mcap key
  - SRT basename
  - ABC track_id
  - YouTube ID
- [ ] `p0/pd`:
  - `work_id = NULL`
  - `unresolved_reason = no_metadata`
  - لا fuzzy matching على UUID.

### المخرجات

```text
build/core/artists.parquet
build/core/albums.parquet
build/core/works.parquet
build/core/recordings.parquet
build/core/legacy_ids.parquet
```

### معيار القبول

- [ ] 236 مسار ألبوم.
- [ ] 107 مسارات خارج الألبومات (80 فريد p0/pd + 10 TODO + 3 ajam).
- [ ] كل `recording` لديه `audio_path` نسبي.
- [ ] لا يوجد عمود `audio_uri`.
- [ ] كل `recording_id` فريد.

**الوقت التقديري:** 1–1.5 يوم.

---

## 4) المرحلة 3 — Exact Links

**الهدف:** ربط ABC / SRT / Captions / yue2 / YouTube IDs.

### 4.1 ABC

- [ ] قراءة 23 ملف ABC.
- [ ] لكل track:
  - `source_uri`
  - `audio_sha256`
  - `status`
  - `abc`
- [ ] الربط بـ `recordings.audio_sha256` أو `relative_path`.
- [ ] إنشاء `transcripts`:
  - 236 صفًا
  - 224 ok / 3 flagged
  - الاحتفاظ بالحالة كما هي.

### 4.2 SRT

- [ ] قراءة `manifest.jsonl`.
- [ ] قراءة `results/corpus/corpus.jsonl` من yue2.
- [ ] ربط 167 أغنية.
- [ ] إنشاء `timings`:
  - `run_id`
  - `srt_path` / `vtt_path` / `words_path`
  - `qa`
  - `is_preferred`
- [ ] اختيار `is_preferred` واحد لكل تسجيل حسب QA والأحدث.

### 4.3 Captions

- [ ] قراءة `mcap/outputs/index.json`.
- [ ] قراءة 23 ملف ألبوم حقيقي.
- [ ] ربط 230 track.
- [ ] إنشاء `captions`:
  - instruments
  - vocals
  - production
  - suno_prompt
  - caption_seconds

### 4.4 yue2 كـ prior links

- [ ] قراءة `inventory.csv`, `title_map.csv`, `coverage.csv`.
- [ ] تسجيلها كـ `links` مع `method = existing_index`, `confidence = 0.90`.
- [ ] عدم اعتبارها مصدر حقيقة.

### 4.5 YouTube / REF

- [ ] استخراج YouTube ID من `TODO` و`ajam_major`.
- [ ] مطابقة `TRANSCRIPTS/REF/maqam_analysis/evidence`.
- [ ] إنشاء links مع `method = youtube_id`.

### المخرجات

```text
build/links/timings.parquet
build/links/transcripts.parquet
build/links/captions.parquet
build/links/links.parquet
build/links/unresolved.parquet
```

### معيار القبول

- [ ] 236 transcripts.
- [ ] 167 timings.
- [ ] 230 captions.
- [ ] 236 yue2 legacy IDs.
- [ ] كل link يحمل `method` + `confidence` + `evidence`.
- [ ] لا رابط gold بثقة < 0.95.

**الوقت التقديري:** 1.5 يوم.

---

## 5) المرحلة 4 — Lyrics Linking

**الهدف:** ربط النصوص بـ `works`، مع طابور مراجعة واضح.

### المهام

- [ ] قراءة `arabic-music-lyrics/data/processed/arabic_lyrics.parquet` (12,463).
- [ ] قراءة `lyrics/fnanen` و`lyrics/kalimataghani` لكل فنان.
- [ ] إزالة التكرار حسب `hash(text_match)`.
- [ ] فلترة أولية إلى فناني الكتالوج:
  - Elissa
  - Fadl Shaker
  - فناني TODO/ajam عند الحاجة
- [ ] استخدام `title_map.csv` كـ gold seed:
  - 174 match
  - 5 ambiguous
  - 5 no_match
  - 52 missing
- [ ] matcher:
  - exact normalized title + artist alias = 0.98
  - fuzzy 0.90–0.97 مع دليل سنة/ألبوم/مدة
  - أقل من 0.90 → unresolved
- [ ] ربط `lyrics.work_id`.
- [ ] عدم ربط lyrics بتسجيل محدد إلا إذا كانت النسخة خاصة بـ live/remix.
- [ ] إنشاء `unresolved.parquet` مع سبب واضح.

### المخرجات

```text
build/artifacts/lyrics.parquet
build/links/lyrics_links.parquet
build/links/unresolved.parquet
```

### معيار القبول

- [ ] 174 lyrics gold.
- [ ] 5 ambiguous: إما حُلّت يدويًا أو في unresolved.
- [ ] 0 lyrics gold بثقة < 0.95.
- [ ] كل unresolved له `reason`.

**الوقت التقديري:** 1–1.5 يوم.

---

## 6) المرحلة 5 — Packaging

**الهدف:** إخراج داتاسِت نصي قابل للتحميل.

### المهام

- [ ] بناء الجداول النهائية حسب `schema/core.sql`.
- [ ] `recordings.parquet` + `audio_index.parquet`.
- [ ] `recordings.jsonl` (صف متداخل لكل تسجيل).
- [ ] `coverage.parquet`:
  - has_lyrics
  - has_timing
  - has_transcript
  - has_caption
  - unresolved_reason
- [ ] `splits` حسب `work_id`:
  - train / val / test
  - generated منفصل
- [ ] `manifest.json`:
  - snapshot_id
  - counts
  - source commits
  - pipeline version
- [ ] `DATASET_CARD.md`:
  - المصادر
  - التغطية
  - القيود
  - الحقوق
- [ ] التحقق من الحجم:
  - < 100 MB
  - لا صوت
  - لا صور

### المخرجات

```text
build/dataset/v0.1/
  catalog/
  artifacts/
  links/
  manifests/
  jsonl/
  splits/
  manifest.json
  DATASET_CARD.md
```

### معيار القبول

- [ ] كل الجداول موجودة.
- [ ] كل الصفوف مطابقة للمخطط.
- [ ] `audio_path` نسبي في كل الصفوف.
- [ ] لا يوجد أي ملف صوت/صورة.
- [ ] الحجم < 100 MB.

**الوقت التقديري:** 0.5–1 يوم.

---

## 7) المرحلة 6 — Validation / QA

**الهدف:** منع نشر بيانات مكسورة أو مسرّبة.

### الاختبارات الآلية

- [ ] `recording_id` فريد.
- [ ] `work_id` موجود أو `NULL` مع سبب.
- [ ] كل `timing/transcript/caption` يشير إلى recording موجود.
- [ ] التوقيتات monotonic داخل المدة.
- [ ] ABC يبدأ بـ `X:` ويحتوي `V: Vocal`.
- [ ] `audio_path` لا يبدأ بـ `gs://`.
- [ ] لا رابط gold بثقة < 0.95.
- [ ] لا تسريب بين splits: كل `work_id` في split واحد.
- [ ] `manifest` hash يطابق الملفات.

### المراجعة اليدوية

- [ ] عينة 50 رابطًا عشوائيًا.
- [ ] قياس precision.
- [ ] الهدف: ≥ 95%.

### تقرير التغطية

- [ ] حسب artist.
- [ ] حسب album.
- [ ] حسب collection.
- [ ] قائمة unresolved.

### معيار القبول

- [ ] كل الاختبارات تمر.
- [ ] precision ≥ 95%.
- [ ] التغطية مطابقة للأرقام المتوقعة أو الفرق مبرر.

**الوقت التقديري:** 0.5–1 يوم.

---

## 8) المرحلة 7 — النشر

**الهدف:** نشر v0.1 بشكل ثابت وقابل لإعادة الإنتاج.

### المهام

- [ ] رفع إلى:
  ```text
  gs://akbar-december-2024-backup/DATASET/v0.1/
  ```
- [ ] ملف `_SUCCESS`.
- [ ] `manifest.json` مع snapshot_id.
- [ ] نسخة immutable: لا تعديل بعد النشر.
- [ ] اختياري: BigQuery external table.
- [ ] اختياري: HuggingFace dataset metadata فقط.
- [ ] توثيق طريقة الاستخدام:
  ```python
  # pseudo
  path = f"gs://{row.audio_bucket}/{row.audio_path}"
  ```

### معيار القبول

- [ ] يمكن تنزيل الداتاسِت كاملًا كنصوص.
- [ ] لا صوت.
- [ ] `snapshot_id` موثق.
- [ ] إعادة البناء تعطي نفس الأرقام.

**الوقت التقديري:** 0.5 يوم.

---

## 9) الجدول الزمني التقديري

| اليوم | المرحلة | الناتج |
|---:|---|---|
| 1 | التهيئة + Scanner | `files.parquet`, `audio_index.parquet` |
| 2 | Core Catalog | `artists/albums/works/recordings` |
| 3 | Exact Links | `timings/transcripts/captions/links` |
| 4 | Lyrics | `lyrics` + `unresolved` |
| 5 | Packaging + QA | `dataset/v0.1` + tests |
| 6 | Publish | GCS + dataset card |

**الإجمالي:** 5–6 أيام عمل، بافتراض وضوح قرارات الحقوق والوصول.

---

## 10) المخاطر وخطط التعامل

| الخطر | الاحتمال | الأثر | التعامل |
|---|---|---|---|
| `p0/pd` بلا metadata | مرتفع | 80 تسجيلًا غير مرتبط | SHA-256 + AcoustID/Chromaprint + طابور يدوي |
| حقوق lyrics | متوسط | لا يمكن نشر النص | نشر روابط/إحصاءات فقط، أو bucket خاص |
| اختلاف transliteration | مرتفع | ربط خاطئ | aliases + `title_map` + عتبة 0.95 |
| SRT متعدد التشغيلات | مرتفع | تضارب | `is_preferred` + QA |
| فقدان ملفات مصدر | منخفض | نقص تغطية | snapshot + manifest + hashes |
| تغيّر نماذج لاحقًا | متوسط | اختلاف المخرجات | `model_revision` + artifact hash |
| تسريب splits | متوسط | نتائج مضللة | split حسب `work_id` |
| حجم الداتاسِت | منخفض | صعوبة تحميل | نصوص فقط + relative paths |

---

## 11) قرارات مطلوبة قبل البدء

1. **الحقوق:** هل سيتم نشر نصوص lyrics كاملة، أم روابط + إحصاءات فقط؟
2. **`p0/pd`:** هل توجد خريطة metadata من `spotify_960k` UUID → track؟ أم نعتمد AcoustID/مراجعة يدوية؟
3. **`arabmaqamrock`:** هل يدخل v0.1 كـ split منفصل أم v0.2؟
4. **`REF/maqam_analysis`:** هل نضمّنه في v0.1 أم نكتفي بربط YouTube IDs؟
5. **النشر:** GCS فقط، أم GCS + HuggingFace metadata؟
6. **المراجعة اليدوية:** من يراجع 5 ambiguous + عينة 50 رابطًا؟

---

## 12) تعريف الإنجاز (Definition of Done)

- [ ] 329 تسجيلًا فريدًا.
- [ ] 236 مسار ألبوم.
- [ ] 167 SRT.
- [ ] 236 ABC.
- [ ] 230 caption.
- [ ] 174 lyrics gold.
- [ ] `audio_path` نسبي 100%.
- [ ] 0 ملف صوت/صورة داخل الداتاسِت.
- [ ] كل الاختبارات تمر.
- [ ] precision ≥ 95% على العينة.
- [ ] `manifest.json` + `DATASET_CARD.md` منشوران.
- [ ] إمكانية إعادة البناء من snapshot.

---

## 13) أوامر مستقبلية (لا تُنفَّذ الآن)

```bash
# 0) تهيئة
make bootstrap

# 1) manifest
python -m src.scan_gcs --config config/settings.yaml
python -m src.scan_github --config config/settings.yaml

# 2) core
python -m src.build_core --manifest build/manifest --out build/core

# 3) exact links
python -m src.link_exact --core build/core --out build/links

# 4) lyrics
python -m src.ingest_lyrics --core build/core --out build/artifacts

# 5) package
python -m src.package_dataset --build build --out build/dataset/v0.1

# 6) validate
python -m src.validate --dataset build/dataset/v0.1

# 7) publish (بعد الموافقة فقط)
python -m src.publish --dataset build/dataset/v0.1 --dest gs://akbar-december-2024-backup/DATASET/v0.1
```
