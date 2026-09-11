"""Running commands inside guests.

VMs go through the QEMU guest agent via `qm guest exec`; containers through
`pct exec`. Both are invoked as subprocesses rather than over the API - the
agent-exec endpoint has historically been the flakiest part of the HTTP
interface, and we are already local.
"""

import base64
import json
import shlex
import socket
import subprocess


class GuestExecError(RuntimeError):
    pass


def _local_node() -> str:
    return socket.gethostname()


def _wrap_for_node(node: str | None, argv: list[str]) -> list[str]:
    """Run argv on `node`, going over SSH if that is not this machine.

    `qm` and `pct` only act on guests that live on the node they run on, so
    after a migration the same command has to be issued somewhere else. The
    whole remote command is quoted as one token: without that, `bash -c 'md5sum
    /srv/x'` arrives at the far end as `bash -c md5sum /srv/x`, which runs
    md5sum with no argument against empty stdin and cheerfully returns the
    checksum of nothing.
    """
    if node is None or node == _local_node():
        return argv
    remote = " ".join(shlex.quote(part) for part in argv)
    return ["ssh", "-n", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
            node, remote]


class _Exec:
    def run(self, command: str, timeout: int = 60) -> str:
        """Run a command, assert it succeeded, return stdout."""
        result = self.exec(command, timeout)
        if result["exitcode"] != 0:
            raise GuestExecError(
                f"command failed (exit {result['exitcode']}): {command}\n"
                f"stderr: {result['stderr']}"
            )
        return result["stdout"]

    def write_file(self, path: str, content: str) -> None:
        b64 = base64.b64encode(content.encode()).decode()
        self.run(f"echo '{b64}' | base64 -d > {path}")

    def read_file(self, path: str) -> str:
        return self.run(f"cat {path}")

    def md5(self, path: str) -> str:
        return self.run(f"md5sum {path}").split()[0]


class GuestAgent(_Exec):
    """Commands inside a VM, via the QEMU guest agent."""

    def __init__(self, vmid: int, node: str | None = None):
        self.vmid = vmid
        self.node = node

    def exec(self, command: str, timeout: int = 60) -> dict:
        proc = subprocess.run(
            _wrap_for_node(self.node,
                           ["qm", "guest", "exec", str(self.vmid), "--",
                            "bash", "-c", command]),
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0 and not proc.stdout:
            raise GuestExecError(
                f"qm guest exec failed (exit {proc.returncode}): {proc.stderr}"
            )
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {"exitcode": proc.returncode, "stdout": proc.stdout,
                    "stderr": proc.stderr}
        return {
            "exitcode": data.get("exitcode", -1),
            "stdout": data.get("out-data", ""),
            "stderr": data.get("err-data", ""),
        }


class ContainerExec(_Exec):
    """Commands inside an LXC container, via pct exec."""

    def __init__(self, vmid: int, node: str | None = None):
        self.vmid = vmid
        self.node = node

    def exec(self, command: str, timeout: int = 60) -> dict:
        proc = subprocess.run(
            _wrap_for_node(self.node,
                           ["pct", "exec", str(self.vmid), "--",
                            "bash", "-c", command]),
            capture_output=True, text=True, timeout=timeout,
        )
        return {"exitcode": proc.returncode, "stdout": proc.stdout,
                "stderr": proc.stderr}
