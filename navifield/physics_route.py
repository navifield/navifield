"""Offline, evidence-bounded matching of explicit physics requirements."""

from __future__ import annotations

FIELDS = ("goals", "operating_conditions", "boundaries", "materials", "transfers")
GETDP = "GetDP 3.5.0 manual: https://getdp.info/doc/texinfo/getdp.pdf"
CCX = "CalculiX 2.22 manual: https://www.dhondt.de/ccx_2.22.pdf"
FOAM = "OpenCFD OpenFOAM v2306 simpleFoam documentation: https://doc.openfoam.com/2306/tools/processing/solvers/rtm/incompressible/simpleFoam/"
BUS = "examples/cli-v01/busbar/README.md"
VALVE = "examples/cli-v01/valve/README.md"
CATALOG = {
    "goals": sorted(
        {
            "current",
            "current_density",
            "joule_heat",
            "temperature",
            "thermal_stress",
            "displacement",
            "flow_rate",
            "magnetic_force",
            "equilibrium_position",
        }
    ),
    "operating_conditions": sorted(
        {"steady", "static", "fixed_gap", "axisymmetric", "transient"}
    ),
    "boundaries": sorted(
        {
            "voltage",
            "current_drive",
            "convection",
            "fixed_support",
            "pressure_inlet_outlet",
            "coil_current",
            "spring",
            "gravity",
        }
    ),
    "materials": sorted(
        {
            "constant_electrical_resistivity",
            "temperature_dependent_resistivity",
            "constant_thermal_properties",
            "linear_elastic_thermal_expansion",
            "incompressible_fluid",
            "linear_magnetic",
        }
    ),
    "transfers": sorted(
        {
            "joule_heat_to_temperature",
            "temperature_to_structure_same_mesh",
            "temperature_to_resistivity",
            "magnetic_force_to_balance",
            "fluid_force_to_balance",
            "gap_to_fields",
        }
    ),
}


def step(
    *,
    name,
    inputs,
    outputs,
    conditions,
    integration,
    workstation,
    reference,
    gaps,
    validation,
    docs,
    evidence_status,
    run_record,
):
    return {
        "step": name,
        "inputs": inputs,
        "outputs": outputs,
        "conditions": conditions,
        "evidence": {
            "solver_documentation": {"status": evidence_status[0], "sources": docs},
            "navifield_integration": {
                "status": evidence_status[1],
                "sources": integration,
            },
            "workstation_run": {
                "status": evidence_status[2],
                "sources": workstation,
                **run_record,
            },
            "target_reference": {"status": evidence_status[3], "sources": reference},
        },
        "requested_target_validated": False,
        "gaps": gaps,
        "minimum_validation": validation,
    }


