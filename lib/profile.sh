# Reading a storage profile's metadata. Source, don't execute.
# shellcheck shell=bash

# A profile describes itself in profile.env, so `--profile-dir <dir>` is the
# only thing a caller needs to name. That matters once several profiles run in
# one lab: per-profile test directories and source trees cannot be passed as
# single global flags any more.
#
#   NAME    what this storage is called in results and image names
#   DISKS   how many lab test disks it wants (default 1)
#   TESTS   optional, relative to the profile dir - extra pytest files
#   SOURCE  optional, relative to the profile dir - a working tree the profile
#           builds and installs instead of using a published package
#
# DISKS is the reason this file exists. Profiles used to hardcode
# virtio-labdisk1, which works only while exactly one profile exists. The lab
# now assigns each profile a disjoint range and hands it over in $LAB_DISKS.

profile_get() {
    local dir="$1" key="$2" default="${3:-}"
    local value=""
    if [[ -f "$dir/profile.env" ]]; then
        value=$(sed -n "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*//p" \
            "$dir/profile.env" | tail -1 | tr -d '"'"'"'')
    fi
    printf '%s' "${value:-$default}"
}

profile_name()  { profile_get "$1" NAME "$(basename "$1")"; }
profile_disks() { profile_get "$1" DISKS 1; }

# Resolve an optional path declared relative to the profile directory.
profile_path() {
    local dir="$1" key="$2"
    local rel
    rel="$(profile_get "$dir" "$key")"
    [[ -n "$rel" ]] || return 0
    ( cd "$dir/$rel" 2>/dev/null && pwd ) || return 0
}
