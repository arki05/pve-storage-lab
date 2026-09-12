# pve-storage-lab

Boots throwaway Proxmox VE nodes under QEMU and runs a backend-agnostic test
suite against a storage. Nothing here knows about a particular backend: a
plugin supplies a profile, and optionally its own tests.

Needs `/dev/kvm` and nothing else — no Proxmox host, no cluster, no API
credentials. It runs in an unprivileged LXC container, which is what makes it
usable from a self-hosted CI runner.

## Requirements

- `/dev/kvm`
- `qemu-system-x86_64`, `qemu-img`, `curl`, `ssh`, `make`, `python3` (3.11+)
- `proxmox-auto-install-assistant` (installs on plain Debian from
  `http://download.proxmox.com/debian/pve`)

## Quick start

```bash
make image                                        # base image, ~10 min, once

./bin/lab run --profile-dir ./profiles/lvm-thin   # up, test, report, tear down

./bin/lab up --nodes 1 --disks 4 --disk-size 8G   # or drive it directly
./bin/lab ssh
./bin/lab status
./bin/lab down --reset                            # --reset discards the disks
```

`bin/lab` takes `up`, `down`, `ssh`, `status`, `node-image` and `run`. The
Makefile wraps the common ones plus `make matrix` and `make check`.

## How it fits together

| | |
|---|---|
| `bin/lab` | the CLI |
| `labkit/` | orchestration: images, qemu, cluster, profiles, phases |
| `labkit/remote/` | shell that runs *on* a node, pushed as files |
| `lab/build-image.sh` | the base image build (one unattended install) |
| `suite/` | the tests, run on the node as root through `pvesh` |
| `profiles/` | one directory per built-in backend |
| `tools/report.py` | junit → verdict + `summary.md` |

**Images** are layered, each cached at a different lifetime:

- **base** — a PVE install with an LXC template and a VM template baked in.
  Never written to again.
- **variant** — base plus a profile's `bake.sh`, for expensive one-time setup
  such as a DKMS module.
- **node** — a variant plus one node's identity, a ~15 MB qcow2 overlay.

A lab boots throwaway overlays on top, so resetting a node is a delete and a
re-create, about 20 seconds to an SSH-ready node, and several labs can run at
once.

**Test disks** are raw files attached with stable serials, addressed as
`/dev/disk/by-id/virtio-labdiskN`. Ask for as many as the backend needs —
replication, tiering and erasure coding are not testable on one disk.

## Nodes and networking

Everything about a node derives from its index and the lab's name
(`labkit/nodes.py`), so nothing is allocated or recorded:

| node | hostname | management | guest bridge | cluster |
|---|---|---|---|---|
| 1 | `pve-node1` | `10.0.2.10` | vmbr0, no address | `10.9.9.1` |
| 2 | `pve-node2` | `10.0.3.10` | vmbr0, no address | `10.9.9.2` |

Three NICs:

- **management** — its own network per node, reached through a host port
  forward. Nothing is shared between nodes.
- **guest bridge** — carries no host address, so a guest cannot take the
  node's address or DHCP lease.
- **cluster** — a QEMU socket netdev, an L2 link between QEMU processes in
  userspace. No tap, no bridge, so `/dev/kvm` stays the only elevated device.

Host ports derive from the lab name and node index, so two labs can run side
by side.

`--nodes 2` forms a real cluster. Migration needs it; single-node runs are
unaffected.

## Writing a profile

```
profiles/<name>/
├── profile.env       # NAME, DISKS, optional TESTS / SOURCE
├── setup.sh          # runs on every node as root; registers the storage
├── capabilities.env  # optional: values tests read
├── bake.sh           # optional: expensive setup, baked into an image once
├── expectations.toml # optional: what this backend cannot do
└── teardown.sh       # optional
```

`profile.env`:

```
NAME=bcachefs
DISKS=4              # test disks wanted; the lab assigns a disjoint range
TESTS=../tests       # extra pytest files, relative to the profile dir
SOURCE=../..         # a working tree to build and install, not a release
```

`setup.sh` gets its disks in `$LAB_DISKS` and must append `STORAGE_NAME=` to
`$LAB_TEST_CONFIG`. Do not glob for disks: with more than one profile in a lab
that steals another storage's disk.

`SOURCE` ships a working tree to **every** node, so a profile can build and
install the thing under test instead of pulling its last release. Every node,
because a storage plugin is a Perl module on each of them.

