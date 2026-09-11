# pve-storage-lab

A disposable Proxmox VE node under QEMU, plus a backend-agnostic test suite for
Proxmox storage plugins.

Point it at a storage profile and it boots a real PVE node, registers the
storage, and runs the suite against it. Nothing in here knows about any
particular backend — a plugin supplies a profile and, optionally, its own
tests.

## Why not nested PVE on a real PVE host

Because it needs one. This runs anywhere with `/dev/kvm`, including inside an
unprivileged LXC container, with no Proxmox host, no cluster, and no
credentials. That matters most for CI: a self-hosted runner that can reach a
production Proxmox API is a much larger thing to trust than one that can only
open `/dev/kvm`.

The node uses user-mode networking with port forwards, so there is no bridge
and no tap device — and therefore no `NET_ADMIN`, no `/dev/net/tun`. One
character device is the entire elevated surface.

## Requirements

- `/dev/kvm`
- `qemu-system-x86_64`, `qemu-img`, `curl`, `ssh`
- `proxmox-auto-install-assistant` (installs cleanly on plain Debian from
  `http://download.proxmox.com/debian/pve`)

## Quick start

```bash
# Build the node image once (~11 min, unattended)
bash lab/build-image.sh

# Boot a node with four test disks
bash lab/up.sh --disks 4 --disk-size 8G

# Run the suite against a storage
bash run.sh --profile-dir ./profiles/lvm-thin

# Shell into the node
bash lab/ssh.sh

# Stop it; --reset also discards the overlay
bash lab/down.sh --reset
```

## How a run is shaped

`build-image.sh` runs Proxmox's own automated installer under QEMU and bakes in
what every run would otherwise redo: the package repositories, an LXC template,
and a VM template with the guest agent already installed. The result is a base
image that is **never written to again**.

`up.sh` boots a throwaway qcow2 overlay on top of it. Resetting a node is a
delete and a re-create, not a reinstall — about 20 seconds to an SSH-ready
node. Because each lab is just an overlay, several can run at once on separate
overlays, which a snapshot-and-rollback model cannot do.

Test disks are raw files attached with stable serials, so a profile addresses
them as `/dev/disk/by-id/virtio-labdiskN` rather than guessing at `vdb`. Ask
for as many as the backend needs — multi-device behaviour (replication,
targets, erasure coding) is not testable on one disk.

## Writing a profile

A profile is a directory:

```
profiles/<name>/
├── setup.sh          # runs on the node as root; registers the storage
├── capabilities.env  # what the storage supports; drives the skip logic
└── teardown.sh       # optional
```

`setup.sh` must append `STORAGE_NAME=` to `$LAB_TEST_CONFIG`. Everything else
is discovered. `capabilities.env` decides which tests apply:

| key | gates |
|---|---|
| `SUPPORTS_SNAPSHOTS` | snapshot, rollback, snapshot-mode backup |
| `SUPPORTS_LINKED_CLONE` | linked clones |
| `SUPPORTS_BACKUP` | vzdump and restore |
| `SUPPORTS_LXC` | every container test |
| `SUPPORTS_IMAGES` | every VM test |

A plugin repository keeps its own profile and passes it in, so this repository
never grows backend-specific knowledge:

```bash
bash run.sh --profile-dir ../pve-bcachefs/test/profile \
            --extra-tests ../pve-bcachefs/test/tests
```

## The suite

Tests run **on the node**, as root, through `pvesh`. No API credentials exist
anywhere — not in this repository, not in CI, not in the image.

Coverage is deliberately about data rather than return codes: `DataGuard`
seeds known files and re-checksums them after every operation, so a backend
that silently drops writes fails the test that would otherwise have passed on
a zero exit status. `test_composed.py` covers operations *in combination* —
rollback after resize, snapshot under `fio` load, back-to-back allocation —
which is where individually-correct operations turn out to be wrong in
sequence.

## Notes from building this

Three things cost real time and are worth knowing if you extend it:

- **Interface naming.** The default `NamePolicy` ends at `path`, which encodes
  PCI bus and slot. Attaching a test disk moved the NIC, `vmbr0` kept bridging
  a port that no longer existed, and the node booted to a login prompt showing
  an IP in its banner while being completely unreachable. The image pins the
  MAC and names the interface after it (`enx…`), which no PCI change can move.
- **`apt-daily.timer` fires on boot**, so a fresh node is often already holding
  the apt lock when sshd first answers. It is disabled in the image — a lab
  node running background package jobs during a test is its own flake source.
- **A serial console is not optional.** Without `console=ttyS0` a node that
  fails to boot is completely silent, and the only way to see anything is to
  screenshot the framebuffer through the QEMU monitor.

## License

MIT.
