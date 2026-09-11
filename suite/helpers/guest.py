"""Running commands inside guests.

VMs go through the QEMU guest agent via `qm guest exec`; containers through
`pct exec`. Both are invoked as subprocesses rather than over the API - the
agent-exec endpoint has historically been the flakiest part of the HTTP
interface, and we are already local.
"""

import base64
import json
import subprocess


class GuestExecError(RuntimeError):
    pass


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

    def __init__(self, vmid: int):
        self.vmid = vmid

    def exec(self, command: str, timeout: int = 60) -> dict:
        proc = subprocess.run(
            ["qm", "guest", "exec", str(self.vmid), "--", "bash", "-c", command],
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

    def __init__(self, vmid: int):
        self.vmid = vmid

    def exec(self, command: str, timeout: int = 60) -> dict:
        proc = subprocess.run(
            ["pct", "exec", str(self.vmid), "--", "bash", "-c", command],
            capture_output=True, text=True, timeout=timeout,
        )
        return {"exitcode": proc.returncode, "stdout": proc.stdout,
                "stderr": proc.stderr}
