"""Write a public cantilever example in mm, N and MPa."""

from pathlib import Path
import sys

case_dir = Path(sys.argv[1])
case_dir.mkdir(parents=True, exist_ok=False)
# The 1000 × 100 × 100 mm beam is fixed at one end; units are N and MPa.
axial_cells, width_cells, height_cells = 40, 6, 6


def node_id(axial_index, width_index, height_index):
    return (
        (axial_index * (width_cells + 1) + width_index) * (height_cells + 1)
        + height_index
        + 1
    )


lines = ["*NODE"]
for axial_index in range(axial_cells + 1):
    for width_index in range(width_cells + 1):
        for height_index in range(height_cells + 1):
            lines.append(
                f"{node_id(axial_index, width_index, height_index)},{1000 * axial_index / axial_cells:.12g},{100 * width_index / width_cells:.12g},{100 * height_index / height_cells:.12g}"
            )
lines.append("*ELEMENT,TYPE=C3D8I,ELSET=ALL")
element_id = 0
for axial_index in range(axial_cells):
    for width_index in range(width_cells):
        for height_index in range(height_cells):
            element_id += 1
            node_ids = (
                node_id(axial_index, width_index, height_index),
                node_id(axial_index + 1, width_index, height_index),
                node_id(axial_index + 1, width_index + 1, height_index),
                node_id(axial_index, width_index + 1, height_index),
                node_id(axial_index, width_index, height_index + 1),
                node_id(axial_index + 1, width_index, height_index + 1),
                node_id(axial_index + 1, width_index + 1, height_index + 1),
                node_id(axial_index, width_index + 1, height_index + 1),
            )
            lines.append(str(element_id) + "," + ",".join(map(str, node_ids)))


def write_nodeset(name, node_ids):
    lines.append("*NSET,NSET=" + name)
    for batch_start in range(0, len(node_ids), 16):
        lines.append(",".join(map(str, node_ids[batch_start : batch_start + 16])))


write_nodeset(
    "FIXED",
    [
        node_id(0, width_index, height_index)
        for width_index in range(width_cells + 1)
        for height_index in range(height_cells + 1)
    ],
)
write_nodeset(
    "TIP",
    [
        node_id(axial_cells, width_index, height_index)
        for width_index in range(width_cells + 1)
        for height_index in range(height_cells + 1)
    ],
)
lines += [
    "*MATERIAL,NAME=STEEL",
    "*ELASTIC",
    "210000,0.3",
    "*SOLID SECTION,ELSET=ALL,MATERIAL=STEEL",
    "*BOUNDARY",
    "FIXED,1,3",
    "*STEP",
    "*STATIC",
    "*CLOAD",
]
for width_index in range(width_cells + 1):
    for height_index in range(height_cells + 1):
        # Trapezoidal weights distribute the total 1000 N load over the end face.
        weight = (0.5 if width_index in (0, width_cells) else 1) * (
            0.5 if height_index in (0, height_cells) else 1
        )
        lines.append(
            f"{node_id(axial_cells, width_index, height_index)},2,{-1000 * weight / (width_cells * height_cells):.12g}"
        )
lines += ["*NODE PRINT,NSET=TIP", "U", "*END STEP"]
(case_dir / "beam.inp").write_text("\n".join(lines) + "\n", encoding="ascii")
print(case_dir)
