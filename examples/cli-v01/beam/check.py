"""Read native tip nodal displacements and integrate the uniform end section."""

from pathlib import Path
import re
import sys

case_dir = Path(sys.argv[1])
text = (case_dir / "beam.dat").read_text(encoding="ascii", errors="replace")
chunks = re.split(r"displacements\s*\(vx,vy,vz\)\s*for set TIP", text, flags=re.I)
if len(chunks) < 2:
    raise ValueError("No native tip displacement block")
tip_displacements = {}
for line in chunks[-1].splitlines():
    parts = line.split()
    if len(parts) == 4:
        try:
            tip_displacements[int(parts[0])] = float(parts[2])
        except ValueError:
            pass
axial_cells, width_cells, height_cells = 40, 6, 6


def node_id(axial_index, width_index, height_index):
    return (
        (axial_index * (width_cells + 1) + width_index) * (height_cells + 1)
        + height_index
        + 1
    )


weighted_sum = 0.0
# Integrate the tip displacement using the same trapezoidal nodal weights.
for width_index in range(width_cells + 1):
    for height_index in range(height_cells + 1):
        weight = (0.5 if width_index in (0, width_cells) else 1) * (
            0.5 if height_index in (0, height_cells) else 1
        )
        weighted_sum += (
            weight * tip_displacements[node_id(axial_cells, width_index, height_index)]
        )
mean_tip_deflection = -weighted_sum / (width_cells * height_cells)
# Euler–Bernoulli reference uses E=210000 MPa and the rectangular second moment.
euler_bernoulli_deflection = 1000 * 1000**3 / (3 * 210000 * (100 * 100**3 / 12))
relative_error = (
    abs(mean_tip_deflection - euler_bernoulli_deflection) / euler_bernoulli_deflection
)
print(
    f"tip_mean_down_mm={mean_tip_deflection:.9g}; euler_bernoulli_mm={euler_bernoulli_deflection:.9g}; relative_error={relative_error:.6g}; limit=0.02"
)
sys.exit(0 if relative_error <= 0.02 else 1)
