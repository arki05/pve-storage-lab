"""Verifying that a volume operation actually did what it reported.

A storage task reporting success is not evidence that anything moved. We have
seen a container move-volume finish with an OK task while the rsync underneath
it failed and the config still pointed at the original storage - so every
relocation here is checked against three things rather than one:

  1. the guest config now names the destination
  2. a volume for that guest exists on the destination
  3. no volume for that guest is left behind on the source

Checking only (1) misses a backend that updates the config without copying the
data. Checking only (2) misses a move that copied but left the original behind,
which fills the source pool one migration at a time.
"""


def volumes_for(pve, storage: str, vmid: int) -> list[str]:
    return [entry["volid"] for entry in pve.storage_content(storage)
            if entry.get("vmid") == vmid or f"-{vmid}-" in entry["volid"]
            or f":{vmid}/" in entry["volid"]]


def assert_relocated(pve, guest, key: str, source: str, destination: str,
                     context: str = "") -> None:
    """Assert a volume really moved from `source` to `destination`.

    `key` is the config key holding it - "rootfs", "scsi0", and so on.
    """
    suffix = f" ({context})" if context else ""
    configured = guest.config().get(key, "")
    problems = []

    if not configured.startswith(f"{destination}:"):
        problems.append(
            f"config still says {key}={configured!r}, expected it on "
            f"'{destination}'"
        )

    if not volumes_for(pve, destination, guest.vmid):
        problems.append(f"no volume for {guest.vmid} exists on '{destination}'")

    left_behind = volumes_for(pve, source, guest.vmid)
    if left_behind:
        problems.append(f"volumes left behind on '{source}': {left_behind}")

    assert not problems, (
        f"the move reported success but did not take effect{suffix}:\n  - "
        + "\n  - ".join(problems)
    )
