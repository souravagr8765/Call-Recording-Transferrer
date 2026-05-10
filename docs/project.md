# Call Recording Manager — Project Documentation

> **Source of Truth** — All code changes must remain consistent with this document.

---

## Project Overview

**Purpose**: Automatically processes call recordings on an Android phone running Termux.

**Working of the Project**:
Every 4 hours (via Termux:Job Scheduler) the script:
1. Scans configured recording folders for new audio files.
2. Uploads each new file to Google Drive via `rclone`.
3. Verifies the upload (size check).
4. Compresses the local copy using `ffmpeg` — *only if compression would meaningfully reduce the file size*.
5. Replaces the original with the compressed version.
6. Records each processed file in a local **SQLite** database so it is never re-processed.
7. Sends Gmail alerts for critical events and a run summary.

---

## System Architecture

```
manager.py
│
├── Config loading          config.yaml  +  .env
├── Logging                 LOG_FILE (from config.yaml)
│
├── SQLite DB               processed.db          ← tracks processed files
│   └── migrate_json_to_sqlite()                  ← one-time migration from processed.json
│
├── Audio pipeline
│   ├── probe_audio_bitrate()   ← ffprobe: check source bitrate before compressing
│   ├── best_output_format()    ← decide codec / skip logic
│   ├── compress_file()         ← ffmpeg encoding + size-saving guard
│   └── replace_with_compressed()
│
├── rclone helpers
│   ├── rclone_about()
│   ├── get_remote_usage_pct()
│   ├── rclone_upload()
│   └── rclone_verify()
│
├── Active-remote selector  get_active_remote()
│
├── Email                   send_email()  /  send_summary_email()
│
└── Entrypoint              run()
```

---

## Database Schema

**File**: `processed.db` (SQLite, stored alongside `manager.py`)

### Table: `processed_files`

| Column          | Type | Constraints          | Description                                          |
|-----------------|------|----------------------|------------------------------------------------------|
| `filename`      | TEXT | PRIMARY KEY          | Original filename (e.g. `2024-01-15_call.m4a`)       |
| `processed_at`  | TEXT | NOT NULL             | ISO-8601 timestamp of when processing completed      |
| `uploaded_to`   | TEXT | NOT NULL             | rclone remote name used for upload (e.g. `gdrive1`)  |
| `compressed_as` | TEXT | NULL allowed         | New filename after compression, or NULL if skipped   |

**Migration**: If `processed.json` exists from a previous run, `migrate_json_to_sqlite()` imports
all records into SQLite on startup and renames the JSON file to `processed.json.bak`.

---

## Environment Configuration

**File**: `.env` (never commit — listed in `.gitignore`)

```env
GMAIL_ADDRESS=you@gmail.com          # Gmail account used to send alerts
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx  # Gmail App Password (16 chars)
NOTIFY_EMAIL=you@gmail.com           # Recipient address for alerts and summaries
```

---

## Configuration Management

**File**: `config.yaml`

```yaml
recording_folders:
  - /sdcard/Recordings/Call          # Paths scanned for new audio files

audio_formats:
  - .mp3
  - .m4a
  - .wav
  - .aac
  - .3gp
  - .amr
  - .ogg
  - .opus

gdrive_remotes:                      # rclone remote names (in priority order)
  - gdrive1
  - gdrive2

gdrive_upload_folder: CallRecordings # Destination folder inside Google Drive
gdrive_max_usage_percent: 90         # Switch to next remote above this threshold

temp_folder: /tmp/call_manager_temp  # Scratch space for ffmpeg output
log_file: /tmp/call_manager.log      # Log file path
schedule_interval_hours: 4           # Used by setup.sh for Termux:Job Scheduler
```

Config values are loaded at startup via `yaml.safe_load()` into module-level constants.

---

## Code Workflow

```
run()
 │
 ├── migrate_json_to_sqlite()          # one-time migration (no-op if already done)
 │
 ├── load_processed()                  # fetch set of already-processed filenames from SQLite
 │
 ├── collect_recordings(processed_set)
 │     └── for each folder → find audio files not in processed_set
 │
 ├── get_active_remote(remotes, summary)
 │     └── rclone_about() → pick first remote under GDRIVE_MAX_PCT
 │
 └── for each recording:
       │
       ├── get_remote_usage_pct()      # re-check capacity before every upload
       │
       ├── rclone_upload()             # upload original file
       │
       ├── rclone_verify()             # size-match check on remote
       │
       ├── compress_file()
       │     ├── best_output_format()
       │     │     ├── probe_audio_bitrate()   # ffprobe — get source kbps
       │     │     ├── Already Opus / FLAC → return None  (SKIP)
       │     │     ├── Lossless WAV/AIFF   → FLAC lossless
       │     │     └── Lossy (mp3/m4a/…)   → Opus 32 kbps mono (voip mode)
       │     │           only if source > 40 kbps AND expected saving > 10%
       │     │
       │     └── runs ffmpeg; discards output if saving < 10%
       │           returns Path  → compression succeeded
       │           returns "SKIP" → compression intentionally skipped
       │           returns None  → ffmpeg error
       │
       ├── replace_with_compressed()   # delete original, move compressed into place
       │
       └── mark_processed()            # INSERT OR REPLACE into processed_files
```

### Compression Skip Conditions

A file is **not** compressed (marked as processed without replacement) when:
- Extension is already `.opus` or `.flac`
- Source bitrate ≤ 40 kbps (already very small; Opus container overhead may increase size)
- Expected size saving is < 10% based on bitrate ratio
- ffmpeg output is not meaningfully smaller than original (< 10% saving after encoding)

This prevents the file-size-growth bug that occurred when re-encoding already-compressed files.
