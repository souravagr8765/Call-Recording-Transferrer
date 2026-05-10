# 📞 Call Recording Manager

Automatically uploads call recordings to Google Drive, compresses them locally, and sends Gmail alerts — all running on your Android phone via Termux.

---

## How It Works

```
Every 4 hours (via Termux Job Scheduler):

  [Find recordings] → [Upload to Drive] → [Verify upload] → [Compress locally] → [Replace original]
                              ↓
                   [Switch account if >90% full]
                              ↓
                   [Email alerts for critical events]
                              ↓
                   [Email summary if anything happened]
```

---

## Project Structure

```
call-recording-manager/
├── manager.py       ← Main script
├── config.yaml      ← All settings (folders, formats, remotes, schedule)
├── .env             ← Gmail credentials (never commit this)
├── setup.sh         ← One-time installation script
└── README.md
```

---

## Installation

### Step 1 — Install Termux apps

From F-Droid (recommended, not Play Store):
- [Termux](https://f-droid.org/en/packages/com.termux/)
- [Termux:Job Scheduler](https://f-droid.org/en/packages/com.termux.jobscheduler/)

> ⚠️ Termux and Termux:Job Scheduler must be from the **same source** (both F-Droid or both Play Store) or job scheduling won't work.

### Step 2 — Copy files to Termux

```bash
# Inside Termux
mkdir -p ~/call_manager
cd ~/call_manager
# Copy all project files here
```

### Step 3 — Run setup

```bash
bash setup.sh
```

This will:
- Install `ffmpeg`, `python`, `rclone`
- Install Python packages (`pyyaml`, `python-dotenv`)
- Register the recurring job with Termux:Job Scheduler

---

## Configuration

### `.env` — Gmail Credentials

```env
GMAIL_ADDRESS=you@gmail.com
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
NOTIFY_EMAIL=you@gmail.com
```

**How to get a Gmail App Password:**
1. Go to [myaccount.google.com/security](https://myaccount.google.com/security)
2. Enable 2-Step Verification
3. Search for "App Passwords"
4. Create one for "Mail" → Copy the 16-character password

### `config.yaml` — Settings

```yaml
recording_folders:
  - /sdcard/Recordings/Call

audio_formats:
  - .mp3
  - .m4a
  - .wav

gdrive_remotes:
  - gdrive1
  - gdrive2

gdrive_upload_folder: CallRecordings
gdrive_max_usage_percent: 90
schedule_interval_hours: 4
```

### rclone — Add Google Drive Accounts

Run once per account:

```bash
rclone config
```

- Choose `n` for new remote
- Name it `gdrive1` (or whatever matches your config.yaml)
- Type: `drive`
- Follow OAuth prompts (opens browser)
- Repeat for `gdrive2`, etc.

---

## Compression Logic

| Input Format | Output Format | Why |
|---|---|---|
| `.wav`, `.flac`, `.aiff` | `.flac` | Lossless → lossless, ~50-60% smaller |
| `.mp3`, `.m4a`, `.aac`, `.3gp`, `.amr`, `.ogg`, `.opus` | `.opus` | Best lossy codec for speech, ~60-70% smaller |

- No audible quality loss for call recordings (speech content)
- The compressed file **replaces** the original with the same base name
- Compression only happens **after verified upload** to Google Drive

---

## Email Alerts

You'll receive an email when:

| Event | Email Sent |
|---|---|
| Upload fails | ❌ Immediately |
| Upload verification fails | ❌ Immediately |
| Compression fails | ⚠️ Immediately |
| Drive account hits 90% | 🔄 On switch |
| All Drive accounts full | 🚨 Immediately (script halts) |
| Run completes (with activity) | 📋 Summary after each run |

---

## Scheduling

The job runs every **2 hours** automatically via Termux:Job Scheduler.

```bash
# Check scheduled jobs
termux-job-scheduler -p

# Cancel all jobs
termux-job-scheduler --cancel-all

# Run manually anytime
python3 ~/call_manager/manager.py
```

---

## Troubleshooting

**Job not running automatically?**
- Make sure Termux:Job Scheduler is installed from the same source as Termux
- Disable battery optimization for Termux in Android settings
- Check logs: `cat ~/call_manager/call_manager.log`

**rclone auth issues?**
```bash
rclone config reconnect gdrive1:
```

**ffmpeg not found?**
```bash
pkg install ffmpeg
```
