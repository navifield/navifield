"""Compare the final 30 interior probes to Ghia et al.; report boundary condition separately."""

from pathlib import Path
import math
import re
import sys

case_dir = Path(sys.argv[1])
files = []
for path in (case_dir / "postProcessing/cavityProbes").glob("*/U"):
    try:
        files.append((float(path.parent.name), path))
    except ValueError:
        pass
if not files:
    raise ValueError("No cavity probe table")
source = max(files)[1]
line = next(
    line
    for line in reversed(source.read_text(encoding="utf-8").splitlines())
    if line.strip() and not line.startswith("#")
)
# The first 15 probes are horizontal velocity on the vertical centreline;
# the remaining 15 are vertical velocity on the horizontal centreline.
probe_vectors = [
    tuple(map(float, value.split())) for value in re.findall(r"\(([^()]*)\)", line)
]
reference_horizontal_velocity = [
    0.65928,
    0.57492,
    0.51117,
    0.46604,
    0.33304,
    0.18719,
    0.05702,
    -0.06080,
    -0.10648,
    -0.27805,
    -0.38289,
    -0.29730,
    -0.22220,
    -0.20196,
    -0.18109,
]
reference_vertical_velocity = [
    -0.21388,
    -0.27669,
    -0.33714,
    -0.39188,
    -0.51550,
    -0.42665,
    -0.31966,
    0.02526,
    0.32235,
    0.33075,
    0.37095,
    0.32627,
    0.30353,
    0.29012,
    0.27485,
]
if len(probe_vectors) != 30 or any(
    len(probe_vector) != 3
    or not all(math.isfinite(component) for component in probe_vector)
    for probe_vector in probe_vectors
):
    raise ValueError("Expected 30 finite interior probe vectors")
probe_time = float(line.split()[0])
if not math.isfinite(probe_time):
    raise ValueError("Invalid probe time")
max_error = max(
    [
        abs(probe_vectors[probe_index][0] - component)
        for probe_index, component in enumerate(reference_horizontal_velocity)
    ]
    + [
        abs(probe_vectors[probe_index + 15][1] - component)
        for probe_index, component in enumerate(reference_vertical_velocity)
    ]
)
written_times = []
for path in case_dir.iterdir():
    try:
        if path.is_dir() and (path / "U").is_file():
            written_times.append((float(path.name), path))
    except ValueError:
        pass
if not written_times:
    raise ValueError("No written velocity field")
velocity = (max(written_times)[1] / "U").read_text(encoding="utf-8")
blocks = dict(
    re.findall(
        r"([A-Za-z_][A-Za-z0-9_]*)\s*\{([^{}]*)\}",
        velocity.split("boundaryField", 1)[1],
        re.S,
    )
)
lid_boundary = blocks.get("movingWall", "")
wall_boundary = blocks.get("fixedWalls", "")
correct_lid = bool(
    re.search(r"\btype\s+fixedValue\s*;", lid_boundary)
    and re.search(
        r"\bvalue\s+uniform\s*\(\s*1(?:\.0*)?\s+0(?:\.0*)?\s+0(?:\.0*)?\s*\)",
        lid_boundary,
    )
)
correct_walls = bool(
    re.search(r"\btype\s+noSlip\s*;", wall_boundary)
    or re.search(
        r"\bvalue\s+uniform\s*\(\s*0(?:\.0*)?\s+0(?:\.0*)?\s+0(?:\.0*)?\s*\)",
        wall_boundary,
    )
)
print(
    f"max_abs_error_over_lid={max_error:.8g}; interior_points=30; boundary_points=4; boundary_valid={correct_lid and correct_walls}; probe_time={probe_time:g}; field_time={max(written_times)[0]:g}; limit=0.05"
)
sys.exit(0 if max_error <= 0.05 and correct_lid and correct_walls else 1)
