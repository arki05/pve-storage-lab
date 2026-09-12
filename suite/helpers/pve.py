"""PVE API access for tests running on the node itself.

Goes through `pvesh` rather than an HTTP client on purpose. The suite always
runs as root on the node under test, so pvesh needs no credentials, no TLS
handling and no session that can go stale mid-run - which removes the whole
reconnect-on-broken-pipe dance an HTTP client needs for session-scoped
fixtures. The cost is a fork per call; at a few hundred calls per run that is
not worth optimising away.
"""

import json
import re
import socket
import subprocess

# UPID:<node>:<pid>:<pstart>:<starttime>:<type>:<id>:<user>: - the trailing
# colon is part of it, and it is followed by a quote or whitespace wherever it
# appears in pvesh's output.
UPID_RE = re.compile(r'UPID:[^\s"\']+')


class PVEError(RuntimeError):
    """A pvesh call failed. `stderr` carries what PVE actually said, which is
    what a test asserting on a *rejection* wants to match against."""

    def __init__(self, args, returncode, stderr):
        self.returncode = returncode
        self.stderr = stderr.strip()
        super().__init__(f"pvesh {' '.join(args)} failed ({returncode}): {self.stderr}")


class PVE:
    """Thin pvesh wrapper. Paths are API paths: '/nodes/pve-lab/qemu'."""

    # pvesh waits for the task it starts, so this timeout bounds the whole
    # operation, not just the request. 120s was enough for everything until a
    # migration, which then failed as a client timeout reported against
    # whatever the test checked next - the lock, usually, which had nothing to
    # do with it.
    DEFAULT_TIMEOUT = 600

    def _run(self, verb: str, path: str, params: dict | None = None,
             timeout: int | None = None):
        timeout = timeout or self.DEFAULT_TIMEOUT
        args = [verb, path, "--output-format", "json"]
        for key, value in (params or {}).items():
            if value is None:
                continue
            if isinstance(value, bool):
                value = 1 if value else 0
            args += [f"--{key}", str(value)]

        proc = subprocess.run(["pvesh", *args], capture_output=True, text=True,
                              timeout=timeout)
        if proc.returncode != 0:
            raise PVEError(args, proc.returncode, proc.stderr)

        out = proc.stdout.strip()
        if not out:
            return None

        missing = object()
        try:
            decoded = json.loads(out)
        except json.JSONDecodeError:
            decoded = missing

        # Objects and arrays are plain data; nothing below applies to them.
        if decoded is not missing and not isinstance(decoded, str):
            return decoded

        # Everything from here is text, and a task-starting endpoint hides the
        # UPID in it rather than returning it cleanly. pvesh exits 0 even when
        # the task it watched failed, so missing the UPID means missing the
        # failure: a migration could abort and the test would sail past it, to
        # fail much later on something unrelated.
        #
        # Its position is not dependable, and neither is the shape of the
        # output. Called against the local node it is streamed log lines with
        # the UPID last; glued to the end of a warning with no newline for
        # some calls ('...enable nesting."UPID:...:vzstart:102:root@pam:"');
        # and - the one that cost a full run to find - when pvesh proxies to
        # *another* node's endpoint the entire task log comes back as one JSON
        # string, which parses cleanly and is not a UPID at all. That last
        # shape is why this cannot simply trust a successful json.loads: the
        # outbound half of a migration round trip decoded to a UPID and the
        # return half decoded to the log.
        text = decoded if decoded is not missing else out
        if text.startswith("UPID:"):
            return text
        for line in reversed(text.splitlines()):
            try:
                value = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
            if isinstance(value, str) and value.startswith("UPID:"):
                return value
        found = UPID_RE.findall(text)
        if found:
            return found[-1]
        return text

    # `_timeout` is popped rather than passed to pvesh: an operation that
    # legitimately takes an hour - a migration, a large backup - needs to say
    # so, and everything else keeps the default.
    def get(self, path, **params):
        return self._run("get", path, params, params.pop("_timeout", None))

    def create(self, path, **params):
        timeout = params.pop("_timeout", None)
        return self._run("create", path, params, timeout)

    def set(self, path, **params):
        timeout = params.pop("_timeout", None)
        return self._run("set", path, params, timeout)

    def delete(self, path, **params):
        timeout = params.pop("_timeout", None)
        return self._run("delete", path, params, timeout)

    # ── Convenience ──────────────────────────────────────────────────────────

    def node(self) -> str:
        """The node these tests are running on.

        Deliberately the local hostname rather than /nodes[0]: that list comes
        back in no particular order, so on a cluster it can name the *other*
        node - which made tests migrate a guest to the node it was already on,
        and look for the VM template on the node that does not have it.
        """
        local = socket.gethostname()
        names = [entry["node"] for entry in self.get("/nodes")]
        if local in names:
            return local
        return sorted(names)[0]

    def other_node(self) -> str | None:
        """A node that is not this one, or None on a single-node lab."""
        here = self.node()
        for name in sorted(entry["node"] for entry in self.get("/nodes")):
            if name != here:
                return name
        return None

    def nextid(self) -> int:
        return int(self.get("/cluster/nextid"))

    def storage_content(self, storage: str, content: str | None = None) -> list:
        node = self.node()
        params = {"content": content} if content else {}
        return self.get(f"/nodes/{node}/storage/{storage}/content", **params) or []
