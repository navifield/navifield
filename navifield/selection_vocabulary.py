"""Enumerate the public engineering vocabulary used in selection research packets."""

from . import target_selection

GENERAL = set(
    """engineering_targets heat_transfer phase_change turbulence fluid_mechanics solid_mechanics
radiation contact_resistance fouling non_newtonian shear_thinning thermal_contact contact plasticity creep
solidification boiling condensation natural_convection forced_convection thermal_radiation viscous_heating
compressible transition turbulent laminar steady transient convection conduction elasticity thermal_expansion
friction mechanical_loading transient_storage cyclic_loading fatigue two_phase compressibility
heat_rate thermal_energy pressure_reference heat_basis wall_readout stress_measure elevation_change
local_loss_coefficients pressure_boundary gravity porous_resistance thermal_contact temperature_field
velocity_field interface_temperature normal_heat_flux K Pa W J W/m2 m/s solid_volume fluid_volume interface_surface
domain flow_phase compressibility thermal_mode operation pressure_definition mechanics phase_change coupling
source target select-models select-chains flow_domain interface_model""".split()
)


def schema(db):
    cards = [
        r
        for row in target_selection.inventory(db)
        for r in [row, *row.get("variants", [])]
    ]
    dimensions = {}
    for card in cards:
        for k, values in card["conditions"].items():
            dimensions.setdefault(k, set()).update(values)
    import json

    readouts = [
        json.loads(body) for (body,) in db.execute("SELECT body FROM target_readouts")
    ]
    return dict(
        domain="engineering_targets",
        goals=sorted({card["target"] for card in cards} | set(target_selection.TARGETS)),
        conditions={k: sorted(v) for k, v in sorted(dimensions.items())},
        input_keys=sorted(
            set(target_selection.QUESTIONS)
            | {k for goal in target_selection.DEFINITIONS.values() for k in goal}
            | {k for c in cards for k in c["inputs"]}
            | {i["key"] for record in readouts for i in record["inputs"]}
        ),
        mechanisms=sorted({k for c in cards for k in c["mechanisms"]}),
        transfers=[
            dict(source_goal=a, target_goal=b, quantity=q, unit=u, location=l)
            for (a, b, q), (u, l) in target_selection.PORTS.items()
        ],
    )


def public_tokens(db):
    data = schema(db)
    public = (
        GENERAL
        | set(data["goals"])
        | set(data["input_keys"])
        | set(data["mechanisms"])
        | set(data["conditions"])
    )
    for values in data["conditions"].values():
        public.update(values)
    for link in data["transfers"]:
        public.update(link.values())
    for stmt in (
        "SELECT solver,version FROM manual_baselines",
        "SELECT domain,value FROM model_facets",
        "SELECT domain,name FROM model_inputs",
        "SELECT domain,goal,chain FROM chain_goals",
    ):
        for row in db.execute(stmt):
            public.update(row)
    return public
