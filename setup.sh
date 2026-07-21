#!/usr/bin/env bash
# setup.sh - provision the everyday Chrome environment on each macOS session.
# NOTE: To actually persist across sessions, PROFILE_DIR must be cloud-synced
# (pull at start / push at end). Within a single session it works standalone.

set -euo pipefail
echo "== Provisioning Chrome environment =="

CHROME_APP="/Applications/Google Chrome.app"

# 1) Install Chrome if it is not already present
if [ ! -d "$CHROME_APP" ]; then
  echo "Installing Google Chrome..."
  brew install --cask google-chrome
fi

# 2) Persistent profile directory.
#    Bookmarks / extensions / settings / history live here and survive across
#    sessions if this folder is cloud-synced. (Saved passwords & cookies do NOT
#    survive a copy, because Chrome encrypts them with the macOS Keychain -
#    use Chrome Sync for those.)
#    Point this at a cloud-synced folder (e.g. ~/Library/CloudStorage/...) to persist.
PROFILE_DIR="$HOME/chrome-profile"
mkdir -p "$PROFILE_DIR"

# 3) Force-install extensions with NO sign-in required (Chrome managed policy).
#    Add the extension IDs you want (found in the Chrome Web Store URL).
#    Example: uBlock Origin = cjpalhdlnbpafiamejdnhcphjbkeiagm
EXT_IDS=(
  # "cjpalhdlnbpafiamejdnhcphjbkeiagm"  # uBlock Origin
  # "eimadpbcbfnmbkopoojfekhnkhdbieeh"  # Dark Reader
)
if [ ${#EXT_IDS[@]} -gt 0 ]; then
  args=()
  for id in "${EXT_IDS[@]}"; do
    args+=("${id};https://clients2.google.com/service/update2/crx")
  done
  sudo defaults write com.google.Chrome ExtensionInstallForcelist -array "${args[@]}"
  echo "Force-install extensions configured: ${#EXT_IDS[@]}"
fi

# 4) Desktop launcher that always opens Chrome with the persistent profile
DESKTOP="$HOME/Desktop"
mkdir -p "$DESKTOP"
LAUNCHER="$DESKTOP/Chrome (persistent).command"
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
open -na "Google Chrome" --args --user-data-dir="$PROFILE_DIR"
EOF
chmod +x "$LAUNCHER"

echo "== Done =="
echo "Use the 'Chrome (persistent)' launcher on the Desktop."
echo "Sign in to Chrome once and turn ON Sync to restore saved passwords."
