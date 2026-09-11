#!/usr/bin/env bash
# Apply one storage profile on this node.
#
#   apply-profile.sh <name> <config path> <disks> <source dir>
#
# The profile's setup.sh formats its assigned disks and registers the storage.
# It receives its disks in $LAB_DISKS and must not go looking for any others:
# with several profiles in one lab, globbing takes a disk belonging to another.
set -euo pipefail
NAME="$1"; CONFIG="$2"; DISKS="$3"; SOURCE="$4"

export LAB_TEST_CONFIG="$CONFIG"
export LAB_DISKS="$DISKS"
export LAB_SOURCE="$SOURCE"

: > "$CONFIG"
rm -f "/root/.lab-profile-applied-$NAME"
chmod +x "/root/lab-profile-$NAME"/*.sh 2>/dev/null || true

# </dev/null so the profile cannot consume anything it should not.
bash "/root/lab-profile-$NAME/setup.sh" </dev/null

if [ -f "/root/lab-profile-$NAME/capabilities.env" ]; then
    cat "/root/lab-profile-$NAME/capabilities.env" >> "$CONFIG"
fi

grep -q '^STORAGE_NAME=' "$CONFIG" || {
    echo "profile $NAME did not write STORAGE_NAME to $CONFIG" >&2
    exit 1
}

echo "--- $NAME ---"
grep -v '^#' "$CONFIG" | grep . || true
touch "/root/.lab-profile-applied-$NAME"
