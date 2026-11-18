"""Create the reduced-mesh public turbulent pipe case."""

import json
import shutil
import sys
from importlib import resources
from pathlib import Path


def make_case(case_dir, template_dir):
    case_dir.mkdir(parents=True, exist_ok=False)
    for directory in ("0", "constant", "system"):
        shutil.copytree(template_dir / directory, case_dir / directory)
    parameters = json.loads(
        (template_dir / "template.json").read_text(encoding="utf-8")
    )["parameters"]
    # The public pipe uses low-y+ wall conditions and an empty axis patch.
    parameters.update(
        wall_k_condition="type fixedValue; value uniform 0;",
        wall_nut_condition="type nutLowReWallFunction; value uniform 0;",
        wall_omega_condition="type omegaWallFunction; value uniform 1.5971914125;",
        extra_boundary_conditions="axis { type empty; }",
    )
    for filename in (
        "system/controlDict",
        "0/U",
        "0/p",
        "0/k",
        "0/omega",
        "0/nut",
        "constant/transportProperties",
    ):
        path = case_dir / filename
        text = path.read_text(encoding="utf-8")
        for key, value in parameters.items():
            text = text.replace("{{" + key + "}}", str(value))
        if "{{" in text:
            raise ValueError("Unresolved template parameter in " + filename)
        path.write_text(text, encoding="utf-8")
    # Reduce the reference mesh to 48,000 cells for the eight-process example.
    mesh = (template_dir / "pipe-benchmark/blockMeshDict").read_text(encoding="utf-8")
    mesh = mesh.replace("(4000 50 1)", "(2000 24 1)")
    (case_dir / "system/blockMeshDict").write_text(mesh, encoding="utf-8")
    control = case_dir / "system/controlDict"
    text = control.read_text(encoding="utf-8")
    probes = (template_dir / "pipe-benchmark/probes").read_text(encoding="utf-8")
    head, tail = text.rsplit("}", 1)
    control.write_text(head + "\n" + probes + "\n}" + tail, encoding="utf-8")
    (case_dir / "system/decomposeParDict").write_text(
        """FoamFile { version 2.0; format ascii; class dictionary; object decomposeParDict; }
numberOfSubdomains 8;
method simple;
simpleCoeffs { n (8 1 1); delta 0.001; }
distributed no;
roots ();
""",
        encoding="ascii",
    )
    print(case_dir)


if __name__ == "__main__":
    output = Path(sys.argv[1])
    repository_template = (
        Path(__file__).resolve().parents[3]
        / "backend/agent_engine/templates/t3-incompressible-steady"
    )
    if len(sys.argv) > 2:
        make_case(output, Path(sys.argv[2]))
    elif repository_template.is_dir():
        make_case(output, repository_template)
    else:
        # A pip-installed release stores the same data inside the navifield package.
        template = resources.files("navifield").joinpath(
            "templates", "t3-incompressible-steady"
        )
        with resources.as_file(template) as template_dir:
            make_case(output, template_dir)
