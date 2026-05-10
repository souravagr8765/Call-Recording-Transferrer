#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
# setup.sh — One-time setup script for Call Recording Manager
# Run this once inside Termux to install dependencies and
# register the job with Termux:Job Scheduler.
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANAGER_SCRIPT="$SCRIPT_DIR/manager.py"
INTERVAL_HOURS=$(python3 -c "import yaml; c=yaml.safe_load(open('$SCRIPT_DIR/config.yaml')); print(c.get('schedule_interval_hours', 4))")
INTERVAL_MS=$(( INTERVAL_HOURS * 60 * 60 * 1000 ))

echo "=================================================="
echo " Call Recording Manager — Setup"
echo "=================================================="
echo ""

# ── 1. Update packages ────────────────────────────────
echo "[1/6] Updating Termux packages..."
pkg update -y && pkg upgrade -y

# ── 2. Install system dependencies ───────────────────
echo "[2/6] Installing ffmpeg, python, rclone..."
pkg install -y ffmpeg python rclone

# ── 3. Install Python dependencies ───────────────────
echo "[3/6] Installing Python packages..."
pip install pyyaml python-dotenv

# ── 4. Make manager.py executable ────────────────────
echo "[4/6] Setting permissions..."
chmod +x "$MANAGER_SCRIPT"

# ── 5. Register with Termux:Job Scheduler ────────────
echo "[5/6] Registering job with Termux:Job Scheduler (every ${INTERVAL_HOURS}h)..."

termux-job-scheduler \
  --script "$MANAGER_SCRIPT" \
  --period-ms "$INTERVAL_MS" \
  --battery-not-low false \
  --network any \
  --persisted true

echo ""
echo "[6/6] Setup complete!"
echo ""
echo "=================================================="
echo " NEXT STEPS"
echo "=================================================="
echo ""
echo "1. Edit .env with your Gmail address and App Password:"
echo "   nano $SCRIPT_DIR/.env"
echo ""
echo "2. Edit config.yaml to set your recording folders"
echo "   and rclone remote names:"
echo "   nano $SCRIPT_DIR/config.yaml"
echo ""
echo "3. Configure rclone remotes (one per Drive account):"
echo "   rclone config"
echo "   (Add a new remote, type: drive, name it gdrive1, gdrive2, etc.)"
echo ""
echo "4. Run manually to test:"
echo "   python3 $MANAGER_SCRIPT"
echo ""
echo "5. The job is now scheduled to run every ${INTERVAL_HOURS} hours automatically."
echo "   To check scheduled jobs: termux-job-scheduler -p"
echo "   To cancel the job:       termux-job-scheduler --cancel-all"
echo "=================================================="
