#!/usr/bin/env python3
"""
Call Recording Manager
----------------------
Uploads call recordings to Google Drive (via rclone),
compresses them locally after confirmed upload,
and sends Gmail alerts for critical events and run summaries.
"""

import os
import sys
import subprocess
import logging
import smtplib
import json
import shutil
from pathlib import Path
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import yaml
from dotenv import load_dotenv

# ── Load environment ──────────────────────────────────────────────────────────
load_dotenv(Path(__file__).parent / ".env")

GMAIL_ADDRESS    = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
NOTIFY_EMAIL     = os.getenv("NOTIFY_EMAIL")

# ── Load config ───────────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent / "config.yaml"

with open(CONFIG_PATH, "r") as f:
    CONFIG = yaml.safe_load(f)

RECORDING_FOLDERS    = CONFIG.get("recording_folders", [])
AUDIO_FORMATS        = set(CONFIG.get("audio_formats", []))
GDRIVE_REMOTES       = CONFIG.get("gdrive_remotes", [])
GDRIVE_UPLOAD_FOLDER = CONFIG.get("gdrive_upload_folder", "CallRecordings")
GDRIVE_MAX_PCT       = CONFIG.get("gdrive_max_usage_percent", 90)
TEMP_FOLDER          = Path(CONFIG.get("temp_folder", "/tmp/call_manager_temp"))
LOG_FILE             = Path(CONFIG.get("log_file", "/tmp/call_manager.log"))
PROCESSED_DB         = Path(__file__).parent / "processed.json"


i=0
for remote in GDRIVE_REMOTES:
    GDRIVE_REMOTES[i] = "gdrive" + GDRIVE_REMOTES[i].split("@")[0]
    i += 1


# ── Logging ───────────────────────────────────────────────────────────────────
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── Processed files tracker ──────────────────────────────────────────────────
def load_processed() -> dict:
    """
    Load the processed files database from disk.
    Structure:
    {
      "filename.mp3": {
        "processed_at": "2024-01-15T10:30:00",
        "uploaded_to": "gdrive1",
        "compressed_as": "filename.opus"   # or null if compression failed
      },
      ...
    }
    """
    if PROCESSED_DB.exists():
        try:
            with open(PROCESSED_DB, "r") as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"Could not read processed.json, starting fresh: {e}")
    return {}


def save_processed(db: dict) -> None:
    """Persist the processed files database to disk."""
    try:
        with open(PROCESSED_DB, "w") as f:
            json.dump(db, f, indent=2)
    except Exception as e:
        log.error(f"Failed to save processed.json: {e}")


def mark_processed(db: dict, filename: str, remote: str, compressed_as: str | None) -> None:
    """Add a file to the processed database and save immediately."""
    db[filename] = {
        "processed_at":  datetime.now().isoformat(),
        "uploaded_to":   remote,
        "compressed_as": compressed_as,
    }
    save_processed(db)


def is_processed(db: dict, filename: str) -> bool:
    """Return True if this filename has already been fully processed."""
    return filename in db


# ── Compression format map ────────────────────────────────────────────────────
# Best lossless-equivalent compression targets per input format.
# We use Opus (libopus) at high bitrate for all lossy sources —
# it gives the best quality-to-size ratio available today.
# For truly lossless (WAV/FLAC) we use FLAC to preserve bit-perfect audio.
LOSSLESS_FORMATS = {".wav", ".flac", ".aiff"}

def best_output_format(input_ext: str) -> tuple[str, list[str]]:
    """
    Returns (output_extension, ffmpeg_codec_args) for the given input extension.
    Strategy:
      - Lossless input  → FLAC  (lossless, ~50-60 % smaller than WAV)
      - Lossy input     → Opus  (best lossy codec; ~60-70 % smaller than MP3/AAC at same quality)
    """
    ext = input_ext.lower()
    if ext in LOSSLESS_FORMATS:
        return ".flac", ["-c:a", "flac", "-compression_level", "12"]
    else:
        # 64k mono Opus ≈ transparent for speech; bump to 96k for music
        return ".opus", ["-c:a", "libopus", "-b:a", "64k", "-ac", "1", "-vbr", "on"]


