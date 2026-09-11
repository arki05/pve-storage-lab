"""Where the lab keeps its things."""

from __future__ import annotations

import os
from pathlib import Path


def state_dir() -> Path:
    return Path(os.environ.get(
        "PVE_LAB_STATE", Path.home() / ".local/share/pve-storage-lab"))


def images_dir() -> Path:
    return state_dir() / "images"


def image_path(version: str, variant: str = "base", node: int | None = None) -> Path:
    name = f"pve-{version}-{variant}"
    if node is not None:
        name += f"-node{node}"
    return images_dir() / f"{name}.qcow2"


def lab_dir(name: str) -> Path:
    return state_dir() / "labs" / name


def ssh_key() -> Path:
    return state_dir() / "id_ed25519"
