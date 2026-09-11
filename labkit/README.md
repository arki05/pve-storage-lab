# labkit

Orchestration for the lab. Python here, shell on the node.

The split is deliberate. Everything that runs *on* a PVE node is `pct`, `qm`,
`pvesm`, `apt` — shell is the right language for that, and those scripts live
in `labkit/remote/`, pushed as files and run with arguments. Everything that
*decides what to run* is orchestration: structured data, error propagation,
concurrency. That was shell too, and it kept going wrong in the same few ways.

Three of those failure modes are now impossible rather than remembered:

- **`ssh` eating the caller's stdin.** `NodeSSH` passes `-n` unless a helper
  deliberately feeds it. A script piped into `bash -s` used to lose the rest of
  itself to the first `ssh` it ran, and bash would reach end of input and exit
  0 — the step "succeeded" having done half its work.
- **Heredocs with mixed expansion.** `run_script()` takes a file and arguments,
  so there is no question of which side a variable belongs to. The errors that
  produced (`cp: missing file operand`, `vmid: unbound variable`) never named
  the cause.
- **Four copies of "boot a QEMU node".** There is one `Machine`. When the
  image's network layout changed, three of the four copies were updated and the
  fourth sat at a login prompt for twenty minutes saying nothing.

## Layout

| | |
|---|---|
| `nodes.py` | a `Node` is its index; hostname, MACs, interfaces and addresses all derive from it |
| `qemu.py` | `Machine`, `Disk`, `UserNet`, `SocketNet` — the one boot primitive, with a fixed device layout |
| `ssh.py` | `NodeSSH` — run, probe, push, run a script with arguments |
| `state.py` | where images and labs live |
| `lab.py` | up and down; nodes boot in parallel |
| `remote/` | shell that runs on a node |