# ── Email ─────────────────────────────────────────────────────────────────────
def send_email(subject: str, body: str) -> None:
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD or not NOTIFY_EMAIL:
        log.warning("Email credentials not configured — skipping email.")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = GMAIL_ADDRESS
        msg["To"]      = NOTIFY_EMAIL
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, NOTIFY_EMAIL, msg.as_string())
        log.info(f"Email sent: {subject}")
    except Exception as e:
        log.error(f"Failed to send email: {e}")


# ── rclone helpers ────────────────────────────────────────────────────────────
def rclone_about(remote: str) -> dict | None:
    """Return rclone about info for a remote as a dict, or None on failure."""
    try:
        result = subprocess.run(
            ["rclone", "about", f"{remote}:", "--json"],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
        log.error(f"rclone about {remote} failed: {result.stderr.strip()}")
    except Exception as e:
        log.error(f"rclone about {remote} exception: {e}")
    return None


def get_remote_usage_pct(remote: str) -> float | None:
    """Return used % for the remote, or None if unavailable."""
    info = rclone_about(remote)
    if info and info.get("total") and info.get("used") is not None:
        return (info["used"] / info["total"]) * 100
    return None


def rclone_upload(local_path: Path, remote: str, remote_folder: str) -> bool:
    """Upload a single file to remote:remote_folder/. Returns True on success."""
    dest = f"{remote}:{remote_folder}"
    try:
        result = subprocess.run(
            ["rclone", "copy", str(local_path), dest, "--progress"],
            capture_output=True, text=True, timeout=300
        )
        if result.returncode == 0:
            log.info(f"Uploaded: {local_path.name} → {dest}")
            return True
        log.error(f"Upload failed for {local_path.name}: {result.stderr.strip()}")
    except Exception as e:
        log.error(f"Upload exception for {local_path.name}: {e}")
    return False


def rclone_verify(local_path: Path, remote: str, remote_folder: str) -> bool:
    """Verify file exists on remote by checking its size matches local."""
    remote_path = f"{remote}:{remote_folder}/{local_path.name}"
    try:
        result = subprocess.run(
            ["rclone", "lsjson", remote_path],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            files = json.loads(result.stdout)
            if files:
                remote_size = files[0].get("Size", -1)
                local_size  = local_path.stat().st_size
                if remote_size == local_size:
                    log.info(f"Verified: {local_path.name} on {remote}")
                    return True
                log.warning(f"Size mismatch for {local_path.name}: local={local_size}, remote={remote_size}")
        else:
            log.warning(f"Verify failed for {local_path.name}: {result.stderr.strip()}")
    except Exception as e:
        log.error(f"Verify exception for {local_path.name}: {e}")
    return False


# ── Active remote selector ────────────────────────────────────────────────────
def get_active_remote(remotes: list[str], summary: dict) -> str | None:
    """
    Return the first remote with usage < GDRIVE_MAX_PCT.
    Sends alerts when switching or when all are full.
    """
    for remote in remotes:
        pct = get_remote_usage_pct(remote)
        if pct is None:
            log.warning(f"Could not get usage for {remote}, skipping.")
            continue
        log.info(f"{remote} usage: {pct:.1f}%")
        if pct < GDRIVE_MAX_PCT:
            return remote
        else:
            log.warning(f"{remote} is {pct:.1f}% full (limit {GDRIVE_MAX_PCT}%), skipping.")
            send_email(
                subject=f"⚠️ Drive Account Full: {remote}",
                body=(
                    f"Google Drive remote '{remote}' has reached {pct:.1f}% usage "
                    f"(limit: {GDRIVE_MAX_PCT}%).\n\n"
                    f"Switching to next available account automatically."
                )
            )
            summary["account_switches"].append(remote)

    # All remotes full
    send_email(
        subject="🚨 ALL Google Drive Accounts Full — Action Required",
        body=(
            "All configured Google Drive remotes have exceeded the "
            f"{GDRIVE_MAX_PCT}% usage limit.\n\n"
            "Configured remotes:\n" + "\n".join(f"  - {r}" for r in remotes) + "\n\n"
            "The script has halted. Please add more Google Drive accounts "
            "to config.yaml or free up space."
        )
    )
    return None


# ── Compression ───────────────────────────────────────────────────────────────
def compress_file(original: Path) -> Path | None:
    """
    Compress audio file using ffmpeg.
    Returns path to compressed file (may have new extension), or None on failure.
    The compressed file is written to TEMP_FOLDER first.
    """
    TEMP_FOLDER.mkdir(parents=True, exist_ok=True)
    out_ext, codec_args = best_output_format(original.suffix)
    out_name = original.stem + out_ext
    out_path = TEMP_FOLDER / out_name

    cmd = ["ffmpeg", "-y", "-i", str(original)] + codec_args + [str(out_path)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0 and out_path.exists():
            orig_size = original.stat().st_size
            comp_size = out_path.stat().st_size
            saving    = (1 - comp_size / orig_size) * 100
            log.info(
                f"Compressed: {original.name} → {out_name} "
                f"({orig_size/1024:.1f}KB → {comp_size/1024:.1f}KB, saved {saving:.1f}%)"
            )
            return out_path
        log.error(f"ffmpeg failed for {original.name}: {result.stderr[-500:]}")
    except Exception as e:
        log.error(f"Compression exception for {original.name}: {e}")
    return None


def replace_with_compressed(original: Path, compressed: Path) -> bool:
    """
    Delete original, move compressed to original's folder with new name.
    Returns True on success.
    """
    try:
        final_dest = original.parent / compressed.name
        original.unlink()
        shutil.move(str(compressed), str(final_dest))
        log.info(f"Replaced: {original.name} → {final_dest.name}")
        return True
    except Exception as e:
        log.error(f"Replace failed for {original.name}: {e}")
        return False


# ── Main workflow ─────────────────────────────────────────────────────────────
def collect_recordings(processed_db: dict) -> list[Path]:
    """Find all unprocessed audio files in configured folders."""
    files = []
    skipped = 0
    for folder in RECORDING_FOLDERS:
        p = Path(folder)
        if not p.exists():
            log.warning(f"Recording folder not found: {folder}")
            continue
        for f in p.iterdir():
            if f.is_file() and f.suffix.lower() in AUDIO_FORMATS:
                if is_processed(processed_db, f.name):
                    skipped += 1
                    log.debug(f"Skipping already-processed: {f.name}")
                else:
                    files.append(f)
    log.info(f"Found {len(files)} new recording(s) to process ({skipped} already processed, skipped).")
    return files


def run() -> None:
    start_time = datetime.now()
    log.info("=" * 60)
    log.info(f"Call Recording Manager started at {start_time.strftime('%Y-%m-%d %H:%M:%S')}")

    summary = {
        "started_at":       start_time.isoformat(),
        "files_found":      0,
        "skipped":          0,
        "uploaded":         [],
        "upload_failed":    [],
        "compressed":       [],
        "compress_failed":  [],
        "account_switches": [],
        "halted":           False,
        "halt_reason":      "",
    }

    processed_db = load_processed()
    all_recordings_count = sum(
        1 for folder in RECORDING_FOLDERS
        for f in (Path(folder).iterdir() if Path(folder).exists() else [])
        if f.is_file() and f.suffix.lower() in AUDIO_FORMATS
    )
    recordings = collect_recordings(processed_db)
    summary["files_found"] = len(recordings)
    summary["skipped"]     = all_recordings_count - len(recordings)

    if not recordings:
        log.info("No recordings to process. Exiting.")
        return  # Nothing happened — no summary email needed

    active_remote = get_active_remote(GDRIVE_REMOTES, summary)
    if active_remote is None:
        summary["halted"]      = True
        summary["halt_reason"] = "All Google Drive accounts are full."
        send_summary_email(summary)
        return

    for rec in recordings:
        log.info(f"Processing: {rec.name}")

        # Re-check remote capacity before each upload
        pct = get_remote_usage_pct(active_remote)
        if pct is not None and pct >= GDRIVE_MAX_PCT:
            log.warning(f"{active_remote} now at {pct:.1f}%, switching remote.")
            active_remote = get_active_remote(GDRIVE_REMOTES, summary)
            if active_remote is None:
                summary["halted"]      = True
                summary["halt_reason"] = "All Google Drive accounts became full during run."
                break

        # ── Step 1: Upload original ──
        uploaded = rclone_upload(rec, active_remote, GDRIVE_UPLOAD_FOLDER)
        if not uploaded:
            summary["upload_failed"].append(rec.name)
            send_email(
                subject=f"❌ Upload Failed: {rec.name}",
                body=f"Failed to upload '{rec.name}' to {active_remote}.\nCheck the log for details."
            )
            continue

        # ── Step 2: Verify upload ──
        verified = rclone_verify(rec, active_remote, GDRIVE_UPLOAD_FOLDER)
        if not verified:
            summary["upload_failed"].append(rec.name)
            send_email(
                subject=f"❌ Upload Verification Failed: {rec.name}",
                body=(
                    f"File '{rec.name}' was uploaded to {active_remote} but "
                    f"verification failed (size mismatch or file not found).\n"
                    f"The original file has NOT been compressed or deleted."
                )
            )
            continue

        summary["uploaded"].append({"file": rec.name, "remote": active_remote})

        # ── Step 3: Compress locally (only after confirmed upload) ──
        compressed = compress_file(rec)
        if compressed is None:
            summary["compress_failed"].append(rec.name)
            send_email(
                subject=f"⚠️ Compression Failed: {rec.name}",
                body=(
                    f"File '{rec.name}' was successfully uploaded to Google Drive "
                    f"but local compression failed.\n"
                    f"The original uncompressed file remains on your phone."
                )
            )
            # Still mark as processed (upload succeeded) so we don't re-upload next run
            mark_processed(processed_db, rec.name, active_remote, compressed_as=None)
            continue

        # ── Step 4: Replace original with compressed ──
        replaced = replace_with_compressed(rec, compressed)
        if replaced:
            summary["compressed"].append({
                "original": rec.name,
                "compressed": compressed.name,
            })
            mark_processed(processed_db, rec.name, active_remote, compressed_as=compressed.name)
        else:
            summary["compress_failed"].append(rec.name)
            mark_processed(processed_db, rec.name, active_remote, compressed_as=None)

    # Cleanup temp folder
    if TEMP_FOLDER.exists():
        shutil.rmtree(TEMP_FOLDER, ignore_errors=True)

    send_summary_email(summary)
    log.info("Run complete.")


def send_summary_email(summary: dict) -> None:
    """Build and send a run summary email."""
    start  = datetime.fromisoformat(summary["started_at"])
    end    = datetime.now()
    dur    = end - start

    lines = [
        "📋 CALL RECORDING MANAGER — RUN SUMMARY",
        "=" * 50,
        f"Started : {start.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Finished: {end.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Duration: {str(dur).split('.')[0]}",
        "",
        f"Files found       : {summary['files_found']} new",
        f"Skipped           : {summary['skipped']} (already processed)",
        f"Uploaded          : {len(summary['uploaded'])}",
        f"Upload failures   : {len(summary['upload_failed'])}",
        f"Compressed        : {len(summary['compressed'])}",
        f"Compress failures : {len(summary['compress_failed'])}",
        f"Account switches  : {len(summary['account_switches'])}",
        "",
    ]

    if summary["uploaded"]:
        lines.append("✅ Uploaded files:")
        for u in summary["uploaded"]:
            lines.append(f"   {u['file']} → {u['remote']}")
        lines.append("")

    if summary["compressed"]:
        lines.append("🗜️  Compressed files:")
        for c in summary["compressed"]:
            lines.append(f"   {c['original']} → {c['compressed']}")
        lines.append("")

    if summary["upload_failed"]:
        lines.append("❌ Upload failures:")
        for f in summary["upload_failed"]:
            lines.append(f"   {f}")
        lines.append("")

    if summary["compress_failed"]:
        lines.append("⚠️  Compression failures:")
        for f in summary["compress_failed"]:
            lines.append(f"   {f}")
        lines.append("")

    if summary["account_switches"]:
        lines.append("🔄 Account switches triggered by:")
        for r in summary["account_switches"]:
            lines.append(f"   {r} (exceeded {GDRIVE_MAX_PCT}%)")
        lines.append("")

    if summary["halted"]:
        lines.append(f"🚨 SCRIPT HALTED: {summary['halt_reason']}")
        lines.append("   Please add more Drive accounts or free up space.")

    body = "\n".join(lines)
    send_email(subject="📋 Call Recording Manager — Run Summary", body=body)


if __name__ == "__main__":
    run()