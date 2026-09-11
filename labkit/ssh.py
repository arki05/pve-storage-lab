"""Talking to a lab node.

Two rules here, both learned the hard way:

  ssh never reads the caller's stdin unless asked. Without -n it does, and a
  shell script piped into `bash -s` then loses the rest of itself to the first
  ssh it runs - bash simply reaches end of input and exits 0, so the step
  "succeeds" having done half its work.

  Scripts are pushed as files and executed with arguments, never piped in and
  never interpolated into a heredoc. A heredoc that expands some variables
  locally and others remotely is a constant source of values landing on the
  wrong side, and the errors it produces ("cp: missing file operand") say
  nothing about the cause.
"""

from __future__ import annotations

import subprocess
import tarfile
import tempfile
from pathlib import Path

SSH_OPTS = [
    # The node is reachable only through a forwarded port on localhost, so
    # there is no host key worth checking and no network to trust.
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "ConnectTimeout=5",
    "-o", "BatchMode=yes",
]


class RemoteError(RuntimeError):
    def __init__(self, command, returncode, stderr):
        self.returncode = returncode
        self.stderr = (stderr or "").strip()
        super().__init__(f"remote command failed ({returncode}): {command}\n{self.stderr}")


class NodeSSH:
    def __init__(self, port: int, key: Path, host: str = "127.0.0.1",
                 user: str = "root"):
        self.port = port
        self.key = key
        self.target = f"{user}@{host}"

    def _base(self, read_stdin: bool) -> list[str]:
        argv = ["ssh"]
        if not read_stdin:
            argv.append("-n")
        return argv + SSH_OPTS + ["-i", str(self.key), "-p", str(self.port),
                                  self.target]

    def run(self, command: str, check: bool = True, timeout: int = 600) -> str:
        proc = subprocess.run(self._base(False) + [command],
                              capture_output=True, text=True, timeout=timeout)
        if check and proc.returncode != 0:
            raise RemoteError(command, proc.returncode, proc.stderr)
        return proc.stdout

    def ok(self, command: str, timeout: int = 60) -> bool:
        """True when the command succeeds. For probing, not for asserting."""
        try:
            proc = subprocess.run(self._base(False) + [command],
                                  capture_output=True, text=True, timeout=timeout)
            return proc.returncode == 0
        except subprocess.SubprocessError:
            return False

    def alive(self) -> bool:
        return self.ok("true", timeout=10)

    def wait(self, timeout: int = 600, interval: int = 5) -> bool:
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.alive():
                return True
            time.sleep(interval)
        return False

    def push_dir(self, local: Path, remote: str) -> None:
        with tempfile.NamedTemporaryFile(suffix=".tar.gz") as tmp:
            with tarfile.open(tmp.name, "w:gz") as tar:
                for item in sorted(Path(local).iterdir()):
                    if item.name in {"__pycache__", ".git"}:
                        continue
                    tar.add(item, arcname=item.name)
            tmp.flush()
            with open(tmp.name, "rb") as handle:
                proc = subprocess.run(
                    self._base(True) + [f"mkdir -p {remote} && tar xz -C {remote}"],
                    stdin=handle, capture_output=True, text=True, timeout=900)
        if proc.returncode != 0:
            raise RemoteError(f"push {local} -> {remote}", proc.returncode, proc.stderr)

    def push_file(self, local: Path, remote: str) -> None:
        with open(local, "rb") as handle:
            proc = subprocess.run(self._base(True) + [f"cat > {remote}"],
                                  stdin=handle, capture_output=True, text=True,
                                  timeout=300)
        if proc.returncode != 0:
            raise RemoteError(f"push {local} -> {remote}", proc.returncode, proc.stderr)

    def run_script(self, script: Path, *args: str, check: bool = True,
                   timeout: int = 3600) -> str:
        """Push a script and run it with arguments.

        Arguments rather than interpolation: the script is a fixed file that
        expands nothing at build time, so there is no question of which side a
        variable belongs to.
        """
        remote = f"/root/.lab-step-{script.name}"
        self.push_file(script, remote)
        import shlex
        quoted = " ".join(shlex.quote(a) for a in args)
        return self.run(f"bash {remote} {quoted}", check=check, timeout=timeout)
