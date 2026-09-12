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
# Build the base image once (~10 min, unattended)
make image

# Run the suite against a storage: brings a lab up, tests it, reports
./bin/lab run --profile-dir ./profiles/lvm-thin

# Or drive the lab directly
./bin/lab up --nodes 1 --disks 4 --disk-size 8G
./bin/lab ssh
./bin/lab status
./bin/lab down --reset          # --reset also discards the disks
```

Orchestration is Python (`labkit/`, driven by `bin/lab`). What runs *on* a
node stays shell, pushed as a file and run with arguments - there is no
heredoc anywhere with two levels of expansion in it. The base image build is
still a shell script, because it is one long unattended install that never
needed the structure.

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

## Two nodes

```bash
./bin/lab run --profile-dir ./profiles/zfs --nodes 2
```

Migration is the only reason this exists — PVE cannot move a guest between
unclustered nodes — and it is opt-in, so single-node runs are unchanged.

Each node boots from its **own node image**: the base image plus that node's
identity, built by `labkit/images.py` as a qcow2 overlay costing ~15 MB. A
node comes up correct rather than being corrected afterwards, and the PVE
rename happens once, on a standalone node, instead of during cluster
formation.

Everything about a node is derived from its index and its lab's name
(`labkit/nodes.py`):

| node | hostname | management | guest bridge | cluster |
|---|---|---|---|---|
| 1 | `pve-node1` | `10.0.2.10` | vmbr0, no address | `10.9.9.1` |
| 2 | `pve-node2` | `10.0.3.10` | vmbr0, no address | `10.9.9.2` |

Three NICs, which is what a real node has, and the separation is load-bearing:

- **management** is on its own network per node, reached only through the
  host's port forward. Nothing is shared between nodes.
- **the guest bridge carries no host address.** The node does not live on the
  segment its guests do, so a guest can never take the node's address or its
  DHCP lease — a bug this lab had when the two shared `vmbr0`.
- **cluster** is a QEMU socket netdev: a raw L2 link between QEMU processes, in
  userspace. No tap and no bridge, so `/dev/kvm` stays the only elevated thing
  the runner needs.

Four things about PVE that this had to learn the hard way, all asserted now
rather than discovered later:

- `/etc/pve` is pmxcfs, where a VMID is globally unique. Guest configs
  **move** between node directories; `cp` fails with "File exists" and `cp -a`
  fails outright. Losing them quietly means the baked VM template vanishes and
  every VM test silently skips.
- **A joining node must have no guests.** `pvecm add` refuses otherwise, so
  only node 1 keeps the baked VM template — the cluster's guests are node 1's.
- Every node image starts from the same base, so each must regenerate its
  **SSH host keys**, and the cluster must then run `pvecm updatecerts`. Without
  it, PVE's inherited `ssh_known_hosts` names the right node with the wrong
  key and any proxied API call fails — appearing as migrations timing out, not
  as an SSH error.
- A **UPID encodes the node it ran on**. Asking any other node for a task's
  status is a 500.

## Testing several storages at once

```bash
./bin/lab run --profile-dir ./profiles/zfs \
            --profile-dir ./profiles/btrfs \
            --profile-dir ./profiles/lvm-thin --cross
