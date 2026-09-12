# contrib

Profiles for things this repository does not maintain.

`profiles/bcachefs-mm` tests
[MmAaXx500/pve-storage-plugin-bcachefs](https://github.com/MmAaXx500/pve-storage-plugin-bcachefs),
a separate bcachefs plugin. It installs `PVE/Storage/Custom/BcachefsPlugin.pm`
and registers the storage type `bcachefs` — the same file and type as
pve-bcachefs — so the two cannot be installed on the same node. Run it in its
own lab:

```bash
./bin/lab run --name other --profile-dir ./contrib/profiles/bcachefs-mm --nodes 2
```

It pins a release `.deb` rather than building from source, so a run says what
that release does, not what the branch does.
