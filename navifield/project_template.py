"""Portable project folder initialization for the agent engine."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


PROJECT_DIRECTORIES = (
    "CAD", "model", "simulation_setup", "result", "report",
    "code", "content", "temporal", ".navifield",
)


def init_project(path: str | Path) -> dict:
    """Create missing project folders and return persisted project metadata.

    Existing metadata and files are preserved, including run numbering.
    """
    root = Path(path)
    for directory in PROJECT_DIRECTORIES:
        (root / directory).mkdir(parents=True, exist_ok=True)

    metadata_path = root / ".navifield" / "project.json"
    if metadata_path.exists():
        return json.loads(metadata_path.read_text(encoding="utf-8"))

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    metadata = {
        "schema_version": 1,
        "project_id": f"p_{uuid4().hex}",
        "name": root.resolve().name,
        "created_at": now,
        "updated_at": now,
        "next_run_number": 1,
        "runs": [],
        "accepted_geometry": None,
    }
    with metadata_path.open("x", encoding="utf-8") as stream:
        json.dump(metadata, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return metadata