A plugin keeps its profile in its own repository:

```bash
./bin/lab run --profile-dir ../pve-bcachefs/test/profile --nodes 2
```

## Results

Every test runs against every backend. Nothing is skipped for being
unsupported — what a backend cannot do is declared in `expectations.toml`,
with a reason:

```toml
[[expected]]
profile = "bcachefs"
marker = "snapshot_migration"
kind = "limitation"
reason = "raw+size and tar+size have nowhere to put a snapshot"
```

Match on a capability `marker` (durable) or a `test` glob (for one-offs).
`kind` is `limitation` for something a backend cannot do, `known-bug` for
something it should do and does not.

`tools/report.py` writes `summary.md` and sorts results into three buckets:

| | |
|---|---|
| **failures** | not declared. These block |
| **expected failures** | declared and failed. Reported, do not block |
| **stale expectations** | declared and **passed**. These block too |

The third one is why this is not a skip list: a declaration that starts
passing means the backend gained something, or the declaration was wrong. A
skip would have hidden both.

## Several storages, and cross-storage

```bash
./bin/lab run --profile-dir ./profiles/zfs \
              --profile-dir ./profiles/btrfs --cross
```

The suite runs once per storage. `--cross` adds pairwise round trips: move a
volume from each storage to each other and back, and check the data.

Pairs are ordered — `zfs > btrfs > zfs` and `btrfs > zfs > btrfs` are both
run, since a backend that reads a foreign volume correctly may still write its
own wrongly. N storages give N×(N−1) round trips, ×5 for container payload
shapes.

The assertion is not "the move succeeds" — some pairs legitimately cannot
round-trip, e.g. a compressed volume holding more logical data than fits
uncompressed. It is that a move either succeeds completely or fails cleanly,
leaving the source intact and nothing stranded.

## Phases

```bash
./bin/lab run --profile-dir ./profiles/zfs --phase prepare
./bin/lab run --phase suite --only zfs
./bin/lab run --phase report
./bin/lab run --phase teardown
```

`prepare` writes a plan (profiles, disks, storage names) into the lab
directory; later phases read it, so they take no arguments of their own. Only
a whole-run invocation clears previous results, so per-suite phases accumulate
and `report` sees all of them.

## The suite

Runs on the node, as root, through `pvesh`. No credentials anywhere.

Lifecycle, snapshots, clones, backup/restore, resize, move, migration (offline
and live), vmstate, durability across hard stops, size enforcement, data
integrity, and combinations — rollback after resize, snapshot under load,
back-to-back allocation.

Checks are about data, not exit codes: `DataGuard` seeds known files and
re-checksums them, `MetadataFixture` covers ownership, modes, hardlinks,
symlinks, xattrs, sparseness and timestamps, and snapshots and migrations run
with `fio --verify` in flight.

Some axes exist because a bug hid in them:

- **aio modes** — `io_uring`, `threads` and `native` take different paths into
  the kernel; everything else runs whatever PVE defaults to.
- **the CLI, not just the API** — `pct` and `qm` run Perl with `-T`, where a
  path from a config file is tainted and `syscall()` refuses it; `pvedaemon`
  does not. A suite driving only `pvesh` passes against a storage where `pct
  create` cannot create a container.
- **stopped volumes** — comparing a guest's filesystem before and after an
  operation only means anything with the guest stopped; a running Debian
  rewrites a dozen paths per boot.
- **payload shape** — random, compressible and sparse, because a backend that
  handles one can mishandle another.

## PVE details worth knowing

- `/etc/pve` is pmxcfs: a VMID is globally unique and guest configs **move**
  between node directories. `cp` fails, `cp -a` fails outright.
- `pvecm add` refuses a node that has guests, so only node 1 keeps the baked
  VM template.
- Node images share a base, so each must regenerate SSH host keys and the
  cluster must run `pvecm updatecerts`. Otherwise proxied API calls fail, and
  it looks like migrations timing out rather than an SSH error.
- A UPID encodes the node it ran on; asking another node for it is a 500.
- `pvesh` exits 0 even when the task it watched failed, and prints the task log
  around the UPID. When it proxies to another node it returns the whole log as
  one JSON string.
- Without `console=ttyS0` a node that fails to boot is silent.

## License

AGPL-3.0-or-later, same as Proxmox VE. See `LICENSE`.

A profile is configuration and shell this consumes, so writing one keeps your
plugin's licence to itself. Test files that import from `suite/` are
derivative of this.