def evaluate(req):
    if not isinstance(req, dict):
        raise ValueError("Requirement must be a JSON object")
    if (
        not isinstance(req.get("original_request"), str)
        or not req["original_request"].strip()
    ):
        raise ValueError("original_request must be a non-empty string")
    for key in FIELDS[:-1]:
        if (
            key not in req
            or not isinstance(req[key], list)
            or any((not isinstance(x, str) or not x.strip() for x in req[key]))
        ):
            raise ValueError(
                f"{key} must be an array of non-empty strings (empty when unknown)"
            )
    if not isinstance(req.get("transfers"), list):
        raise ValueError("transfers must be an array")
    transfer_items = req["transfers"]
    if any(
        (
            not (
                isinstance(x, str)
                and x.strip()
                or (
                    isinstance(x, dict)
                    and isinstance(x.get("source"), str)
                    and x["source"].strip()
                    and isinstance(x.get("target"), str)
                    and x["target"].strip()
                )
            )
            for x in transfer_items
        )
    ):
        raise ValueError(
            "transfer must be a nonempty token or object with source and target"
        )
    goals, ops, bc, mats = (set(req[k]) for k in FIELDS[:-1])
    transfers = {x for x in transfer_items if isinstance(x, str)}
    steps, gaps, questions = ([], [], [])
    known_goals = set(CATALOG["goals"])
    known_ops = set(CATALOG["operating_conditions"])
    known_bc = set(CATALOG["boundaries"])
    known_mats = set(CATALOG["materials"])
    known_transfers = set(CATALOG["transfers"])
    for key, values, known in zip(
        FIELDS,
        (goals, ops, bc, mats, transfers),
        (known_goals, known_ops, known_bc, known_mats, known_transfers),
    ):
        for value in sorted(values - known):
            gaps.append(
                f"{key}: {value!r} is not in the supported vocabulary; capability unknown"
            )
    if {"constant_electrical_resistivity", "temperature_dependent_resistivity"} <= mats:
        gaps.append(
            "Both resistivity relations declared: specify the material/domain for each and clarify any overlap."
        )
    if {"voltage", "current_drive"} <= bc:
        gaps.append(
            "Both voltage and current boundaries declared: specify terminal selections and check for overlap or overconstraint."
        )
    electrical = bool(
        goals & {"current", "current_density", "joule_heat"}
        and bc & {"voltage", "current_drive"}
        or transfers & {"joule_heat_to_temperature", "temperature_to_resistivity"}
    )
    if (
        goals & {"temperature", "thermal_stress", "displacement"}
        and (not electrical)
        and ("temperature_to_structure_same_mesh" not in transfers)
    ):
        questions.append(
            "Identify the heat source and temperature-to-structure transfer if applicable; a generic target does not identify its physics."
        )
    if electrical:
        if not {"voltage", "current_drive"} & bc:
            questions.append(
                "Electrical drive: fixed voltage or current, values, terminal selection and units?"
            )
        if (
            not {"constant_electrical_resistivity", "temperature_dependent_resistivity"}
            & mats
        ):
            questions.append(
                "Electrical resistivity: constant or temperature-dependent, values and applicable temperature range?"
            )
        steps.append(
            step(
                name="DC electric potential → current and volumetric Joule heat",
                inputs=[
                    "terminal voltage/current",
                    "electrical conductivity",
                    "geometry",
                ],
                outputs=["potential V", "current density A/m²", "Joule source W/m³"],
                conditions="Steady DC; example uses constant materials and prescribed voltage; other drives require adaptation.",
                integration=[BUS, "examples/cli-v01/busbar/busbar.pro"],
                workstation=[BUS + ": constant-property public example run"],
                reference=[
                    BUS
                    + ": fine mesh terminal ∫|J| differs from public reference by +6.95%; copper Joule heat +12.60%. ∫|J| and signed ∫J·n are different quantities."
                ],
                gaps=[
                    "Specify terminals, contact assumptions and material values; requested geometry is not automatically mapped."
                ],
                validation=[
                    "Check signed current conservation, electrical power vs integrated Joule heat, refine mesh and compare the defined observable."
                ],
                docs=[
                    GETDP,
                    "Gmsh 4.15.2 manual: https://gmsh.info/doc/texinfo/gmsh.html",
                ],
                evidence_status=[
                    "documented",
                    "integrated_example",
                    "tested_example",
                    "reference_compared_example",
                ],
                run_record={
                    "run_ids": ["r004", "r005", "r006"],
                    "project": "T8 public busbar project",
                    "provenance": "T8 public busbar example README; casefine gmsh.log and getdp.log",
                    "binary_versions": {
                        "Gmsh": "4.15.2 for T8 r005 only; other runs unknown",
                        "GetDP": "3.5.0 for T8 r005 only; other runs unknown",
                    },
                },
            )
        )
        if "current_density" in goals:
            questions.append(
                "Define current-density selection, spatial location or region, component/direction or magnitude, units and comparison tolerance."
            )
            electrical_step = steps[-1]
            electrical_step["evidence"]["target_reference"]["status"] = "not_available"
            electrical_step["evidence"]["target_reference"]["sources"].append(
                "The cited terminal integral is not a local current-density reference."
            )
            electrical_step["minimum_validation"].append(
                "Compare the specified local component or magnitude on the defined selection against an independent reference; check mesh sensitivity."
            )
        if transfers & {"joule_heat_to_temperature", "temperature_to_resistivity"}:
            if "joule_heat_to_temperature" not in transfers:
                questions.append(
                    "Confirm the Joule heat to temperature feedback path and thermal boundaries."
                )
            if "convection" not in bc:
                questions.append(
                    "Specify thermal boundaries, ambient temperature and heat-transfer coefficients or other heat removal."
                )
            steps.append(
                step(
                    name="Joule heat → steady temperature",
                    inputs=[
                        "volumetric heat W/m³",
                        "thermal conductivity",
                        "thermal boundaries",
                    ],
                    outputs=["temperature K"],
                    conditions="Example assumes constant thermal properties, perfect interfaces and steady heat transfer.",
                    integration=[BUS, "examples/cli-v01/busbar/busbar.pro"],
                    workstation=[BUS + ": heat balance and temperature recorded"],
                    reference=[
                        BUS
                        + ": selected temperatures compared with public reference; differences remain and mesh independence is not established."
                    ],
                    gaps=[
                        "Supply material and thermal boundary data; applicability to other geometry is unverified."
                    ],
                    validation=[
                        "Compare heat generation and heat removal; refine mesh and compare the specified temperature location."
                    ],
                    docs=[GETDP],
                    evidence_status=[
                        "documented",
                        "integrated_example",
                        "tested_example",
                        "reference_compared_example",
                    ],
                    run_record={
                        "run_ids": ["r004", "r005", "r006"],
                        "project": "T8 public busbar project",
                        "provenance": "T8 public busbar example README; casefine gmsh.log and getdp.log",
                        "binary_versions": {
                            "Gmsh": "4.15.2 for T8 r005 only; other runs unknown",
                            "GetDP": "3.5.0 for T8 r005 only; other runs unknown",
                        },
                    },
                )
            )
        if (
            "temperature_dependent_resistivity" in mats
            and "temperature_to_resistivity" not in transfers
        ):
            questions.append(
                "Is temperature prescribed externally or solved in a feedback loop? Provide its field or explicitly declare temperature_to_resistivity."
            )
        if "temperature_to_resistivity" in transfers:
            steps.append(
                step(
                    name="temperature → resistivity/conductivity → updated electric and thermal fields",
                    inputs=["temperature K", "ρe(T) Ω·m", "electrical drive"],
                    outputs=[
                        "updated σ(T)=1/ρe(T)",
                        "updated potential and Joule heat",
                    ],
                    conditions="Requires explicit feedback convergence; fixed-current and fixed-voltage response differ.",
                    integration=[
                        "No existing feedback deck or loop established for this example."
                    ],
                    workstation=["Unknown: no feedback run established."],
                    reference=["Unknown: no feedback target reference established."],
                    gaps=[
                        "Obtain ρe(T), reference temperature, valid range and units; determine drive and convergence criteria; verify solver formulation and implement/test feedback."
                    ],
                    validation=[
                        "Recover constant-property limit, check conservation and convergence, then compare a defined feedback benchmark under the same drive."
                    ],
                    docs=[
                        GETDP
                        + ": general nonlinear formulation support; this does not verify a particular feedback deck."
                    ],
                    evidence_status=[
                        "documented",
                        "unknown",
                        "unknown",
                        "unknown",
                    ],
                    run_record={},
                )
            )
    if "temperature_to_structure_same_mesh" in transfers:
        if "fixed_support" not in bc:
            questions.append(
                "Specify mechanical constraints and reference temperature, including rigid-body restraint."
            )
        if "linear_elastic_thermal_expansion" not in mats:
            questions.append(
                "Specify elastic modulus, Poisson ratio and thermal expansion versus temperature."
            )
        steps.append(
            step(
                name="same-mesh temperature transfer",
                inputs=["GetDP nodal temperature K and mesh node IDs"],
                outputs=["CalculiX nodal temperature K"],
                conditions="Only matching mesh node IDs are evidenced.",
                integration=[BUS, "examples/cli-v01/busbar/structure.py"],
                workstation=[BUS + ": zero missing nodes, 0 K maximum readback error"],
                reference=[
                    "Unknown: field transfer is not validation of the requested target."
                ],
                gaps=["Different mesh mapping is unverified."],
                validation=["Check node ID coverage and temperature readback."],
                docs=[CCX],
                evidence_status=[
                    "documented",
                    "integrated_example",
                    "tested_example",
                    "not_available",
                ],
                run_record={
                    "run_ids": ["r001", "r002", "r003"],
                    "project": "T8b public busbar structural project",
                    "provenance": "T8b public busbar example README; coarse/fine and rod logs",
                    "binary_versions": {
                        "CalculiX": "2.22 for T8b r002 only; other runs unknown",
                        "Gmsh": "4.15.2 for T8b r002 only; other runs unknown",
                        "GetDP": "3.5.0 for T8b r002 only; other runs unknown",
                    },
                },
            )
        )
        steps.append(
            step(
                name="same-mesh nodal temperature → static thermal deformation and stress",
                inputs=[
                    "nodal temperature K",
                    "reference temperature",
                    "elastic properties and constraints",
                ],
                outputs=["displacement m", "stress Pa"],
                conditions="Small-deformation linear elastic example; shared node identifiers; no general different-mesh mapping.",
                integration=[BUS, "examples/cli-v01/busbar/structure.py"],
                workstation=[
                    BUS
                    + ": nodal transfer readback had zero missing nodes and 0 K maximum error; two meshes and rod checks run"
                ],
                reference=[
                    BUS
                    + ": rod checks agree with αΔTL and −EαΔT to reported precision; no official busbar stress/displacement reference; peak integration-point stress changed −30.73% on refinement."
                ],
                gaps=[
                    "Requested constraints and material behavior need explicit definition; different mesh transfer is unverified."
                ],
                validation=[
                    "Verify node IDs and temperature readback, free/fixed heated rod, reaction balance and stress mesh sensitivity."
                ],
                docs=[CCX],
                evidence_status=[
                    "documented",
                    "integrated_example",
                    "tested_example",
                    "not_available",
                ],
                run_record={
                    "run_ids": ["r001", "r002", "r003"],
                    "project": "T8b public busbar structural project",
                    "provenance": "T8b public busbar example README; coarse/fine and rod logs",
                    "binary_versions": {
                        "CalculiX": "2.22 for T8b r002 only; other runs unknown",
                        "Gmsh": "4.15.2 for T8b r002 only; other runs unknown",
                        "GetDP": "3.5.0 for T8b r002 only; other runs unknown",
                    },
                },
            )
        )
    valve_context = "fixed_gap" in ops or bool(
        transfers
        & {"gap_to_fields", "fluid_force_to_balance", "magnetic_force_to_balance"}
    )
    if "gap_to_fields" in transfers:
        steps.append(
            step(
                name="gap to fixed-gap field geometries",
                inputs=["gap m", "geometry and moving boundaries"],
                outputs=["fluid and magnetic fixed-gap geometries"],
                conditions="Public axisymmetric valve example only; not dynamic mesh coupling.",
                integration=[
                    VALVE,
                    "tools/valve_reference/fluid_geometry.py",
                    "tools/valve_reference/morph_magnet_mesh.py",
                ],
                workstation=[
                    VALVE + ": fixed-gap field evaluations in five-point example"
                ],
                reference=[VALVE + ": no whole-valve experiment"],
                gaps=["Verify gap definition, moving boundaries and mesh quality."],
                validation=[
                    "Read back the gap in both domains and check mesh quality at extreme positions."
                ],
                docs=[GETDP, FOAM],
                evidence_status=[
                    "documented",
                    "integrated_example",
                    "tested_example",
                    "not_available",
                ],
                run_record={
                    "run_ids": [],
                    "run_identifier": "case/cases/full/result.json and commands.json; CLI run ID not established",
                    "provenance": "examples/cli-v01/valve/README.md",
                    "binary_versions": {
                        "OpenFOAM": "v2606 per toolchain record, exact run binary needs verification",
                        "Gmsh": "unknown for this run",
                        "GetDP": "unknown for this run",
                    },
                },
            )
        )
    if "flow_rate" in goals and (not valve_context):
        questions.append(
            "Specify fluid domain, flow regime, driving boundaries and whether a fixed valve gap is involved."
        )
    if valve_context and (
        "flow_rate" in goals or "fluid_force_to_balance" in transfers
    ):
        steps.append(
            step(
                name="steady fluid flow and force at a fixed gap",
                inputs=[
                    "pressure boundaries",
                    "fluid properties",
                    "gap and surface groups",
                ],
                outputs=["flow rate", "fluid force"],
                conditions="Public example uses fixed gap, axisymmetry and steady incompressible flow; other configurations need review.",
                integration=[
                    VALVE,
                    "tools/valve_reference/fluid_case.py",
                    "tools/valve_reference/fluid_forces.py",
                ],
                workstation=[VALVE + ": five-point example"],
                reference=[VALVE + ": no whole-valve experiment"],
                gaps=[
                    "Confirm pressure, geometry, wetted force surfaces and fluid properties."
                ],
                validation=[
                    "Check boundary selections, force sign, flow conservation and grid sensitivity."
                ],
                docs=[FOAM],
                evidence_status=[
                    "documented",
                    "integrated_example",
                    "tested_example",
                    "not_available",
                ],
                run_record={
                    "run_ids": [],
                    "run_identifier": "case/cases/full/result.json and commands.json; CLI run ID not established",
                    "provenance": "examples/cli-v01/valve/README.md",
                    "binary_versions": {
                        "OpenFOAM": "v2606 per toolchain record, exact run binary needs verification",
                        "Gmsh": "unknown for this run",
                        "GetDP": "unknown for this run",
                    },
                },
            )
        )
    if "magnetic_force" in goals and (not valve_context):
        questions.append(
            "Specify magnetic geometry, excitation and whether a fixed valve gap is involved."
        )
    if valve_context and (
        "magnetic_force" in goals or "magnetic_force_to_balance" in transfers
    ):
        steps.append(
            step(
                name="magnetic force at a fixed gap",
                inputs=["coil current", "magnetic properties", "gap"],
                outputs=["magnetic force"],
                conditions="Example is linear axisymmetric magnetostatics at fixed gap.",
                integration=[VALVE, "tools/valve_reference/fixed_flux_force.py"],
                workstation=[VALVE + ": five-point example"],
                reference=[VALVE + ": no whole-valve experiment"],
                gaps=["Define coil, material, magnetic boundary and force surface."],
                validation=[
                    "Verify force direction and mesh sensitivity against a defined reference."
                ],
                docs=[GETDP],
                evidence_status=[
                    "documented",
                    "integrated_example",
                    "tested_example",
                    "not_available",
                ],
                run_record={
                    "run_ids": [],
                    "run_identifier": "case/cases/full/result.json and commands.json; CLI run ID not established",
                    "provenance": "examples/cli-v01/valve/README.md",
                    "binary_versions": {
                        "OpenFOAM": "v2606 per toolchain record, exact run binary needs verification",
                        "Gmsh": "unknown for this run",
                        "GetDP": "unknown for this run",
                    },
                },
            )
        )
    if "equilibrium_position" in goals and (
        not transfers & {"fluid_force_to_balance", "magnetic_force_to_balance"}
    ):
        questions.append(
            "Specify the force contributors and their transfer to the equilibrium calculation."
        )
    if transfers & {"fluid_force_to_balance", "magnetic_force_to_balance"}:
        steps.append(
            step(
                name="static force balance",
                inputs=["fluid and magnetic force versus gap", "spring and gravity"],
                outputs=["equilibrium gap"],
                conditions="Fixed-gap evaluations, not a transient or simultaneous two-way dynamic solution.",
                integration=[VALVE, "tools/valve_reference/balance.py"],
                workstation=[VALVE + ": five-point direct force-balance checks"],
                reference=[VALVE + ": no whole-valve experiment"],
                gaps=["Specify force contributors, directions, spring and gap range."],
                validation=[
                    "Directly recompute the proposed root and examine gap and mesh sensitivity."
                ],
                docs=[GETDP, FOAM],
                evidence_status=[
                    "documented",
                    "integrated_example",
                    "tested_example",
                    "not_available",
                ],
                run_record={
                    "run_ids": [],
                    "run_identifier": "case/cases/full/result.json and commands.json; CLI run ID not established",
                    "provenance": "examples/cli-v01/valve/README.md",
                    "binary_versions": {
                        "OpenFOAM": "v2606 per toolchain record, exact run binary needs verification",
                        "Gmsh": "unknown for this run",
                        "GetDP": "unknown for this run",
                    },
                },
            )
        )
    for transfer in transfer_items:
        if isinstance(transfer, dict):
            source, target = (transfer["source"], transfer["target"])
            steps.append(
                step(
                    name="unmatched transfer: " + source + " -> " + target,
                    inputs=[source],
                    outputs=[target],
                    conditions="Unmatched directed interface; mechanism and mapping unknown. Downstream execution depends on implementing and checking this relation.",
                    integration=["No integration established for this interface."],
                    workstation=["Unknown: not run."],
                    reference=["Unknown: not compared."],
                    gaps=[
                        "Define quantity, units, source/target domains, mapping, timing and coupling direction; identify or implement an interface.",
                        "Downstream must support this relation through its boundary condition or material law, with readback before executing a dependent template.",
                    ],
                    validation=[
                        "Check units, mapping conservation/readback, and a simple known-value transfer."
                    ],
                    docs=["Unknown: applicable solver capability not established."],
                    evidence_status=["unknown", "unknown", "unknown", "unknown"],
                    run_record={},
                )
            )
    # Place each unmatched interface before the first candidate that consumes its
    # target. Detect candidate sources conservatively, without inferring a mapping.
    unmatched = [
        item for item in steps if item["step"].startswith("unmatched transfer: ")
    ]
    for item in unmatched:
        steps.remove(item)
    for item in unmatched:
        source = item["inputs"][0].strip().lower()
        target = item["outputs"][0].strip().lower()
        producers = [
            candidate
            for candidate in steps
            if any(
                source == output.lower() or output.lower().startswith(source + " ")
                for output in candidate["outputs"]
            )
        ]
        if not producers:
            item["gaps"].append(
                "No upstream producer was matched for "
                + source
                + "; supply verified input data or implement and validate an upstream solve."
            )
        else:
            item["gaps"].append(
                "A candidate upstream output was identified; verify its definition, domain and units before transfer."
            )
        item["match_status"] = "unmatched"
        consumers = [
            i
            for i, candidate in enumerate(steps)
            if any(
                target == value.lower() or target in value.lower()
                for value in candidate["inputs"]
            )
        ]
        position = min(consumers) if consumers else 0
        if producers and steps.index(producers[0]) >= position:
            item["gaps"].append(
                "The candidate producer is not before the dependent step; resolve ordering or coupling iteration before execution."
            )
        steps.insert(position, item)
    if not ops or "transient" in ops or (not ops & {"steady", "static"}):
        gaps.append(
            "Steady/static applicability is unspecified or contradicted: candidate steps cannot be treated as an applicable route."
        )
    if {"steady", "transient"} <= ops or {"static", "transient"} <= ops:
        gaps.append(
            "Potentially conflicting operating conditions: specify which stage and time behavior each applies to."
        )
    if "transient" in ops:
        gaps.append(
            "Transient behavior is outside the evidenced static routes; solver and implementation unknown."
        )
    if "temperature_to_structure_same_mesh" in transfers and (not electrical):
        gaps.append(
            "Temperature-to-structure requested, but the source temperature field and its provenance are not established."
        )
    for token in sorted(transfers - known_transfers):
        gaps.append(
            "Unknown directed transfer "
            + repr(token)
            + ": supply {source,target}, quantity, units and mapping; verify a known-value readback."
        )
    for item in steps:
        item["applicability"] = (
            "blocked"
            if item.get("match_status") == "unmatched"
            or not ops
            or bool(ops - known_ops)
            or "transient" in ops
            or (not ops & {"steady", "static"})
            else "candidate_requires_review"
        )
    if not steps:
        gaps.append(
            "No supported physical route identified; supply explicit goals and transfers. No solver selected."
        )
    gaps.append(
        "Values, units, geometry, boundary selections, tolerances and executable versions must be checked against the actual request; a route is not an engineering validation."
    )
    capability_gaps = []
    if unmatched or transfers - known_transfers:
        capability_gaps.append("unmatched_interface")
    if "temperature_to_resistivity" in transfers:
        capability_gaps.append("feedback_not_integrated")
    unmatched_goals = sorted(goals - known_goals)
    unsupported_condition = (
        not ops
        or bool(ops - known_ops)
        or "transient" in ops
        or not ops & {"steady", "static"}
    )
    candidate_steps = [
        item for item in steps if item.get("match_status") != "unmatched"
    ]
    limitations = []
    if not candidate_steps:
        limitations.append("no_candidate")
    if unsupported_condition:
        limitations.append("unsupported_condition")
    if unmatched_goals:
        limitations.append("unmatched_goal")
    if capability_gaps:
        readiness_status = "blocked_missing_capability"
        explanation = "Implement and verify the missing interfaces or feedback before running dependent templates."
    elif not candidate_steps:
        readiness_status = "no_candidate"
        explanation = "No applicable candidate step was identified."
    elif unsupported_condition:
        readiness_status = "unsupported_condition"
        explanation = "The declared time conditions are unsupported or insufficient to establish applicability."
    elif unmatched_goals:
        readiness_status = "unmatched_goal"
        explanation = "Some requested goals are not covered by the candidate steps."
    else:
        readiness_status = "candidate_requires_review"
        explanation = "Candidates require case-specific setup, physical review and confirmation; no executable setup is produced."
    route_readiness = {
        "status": readiness_status,
        "directly_executable": False,
        "capability_gaps": capability_gaps,
        "limitations": limitations,
        "unmatched_goals": unmatched_goals,
        "explanation": explanation,
    }
    return {
        "status": "needs_review",
        "requirement": req,
        "route_readiness": route_readiness,
        "assumptions": [
            "No undeclared material, boundary, geometry or physical feedback is assumed."
        ],
        "steps": steps,
        "gaps": gaps,
        "questions": questions,
    }