```

The suite runs **once per storage**, each with its own capabilities — proving
one backend works says nothing about the others. `--cross` adds the pairwise
tests: move a volume from every storage to every other and back, and check the
data is unchanged.

## Writing a profile

A profile is a directory:

```
profiles/<name>/
├── profile.env       # NAME, DISKS, optional TESTS / SOURCE
├── setup.sh          # runs on the node as root; registers the storage
├── capabilities.env  # what the storage supports; drives the skip logic
├── bake.sh           # optional: expensive setup, baked into an image once
├── expectations.toml # optional: this backend's known results
└── teardown.sh       # optional
```

`profile.env` makes a profile self-describing, so `--profile-dir` is the only
thing a caller passes:

```
NAME=bcachefs
DISKS=4              # lab test disks wanted; the lab assigns a disjoint range
TESTS=../tests       # extra pytest files, relative to the profile dir
SOURCE=../..         # a working tree to build and install, instead of a release
```

**Profiles must not go looking for disks.** The assigned devices arrive in
`$LAB_DISKS`; globbing for `virtio-labdisk1` works only while exactly one
profile exists, and silently steals another storage's disk the moment one
doesn't.

`setup.sh` must append `STORAGE_NAME=` to `$LAB_TEST_CONFIG`. Everything else
is discovered.

### Capabilities do not gate tests

Every test runs against every backend. A capability is a **label**, not a
switch: what a backend genuinely cannot do is declared in `expectations.toml`,
and the runner sorts the results into three buckets — failures, expected
failures, and *stale* expectations, which are declarations that started
passing.

That last bucket is the whole point. The obvious design is to skip a test a
backend does not support, and it is wrong in a way that hides bugs silently: a
capability declared `false` by mistake means the test never runs and nothing
says so. That is not hypothetical — `SUPPORTS_LINKED_CLONE=false` was declared
on a backend that supports linked clones perfectly well, and the tests sat
unrun for a day. Under this model they would have run, passed, and been
reported as a stale declaration.

```toml
[[expected]]
profile = "bcachefs"
marker = "ct_resize_visible"
kind = "limitation"
reason = "the rootfs is a subvolume on a much larger filesystem, so df inside
reports the filesystem rather than the quota - the limit is real and simply
not visible from in there"
```

Declared by capability **marker** rather than test name, because names drift
and capabilities do not; `test = "glob"` is there for the one-off. `kind` is
`limitation` for something a backend cannot do and `known-bug` for something
it should do and does not — the distinction is for whoever reads the report,
not for the runner.

`capabilities.env` still exists, for values a test needs to *read* rather than
be gated by: a mount point, whether project quotas are on, the command that
checks the filesystem is undamaged.

### Pointing it at your own plugin

A plugin repository keeps its own profile and passes it in, so this repository
never grows backend-specific knowledge:

```bash
./bin/lab run --profile-dir ../pve-bcachefs/test/profile --nodes 2
```

`TESTS=` and `SOURCE=` in `profile.env` do the rest. `SOURCE` ships the working
tree to **every** node, so a profile can build and install the thing under test
rather than pulling its last release — without it the lab tests whatever was
published, which is precisely the code you are not trying to find bugs in. It
goes to every node rather than the first, because a storage plugin is a Perl
module on each of them, and shipping it to one leaves the cluster behaving
differently depending on where a guest happens to land.

## Cross-storage

```bash
./bin/lab run --profile-dir ./profiles/zfs \
            --profile-dir ./profiles/btrfs \
            --profile-dir ./profiles/lvm-thin --cross
