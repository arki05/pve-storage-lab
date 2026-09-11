"""Wait-for-condition helpers."""

import time
from typing import Callable


def wait_for(condition: Callable[[], bool], timeout: int = 120,
             interval: float = 2, desc: str = "condition") -> None:
    start = time.time()
    last_err = None
    while time.time() - start < timeout:
        try:
            if condition():
                return
        except Exception as exc:       # noqa: BLE001 - report, don't mask
            last_err = exc
        time.sleep(interval)
    msg = f"timed out waiting for {desc} after {timeout}s"
    if last_err:
        msg += f" (last error: {last_err})"
    raise TimeoutError(msg)


def wait_for_task(pve, upid, timeout: int = 300) -> None:
    """Wait for a PVE task, raising if it fails.

    Many PVE calls return a UPID and run asynchronously. Returning before the
    task finishes is the single most common source of flaky storage tests: the
    next test takes the same VMID from /cluster/nextid while the previous
    volume is still on disk.
    """
    if upid is None or isinstance(upid, (dict, list)):
        return
    if not isinstance(upid, str) or not upid.startswith("UPID"):
        # Deliberately loud. This used to return quietly, which turned every
        # "we waited for the task" into a no-op the moment pvesh printed
        # something other than a bare UPID - and an aborted migration then
        # looked like a lock that never cleared.
        raise RuntimeError(
            f"expected a UPID to wait on, got {type(upid).__name__}: "
            f"{str(upid)[:300]}")

    # A UPID carries the node it ran on: UPID:<node>:<pid>:<pstart>:<start>:...
    # Asking any other node for it is a 500, which is how every migration test
    # failed the first time this ran on a cluster - the task belongs to the
    # node the guest was on *before* it moved, not to whichever node the client
    # happens to be pointed at.
    parts = upid.split(":")
    node = parts[1] if len(parts) > 2 and parts[1] else pve.node()
    start = time.time()
    while time.time() - start < timeout:
        status = pve.get(f"/nodes/{node}/tasks/{upid}/status")
        if status.get("status") == "stopped":
            exitstatus = status.get("exitstatus") or ""
            # PVE reports "OK" on success and "WARNINGS: N" when the task
            # finished but printed warnings - routine for LXC and storage
            # operations on PVE 9, and not a failure.
            if exitstatus == "OK" or exitstatus.startswith("WARNINGS"):
                return
            log = pve.get(f"/nodes/{node}/tasks/{upid}/log") or []
            tail = " | ".join(entry.get("t", "") for entry in log[-8:])
            raise RuntimeError(f"task failed ({exitstatus}): {tail}")
        time.sleep(1)
    raise TimeoutError(f"task {upid} did not finish within {timeout}s")
