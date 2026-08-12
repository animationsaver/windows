#!/usr/bin/env bash
# Install envsnap onto a runner. Idempotent.
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
sudo=""
[ "$(id -u)" -eq 0 ] || sudo=sudo

need=()
for pkg in zstd jq openssl; do
	command -v "$pkg" >/dev/null 2>&1 || need+=("$pkg")
done
# attr/acl bring the tar helpers that keep xattrs and ACLs intact
dpkg -s acl >/dev/null 2>&1 || need+=(acl)
dpkg -s attr >/dev/null 2>&1 || need+=(attr)
if [ "${#need[@]}" -gt 0 ]; then
	echo "installing: ${need[*]}"
	# env, not a bare assignment prefix: with $sudo empty the prefix would
	# become the command name and bash would look for a program called
	# DEBIAN_FRONTEND=noninteractive.
	$sudo env DEBIAN_FRONTEND=noninteractive apt-get update -qq
	$sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${need[@]}"
fi

$sudo install -Dm755 "$here/envsnap"       /usr/local/sbin/envsnap
$sudo install -Dm755 "$here/envsnap-enter" /usr/local/sbin/envsnap-enter
$sudo install -Dm755 "$here/envsnap-shell" /usr/local/bin/envsnap-shell
$sudo install -Dm644 "$here/exclude.txt"   /usr/local/share/envsnap/exclude.txt
$sudo install -Dm644 "$here/profile.d-envsnap.sh" /etc/profile.d/envsnap.sh
$sudo mkdir -p /run/envsnap

echo "envsnap installed:"
echo "  /usr/local/sbin/envsnap"
echo "  /usr/local/sbin/envsnap-enter"
echo "  /usr/local/bin/envsnap-shell"
echo "  /usr/local/share/envsnap/exclude.txt"
echo "  /etc/profile.d/envsnap.sh"
