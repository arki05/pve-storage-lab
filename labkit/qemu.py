"""Booting a QEMU machine, and talking to it.

This is the one place that knows how to start a lab VM. It exists because
there were four - the image builder, the node-image builder, the profile bake
and the lab itself - and when the image's network layout changed, three were
updated and one was not. That one booted a single-NIC guest against a two-NIC
image and sat at a login prompt for twenty minutes saying nothing.

Device layout is fixed and shared by every caller, which is the other half of
the same problem:

  0x10  system disk, bootindex=0     SeaBIOS otherwise picks by PCI slot order,
                                     and adding a test disk stops the node
                                     booting with no error anywhere
  0x11  management NIC
  0x12  guest NIC (vmbr0)
  0x13+ test disks, addressed by serial rather than by letter
"""

from __future__ import annotations

import shlex
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

SYSTEM_ADDR = 0x10
MGMT_ADDR = 0x11
GUEST_ADDR = 0x12
CLUSTER_ADDR = 0x13
FIRST_DISK_ADDR = 0x14


class QemuError(RuntimeError):
    pass


@dataclass
class Disk:
    """A test disk. Serials are what profiles address, so the name a profile
    sees never depends on enumeration order."""

    path: Path
    serial: str
    cache: str = "unsafe"   # a failed build is thrown away, never resumed


@dataclass
class UserNet:
    """User-mode networking: no tap, no bridge, so no NET_ADMIN and no
    /dev/net/tun. That is what lets the whole lab run in an unprivileged
    container with /dev/kvm as its only elevated device."""

    mac: str
    addr: int
    net: str | None = None
    gw: str | None = None
    dhcpstart: str | None = None
    forwards: list[tuple[int, str, int]] = field(default_factory=list)
    ident: str = "mgmt"

    def args(self) -> list[str]:
        opts = [f"user,id={self.ident}"]
        if self.net:
            opts.append(f"net={self.net}")
        if self.gw:
            opts.append(f"host={self.gw}")
        if self.dhcpstart:
            opts.append(f"dhcpstart={self.dhcpstart}")
        for host_port, guest_ip, guest_port in self.forwards:
            opts.append(f"hostfwd=tcp:127.0.0.1:{host_port}-{guest_ip}:{guest_port}")
        return ["-netdev", ",".join(opts),
                "-device", f"virtio-net-pci,netdev={self.ident},"
                           f"addr={hex(self.addr)},mac={self.mac}"]


@dataclass
class SocketNet:
    """A raw L2 link between QEMU processes. Multicast rather than a
    point-to-point pair so one form works for any node count."""

    mac: str
    addr: int
    mcast: str
    ident: str = "clus"

    def args(self) -> list[str]:
        return ["-netdev", f"socket,id={self.ident},mcast={self.mcast}",
                "-device", f"virtio-net-pci,netdev={self.ident},"
                           f"addr={hex(self.addr)},mac={self.mac}"]


@dataclass
class Machine:
    """A QEMU machine under the lab's control."""

    name: str
    image: Path
    workdir: Path
    mem: int = 6144
    cpus: int = 4
    nets: list = field(default_factory=list)
    disks: list[Disk] = field(default_factory=list)
    cdrom: Path | None = None
    boot_from_cdrom: bool = False
    # -no-reboot turns a guest reboot into a clean exit, which is how an
    # installer signals it has finished.
    no_reboot: bool = False
    system_cache: str | None = None
    monitor: bool = True

    @property
    def pidfile(self) -> Path:
        return self.workdir / f"{self.name}.pid"

    @property
    def console(self) -> Path:
        return self.workdir / f"{self.name}.console.log"

    @property
    def monitor_sock(self) -> Path:
        return self.workdir / f"{self.name}.monitor.sock"

    def _argv(self) -> list[str]:
        system = f"file={self.image},if=none,id=sys,format=qcow2"
        if self.system_cache:
            system += f",cache={self.system_cache}"

        argv = [
            "qemu-system-x86_64",
            "-enable-kvm",
            # -cpu host passes the virtualisation flags through, which the
            # nested guests inside a PVE node need.
            "-cpu", "host", "-machine", "q35",
            "-smp", str(self.cpus), "-m", str(self.mem),
            "-drive", system,
            "-device", f"virtio-blk-pci,drive=sys,serial=labsystem,"
                       f"addr={hex(SYSTEM_ADDR)},bootindex=0",
        ]
        if self.cdrom:
            argv += ["-cdrom", str(self.cdrom)]
            if self.boot_from_cdrom:
                argv += ["-boot", "d"]
        for net in self.nets:
            argv += net.args()
        for i, disk in enumerate(self.disks):
            argv += [
                "-drive", f"file={disk.path},if=none,id=d{i},format=raw,"
                          f"cache={disk.cache}",
                "-device", f"virtio-blk-pci,drive=d{i},serial={disk.serial},"
                           f"addr={hex(FIRST_DISK_ADDR + i)}",
            ]
        argv += ["-display", "none", "-serial", f"file:{self.console}"]
        if self.monitor:
            argv += ["-monitor", f"unix:{self.monitor_sock},server,nowait"]
        if self.no_reboot:
            argv += ["-no-reboot"]
        argv += ["-pidfile", str(self.pidfile), "-daemonize"]
        return argv

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.monitor_sock.unlink(missing_ok=True)
        proc = subprocess.run(self._argv(), capture_output=True, text=True)
        if proc.returncode != 0:
            raise QemuError(
                f"{self.name}: qemu refused to start: {proc.stderr.strip()}\n"
                f"  {' '.join(shlex.quote(a) for a in self._argv())}")

    def run_to_completion(self, timeout: int) -> None:
        """Start without -daemonize semantics: wait for the machine to exit.

        Used for the installer, where the guest rebooting (and so QEMU exiting,
        because of -no-reboot) is the signal that it has finished.
        """
        self.workdir.mkdir(parents=True, exist_ok=True)
        argv = [a for a in self._argv() if a != "-daemonize"]
        try:
            subprocess.run(argv, timeout=timeout, check=True)
        except subprocess.TimeoutExpired as exc:
            raise QemuError(
                f"{self.name}: timed out after {timeout}s - see {self.console}"
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise QemuError(
                f"{self.name}: exited {exc.returncode} - see {self.console}"
            ) from exc

    def pid(self) -> int | None:
        try:
            return int(self.pidfile.read_text().strip())
        except (OSError, ValueError):
            return None

    def running(self) -> bool:
        pid = self.pid()
        if pid is None:
            return False
        try:
            import os
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def kill(self, timeout: int = 30) -> None:
        import os
        import signal
        pid = self.pid()
        if pid is None:
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            self.pidfile.unlink(missing_ok=True)
            return
        for _ in range(timeout):
            if not self.running():
                break
            time.sleep(1)
        if self.running():
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        self.pidfile.unlink(missing_ok=True)

    def wait_until_stopped(self, timeout: int = 120) -> bool:
        for _ in range(timeout // 2):
            if not self.running():
                return True
            time.sleep(2)
        return not self.running()

    # ── Monitor ──────────────────────────────────────────────────────────────

    def monitor_command(self, command: str) -> str:
        """Talk to the running machine. Used for things that cannot be
        expressed on the command line, like moving a port forward."""
        sock = socket.socket(socket.AF_UNIX)
        try:
            sock.connect(str(self.monitor_sock))
            time.sleep(0.3)
            sock.recv(65536)
            sock.sendall(command.encode() + b"\n")
            time.sleep(0.4)
            return sock.recv(65536).decode(errors="replace")
        finally:
            sock.close()
