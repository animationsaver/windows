#!/usr/bin/env bash
# envsnap と依存パッケージをランナーに入れる。ワークフローから呼ぶ。
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

sudo install -m 0755 "$here/envsnap"       /usr/local/sbin/envsnap
sudo install -m 0755 "$here/envsnap-enter" /usr/local/sbin/envsnap-enter
sudo install -m 0755 "$here/envsnap-shell" /usr/local/bin/envsnap-shell
sudo install -d -m 0755 /usr/local/share/envsnap
sudo install -m 0644 "$here/exclude.txt"   /usr/local/share/envsnap/exclude.txt

missing=()
for c in zstd jq openssl; do command -v "$c" > /dev/null || missing+=("$c"); done
if [ "${#missing[@]}" -gt 0 ]; then
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
fi
if ! command -v rclone > /dev/null; then
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq rclone \
    || curl -fsSL https://rclone.org/install.sh | sudo bash
fi

printf 'envsnap installed: %s / rclone %s\n' \
  "$(command -v envsnap)" "$(rclone version | head -1 | awk '{print $2}')"
