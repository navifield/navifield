"""Create a public Re=1000 lid-driven cavity OpenFOAM case."""

from pathlib import Path
import sys

case_dir = Path(sys.argv[1])
case_dir.mkdir(parents=True, exist_ok=False)
# Unit square, lid speed 1 m/s and nu=0.001 m²/s give Re=1000.
for directory in ("0", "constant", "system"):
    (case_dir / directory).mkdir()


def foam_header(object_class, object_name):
    return (
        f"FoamFile {{ version 2.0; format ascii; "
        f"class {object_class}; object {object_name}; }}\n"
    )


def write_file(path, text):
    (case_dir / path).write_text(text, encoding="ascii")


write_file(
    "system/blockMeshDict",
    foam_header("dictionary", "blockMeshDict")
    + """scale 1;
vertices ((0 0 0) (1 0 0) (1 1 0) (0 1 0) (0 0 0.01) (1 0 0.01) (1 1 0.01) (0 1 0.01));
blocks (hex (0 1 2 3 4 5 6 7) (80 80 1) simpleGrading (1 1 1));
edges ();
boundary (
 movingWall { type wall; faces ((3 7 6 2)); }
 fixedWalls { type wall; faces ((0 4 7 3) (1 2 6 5) (0 1 5 4)); }
 frontBack { type empty; faces ((0 3 2 1) (4 5 6 7)); }
);
mergePatchPairs ();
""",
)
write_file(
    "0/U",
    foam_header("volVectorField", "U")
    + """dimensions [0 1 -1 0 0 0 0];
internalField uniform (0 0 0);
boundaryField {
 movingWall { type fixedValue; value uniform (1 0 0); }
 fixedWalls { type noSlip; }
 frontBack { type empty; }
}
""",
)
write_file(
    "0/p",
    foam_header("volScalarField", "p")
    + """dimensions [0 2 -2 0 0 0 0];
internalField uniform 0;
boundaryField {
 movingWall { type zeroGradient; }
 fixedWalls { type zeroGradient; }
 frontBack { type empty; }
}
""",
)
write_file(
    "constant/transportProperties",
    foam_header("dictionary", "transportProperties")
    + "transportModel Newtonian;\nnu [0 2 -1 0 0 0 0] 0.001;\n",
)
write_file(
    "constant/turbulenceProperties",
    foam_header("dictionary", "turbulenceProperties") + "simulationType laminar;\n",
)
write_file(
    "system/fvSchemes",
    foam_header("dictionary", "fvSchemes")
    + """ddtSchemes { default steadyState; }
gradSchemes { default Gauss linear; }
divSchemes { default none; div(phi,U) Gauss linearUpwind grad(U); div((nuEff*dev2(T(grad(U))))) Gauss linear; }
laplacianSchemes { default Gauss linear corrected; }
interpolationSchemes { default linear; }
snGradSchemes { default corrected; }
""",
)
write_file(
    "system/fvSolution",
    foam_header("dictionary", "fvSolution")
    + """solvers {
 p { solver GAMG; tolerance 1e-8; relTol 0.01; smoother GaussSeidel; }
 U { solver smoothSolver; smoother symGaussSeidel; tolerance 1e-8; relTol 0.01; }
}
SIMPLE { nNonOrthogonalCorrectors 0; pRefCell 0; pRefValue 0; residualControl { p 1e-8; U 1e-8; } }
relaxationFactors { fields { p 0.3; } equations { U 0.7; } }
""",
)
vertical_positions = [
    0.9766,
    0.9688,
    0.9609,
    0.9531,
    0.8516,
    0.7344,
    0.6172,
    0.5,
    0.4531,
    0.2813,
    0.1719,
    0.1016,
    0.0703,
    0.0625,
    0.0547,
]
horizontal_positions = [
    0.9688,
    0.9609,
    0.9531,
    0.9453,
    0.9063,
    0.8594,
    0.8047,
    0.5,
    0.2344,
    0.2266,
    0.1563,
    0.0938,
    0.0781,
    0.0703,
    0.0625,
]
probe_positions = "\n".join(
    [f"(0.5 {vertical_position} 0.005)" for vertical_position in vertical_positions]
    + [
        f"({horizontal_position} 0.5 0.005)"
        for horizontal_position in horizontal_positions
    ]
)
write_file(
    "system/controlDict",
    foam_header("dictionary", "controlDict")
    + f"""application simpleFoam;
startFrom startTime; startTime 0; stopAt endTime; endTime 6000; deltaT 1;
writeControl timeStep; writeInterval 500; purgeWrite 0; writeFormat ascii; writePrecision 12;
runTimeModifiable false;
functions {{ cavityProbes {{ type probes; libs (sampling); fields (U); interpolationScheme cellPoint;
 writeControl timeStep; writeInterval 50; probeLocations (\n{probe_positions}\n); }} }}
""",
)
print(case_dir)
