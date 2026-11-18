"""Read actual pipe monitor tables; pressure is kinematic and multiplied by rho."""

import math
import re
import statistics
import sys
from pathlib import Path

case_dir = Path(sys.argv[1])
postprocessing_dir = case_dir / "postProcessing"


def read_table(name, field, vector=False, is_yplus=False):
    files = sorted((postprocessing_dir / name).glob("*/" + field))
    if not files:
        raise ValueError("Monitor table unavailable: " + name + "/" + field)
    result = {}
    for table_path in files:
        for line in table_path.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = line.replace("(", " ").replace(")", " ").split()
            if is_yplus:
                parts = [parts[0]] + parts[2:]
            try:
                sample_time = float(parts[0])
            except (ValueError, IndexError) as error:
                raise ValueError("Invalid monitor time") from error
            if not math.isfinite(sample_time):
                raise ValueError("Invalid monitor time")
            try:
                result[sample_time] = list(map(float, parts[1:]))
            except ValueError:
                result[sample_time] = None
    return result, files[0]


pressure_table, _ = read_table("pipePressure", "p")
velocity_table, _ = read_table("pipePressure", "U")
turbulence_table, _ = read_table("pipePressure", "k")
inlet_flow, infile = read_table("inletFlux", "surfaceFieldValue.dat")
outlet_flow, _ = read_table("outletFlux", "surfaceFieldValue.dat")
yplus, _ = read_table("wallYPlus", "yPlus.dat", is_yplus=True)
tables = (
    pressure_table,
    velocity_table,
    turbulence_table,
    inlet_flow,
    outlet_flow,
    yplus,
)
times = sorted(set().union(*tables))[-10:]
if len(times) != 10 or any(
    sample_time not in table_data for table_data in tables for sample_time in times
):
    raise ValueError("Final monitor window is incomplete")
for table_data, minimum_values in (
    (pressure_table, 7),
    (velocity_table, 21),
    (turbulence_table, 7),
    (inlet_flow, 1),
    (outlet_flow, 1),
    (yplus, 3),
):
    for sample_time in times:
        row = table_data[sample_time]
        if (
            row is None
            or len(row) < minimum_values
            or any(not math.isfinite(value) or abs(value) >= 1e100 for value in row)
        ):
            raise ValueError(
                "Invalid or incomplete value in the final common monitor window"
            )
area_match = re.search(
    r"^#\s*Area\s*:\s*([0-9.eE+-]+)", infile.read_text(encoding="utf-8"), re.M
)
if not area_match:
    raise ValueError("Actual inlet area unavailable")
inlet_area = float(area_match.group(1))
average = statistics.fmean
if not math.isfinite(inlet_area) or inlet_area <= 0:
    raise ValueError("Invalid actual inlet area")
bulk_velocity = (
    abs(average(inlet_flow[sample_time][0] for sample_time in times)) / inlet_area
)
reynolds = bulk_velocity * 0.1 / 1e-6
if not 4000 <= reynolds <= 100000:
    raise ValueError("Reynolds number outside correlation range")
# OpenFOAM p is kinematic pressure; density 1000 kg/m³ converts it to Pa.
pressure_differences = [
    1000 * (pressure_table[sample_time][1] - pressure_table[sample_time][2])
    for sample_time in times
]
reference_pressure_drop = (
    0.316 * reynolds ** (-0.25) * (2 / 0.1) * 1000 * bulk_velocity**2 / 2
)
upstream_pressure_drop = average(
    1000 * (pressure_table[sample_time][0] - pressure_table[sample_time][1])
    for sample_time in times
)
mean_pressure_drop = average(pressure_differences)
radial_pressure_spread = max(
    abs(
        pressure_table[sample_time][first_probe]
        - pressure_table[sample_time][second_probe]
    )
    * 1000
    / abs(mean_pressure_drop)
    for sample_time in times
    for first_probe, second_probe in ((1, 3), (1, 4), (2, 5), (2, 6))
)
velocity_profile_difference = max(
    abs(
        velocity_table[sample_time][3 * first_probe]
        - velocity_table[sample_time][3 * second_probe]
    )
    / bulk_velocity
    for sample_time in times
    for first_probe, second_probe in ((1, 2), (3, 5), (4, 6))
)
mass_flow_imbalance = max(
    abs(inlet_flow[sample_time][0] + outlet_flow[sample_time][0])
    / max(abs(inlet_flow[sample_time][0]), abs(outlet_flow[sample_time][0]))
    for sample_time in times
)
window_spread = (max(pressure_differences) - min(pressure_differences)) / abs(
    mean_pressure_drop
)
half_window_difference = abs(
    average(pressure_differences[:5]) - average(pressure_differences[5:])
) / abs(mean_pressure_drop)
print(
    f"window={times[0]:g}..{times[-1]:g}; samples=10; area_sector_m2={inlet_area:.10g}; area_72_m2={72 * inlet_area:.10g}; Ub_m_s={bulk_velocity:.9g}; Re={reynolds:.9g}"
)
error_percent = (
    100 * (mean_pressure_drop - reference_pressure_drop) / reference_pressure_drop
)
print(
    f"dp_7_9_Pa={mean_pressure_drop:.9g}; reference_Pa={reference_pressure_drop:.9g}; signed_error_percent={error_percent:.6g}; limit_percent=5"
)
print(
    f"pressure_window_spread={window_spread:.6g}; half_window_difference={half_window_difference:.6g}; upstream_gradient_difference={abs(upstream_pressure_drop - mean_pressure_drop) / abs(mean_pressure_drop):.6g}; radial_pressure_spread={radial_pressure_spread:.6g}; velocity_profile_difference={velocity_profile_difference:.6g}; mass_flux_imbalance={mass_flow_imbalance:.6g}; wall_yplus_max={max(yplus[sample_time][1] for sample_time in times):.6g}"
)
print(
    "Inspect final wall yPlus between x=7 and 9 m and mesh quality; one mesh is not a grid convergence study."
)
sys.exit(0 if abs(error_percent) <= 5 else 1)