```

Proving a backend works on its own says nothing about what happens when data
crosses between two of them — and that is where the interesting failures are.
One backend's assumptions about layout, extended attributes, sparseness or
size only get tested when something else has to read or reproduce them.

Pairs are **ordered**, and direction is not a duplicate:

| pair | round trip |
|---|---|
| `(zfs, btrfs)` | `zfs > btrfs > zfs` |
| `(btrfs, zfs)` | `btrfs > zfs > btrfs` |

Each backend writes a volume in one and reads a foreign one in the other, and
a backend that reads correctly may still write its own wrongly. Deduplication
is only `a != b`, so N storages give N×(N−1) round trips, each appearing
exactly once. Container round trips run against all three payload shapes,
so the count is N×(N−1)×5.

The central assertion is **not** "the move succeeds". Some pairs legitimately
cannot round-trip — a volume on a compressing backend can hold more logical
data than its nominal size, and that will not fit on one that does not
compress. What must always hold is that a move either succeeds completely or
**fails cleanly**: the source still points at itself, its volume is still
there, nothing is left stranded on the destination, and the guest still boots
with its data. A move that reports success and delivers a truncated filesystem
is the real hazard, and that is what these tests are looking for.

## Phases

A whole run is one command, but each phase can be invoked on its own. CI wants
this: a failing step then names the thing that broke instead of burying it in
one long log.

```bash
./bin/lab run --profile-dir ./profiles/zfs --profile-dir ./profiles/btrfs --phase prepare
./bin/lab run --phase suite --only zfs
./bin/lab run --phase suite --only btrfs
./bin/lab run --phase report
./bin/lab run --phase teardown
```

`prepare` writes a plan into the lab directory — which profiles, which disks,
which storage each registered — and the later phases read it. So they need no
arguments of their own, and cannot be handed different ones half way through a
run.

Only a whole-run invocation clears previous results; a per-suite phase adds to
them, so `report` at the end sees every suite rather than just the last.

## Expected results

"Did the suite pass" is the wrong question once a backend has known defects: a
suite that is always red tells you nothing the day something new breaks. So the
exit status is driven by *unexpected* failures.

`expectations.toml` — in this repo, and optionally one per profile, merged —
declares results that are already known:

```toml
[[expected]]
profile = "bcachefs"
marker = "snapshot_migration"
kind = "limitation"
reason = "raw+size and tar+size have nowhere to put a snapshot, so PVE refuses
the migration up front rather than dropping them silently"
```

Three buckets come out, and the third is the one that matters:

| | |
|---|---|
| **failures** | not declared. These block |
| **expected failures** | declared and failed. Reported, do not block |
| **stale expectations** | declared and **passed**. Surfaced loudly, and they block too |

Without stale detection the file rots into a permanent mute button, and a fix
nobody noticed keeps its workaround forever. An expectation that no longer
holds is a claim about the system that has quietly become false, which is
exactly what the file exists to prevent.

`kind` distinguishes a **known-bug** — real, tracked, tolerated for now — from
a **limitation**, which is not a defect and will never be fixed. Prefer
recording limitations as facts rather than as suppressed failures.

## Payload shape is a test dimension

Moves, backups and integrity checks run against three fill patterns, because
several classes of bug appear for only one shape:

| | |
|---|---|
| `random` | incompressible — the only honest way to test a size limit |
| `compressible` | a short random line repeated, so logical and physical size diverge sharply |
| `sparse` | real holes, checked for inflation after a copy |

The period of the compressible pattern is deliberately far below the
compression block size. Repeating a 1 MiB random block looks compressible to a
human and is incompressible to lz4 — ZFS compresses per 128 KiB record, and
inside any one record that data is still random.

This matters more than it sounds. A `/dev/zero` fill tests nothing on a
compressing backend: ZFS elides all-zero blocks into holes, so a 1 GiB volume
happily swallows many gigabytes and a "quota not enforced" failure is really
measuring the compressor.

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

Some axes exist because a bug hid in them:

- **aio modes.** `test_aio.py` runs a verified write, an fsynced hard stop and
  a snapshot under live I/O through `io_uring`, `threads` and `native`. They
  take different paths into the kernel, so a storage can be well behaved in
  one and not the others, and everything else runs whatever PVE defaults to.
- **The CLI, not just the API.** `pct` and `qm` run Perl with `-T`, where a
  path built from a config file is tainted and `syscall()` refuses it;
  `pvedaemon` does not. A suite that only drives `pvesh` will pass in full
  against a storage on which `pct create` cannot create a container at all -
  which is exactly what happened.
- **Stopped volumes.** Comparing a guest's filesystem before and after an
  operation only means something with the guest stopped. Measured from inside
  a running container, a plain restart with no operation at all changes a
  dozen paths, and the check can never hold on any backend.
- **In-flight I/O.** Snapshots, rollbacks and live migrations are taken with
  `fio --verify` running, throttled so the test measures the operation rather
  than whether the guest can outrun it.

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

AGPL-3.0-or-later, the same as Proxmox VE and pve-bcachefs. See `LICENSE`.

A profile is configuration and shell that this harness consumes, so writing
one for your plugin keeps your plugin's licence entirely to itself. Test files
that import from `suite/` are a different matter - those are derivative of
this, and AGPL applies to them.
