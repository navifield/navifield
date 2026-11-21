"""Steady isotropic conduction in a straight circular fin with a uniform side film.

The generated CalculiX deck uses metres, watts, and degrees Celsius. Its base
has a prescribed temperature and its tip is adiabatic. Readback is scoped to
decks produced by this module.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from numbers import Real
from pathlib import Path
from typing import Any

import numpy as np

_MODEL_PREFIX = "** NAVIFIELD CIRCULAR FIN V1 "
_MAX_ELEMENTS = 500_000
_GAUSS = (-1.0 / math.sqrt(3), 1.0 / math.sqrt(3))
_CUBE = np.array([
    [-1, -1, -1], [1, -1, -1], [1, 1, -1], [-1, 1, -1],
    [-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1],
])
_TABLE_HEADER = re.compile(
    r"^\s*(total heat generation|heat generation|temperatures)"
    r"\s*(?:\([^)]*\))?\s+for set\s+(\w+)\s+and time\s+(\S+)\s*$",
    re.IGNORECASE,
)
_REQUIRED_TABLES = {
    ("temperatures", "ALLNODES"),
    ("heat generation", "BASE"),
    ("total heat generation", "BASE"),
}


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise ValueError(f"{field} must be a finite number")
    return float(value)


def _subdivisions(value: Any, field: str, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


@dataclass(frozen=True)
class _Model:
    radius_m: float
    length_m: float
    conductivity_w_m_k: float
    base_temperature_c: float
    ambient_temperature_c: float
    film_coefficient_w_m2_k: float
    cross_section_divisions: int
    axial_divisions: int
    axial_breakpoints_m: tuple[float, ...]

    @classmethod
    def parse(cls, values: Mapping[str, Any]) -> _Model:
        fields = (
            "radius_m", "length_m", "conductivity_w_m_k", "base_temperature_c",
            "ambient_temperature_c", "film_coefficient_w_m2_k",
        )
        required = {*fields, "cross_section_divisions", "axial_divisions"}
        missing = required.difference(values)
        extra = set(values).difference(required, {"axial_breakpoints_m"})
        if missing or extra:
            raise ValueError(f"Fin model fields missing={sorted(missing)}, unsupported={sorted(extra)}")
        physical = [_number(values[field], field) for field in fields]
        radius, length, conductivity, base, ambient, film = physical
        if min(radius, length, conductivity, film) <= 0:
            raise ValueError("Radius, length, conductivity and film coefficient must be positive")
        if min(base, ambient) < -273.15:
            raise ValueError("Prescribed temperatures must be at or above absolute zero")
        cross = _subdivisions(values["cross_section_divisions"], "cross_section_divisions", 2)
        axial = _subdivisions(values["axial_divisions"], "axial_divisions", 1)
        raw = values.get("axial_breakpoints_m", ())
        if not isinstance(raw, (list, tuple)):
            raise ValueError("axial_breakpoints_m must be a list of increasing internal distances")
        anchors = (0.0, *(_number(value, "axial_breakpoints_m") for value in raw), length)
        if any(hi <= lo for lo, hi in zip(anchors, anchors[1:])):
            raise ValueError("axial_breakpoints_m must increase strictly between zero and length_m")
        if cross * cross * axial * (len(anchors) - 1) > _MAX_ELEMENTS:
            raise ValueError(f"Fin mesh exceeds {_MAX_ELEMENTS:,} elements")
        return cls(*physical, cross, axial, anchors[1:-1])


def _solver_float(value: float) -> str:
    # CalculiX input fields are at most 20 characters. 14 significant digits
    # also preserve the discretization written by the original brass-fin study.
    return f"{value:.14g}"


@dataclass(frozen=True)
class _Mesh:
    xy: np.ndarray
    z: np.ndarray
    quads: np.ndarray
    side_faces: list[tuple[int, int]]
    perimeter_edges: np.ndarray

    @classmethod
    def generate(cls, model: _Model) -> _Mesh:
        n = model.cross_section_divisions
        width = n + 1
        anchors = (0.0, *model.axial_breakpoints_m, model.length_m)
        z = [0.0]
        for lo, hi in zip(anchors, anchors[1:]):
            z.extend(lo + (hi - lo) * i / model.axial_divisions for i in range(1, model.axial_divisions))
            z.append(hi)
        # The readback integrates the same rounded coordinates written to the solver.
        levels = np.array([float(_solver_float(value)) for value in z])
        if not np.isfinite(levels).all() or np.any(np.diff(levels) <= 0):
            raise ValueError("Axial mesh contains unresolved or non-finite coordinates")
        xy = []
        for j in range(width):
            v = -1 + 2 * j / n
            for i in range(width):
                u = -1 + 2 * i / n
                x = model.radius_m * u * math.sqrt(1 - v * v / 2)
                y = model.radius_m * v * math.sqrt(1 - u * u / 2)
                xy.append((float(_solver_float(x)), float(_solver_float(y))))
        quads = []
        boundary = []
        edges = []
        for j in range(n):
            for i in range(n):
                start = j * width + i
                quad = (start, start + 1, start + width + 1, start + width)
                quads.append(quad)
                for on_boundary, edge in ((j == 0, 0), (i == n - 1, 1), (j == n - 1, 2), (i == 0, 3)):
                    if on_boundary:
                        boundary.append((len(quads), edge + 3))
                        edges.append((quad[edge], quad[(edge + 1) % 4]))
        return cls(np.array(xy), levels, np.array(quads), boundary, np.array(edges))

    def measurements(self) -> tuple[np.ndarray, np.ndarray]:
        edge_lengths = np.linalg.norm(self.xy[self.perimeter_edges[:, 1]] - self.xy[self.perimeter_edges[:, 0]], axis=1)
        edge_weights = np.zeros(len(self.xy))
        np.add.at(edge_weights, self.perimeter_edges.ravel(), np.repeat(edge_lengths / 2, 2))
        xy = self.xy[self.quads]
        nodal_areas = np.zeros(self.quads.shape)
        for gauss in itertools.product(_GAUSS, repeat=2):
            shape, derivative = _shape(_CUBE[:4, :2], gauss)
            jacobian = np.einsum("end,nj->edj", xy, derivative)
            det = np.linalg.det(jacobian)
            if not np.isfinite(det).all() or np.any(det <= 0):
                raise ValueError("Fin mesh has a collapsed or inverted cross-section element")
            nodal_areas += det[:, None] * shape
        area_weights = np.zeros(len(self.xy))
        np.add.at(area_weights, self.quads.ravel(), nodal_areas.ravel())
        return edge_weights, area_weights


def _shape(corners: np.ndarray, point: Sequence[float]) -> tuple[np.ndarray, np.ndarray]:
    factors = 1 + corners * point
    scale = 2 ** len(point)
    return factors.prod(axis=1) / scale, np.column_stack([
        corners[:, axis] * np.delete(factors, axis, axis=1).prod(axis=1) / scale
        for axis in range(len(point))
    ])


def _deck(model: _Model, mesh: _Mesh) -> str:
    nodes_per_plane = len(mesh.xy)
    elements_per_layer = len(mesh.quads)
    lines = [
        _MODEL_PREFIX + json.dumps(vars(model), separators=(",", ":")),
        "*HEADING", "Steady circular fin in SI units; temperatures in degrees Celsius",
        "*NODE",
    ]
    for level, z in enumerate(mesh.z):
        for index, (x, y) in enumerate(mesh.xy, 1):
            point = ",".join(map(_solver_float, (x, y, z)))
            lines.append(f"{level * nodes_per_plane + index},{point}")
    lines.append("*ELEMENT, TYPE=DC3D8, ELSET=SOLID")
    for layer in range(len(mesh.z) - 1):
        node_offset = layer * nodes_per_plane + 1
        first_eid = layer * elements_per_layer + 1
        for eid, quad in enumerate(mesh.quads, first_eid):
            bottom = quad + node_offset
            top = bottom + nodes_per_plane
            lines.append(",".join(map(str, (eid, *bottom, *top))))
    lines.extend([
        "*NSET, NSET=ALLNODES, GENERATE", f"1,{nodes_per_plane * len(mesh.z)},1",
        "*NSET, NSET=BASE, GENERATE", f"1,{nodes_per_plane},1",
        "*MATERIAL, NAME=FIN", "*CONDUCTIVITY", _solver_float(model.conductivity_w_m_k),
        "*SOLID SECTION, ELSET=SOLID, MATERIAL=FIN",
        "*STEP", "*HEAT TRANSFER, STEADY STATE", "*BOUNDARY",
        f"BASE,11,11,{_solver_float(model.base_temperature_c)}", "*FILM",
    ])
    ambient = _solver_float(model.ambient_temperature_c)
    film = _solver_float(model.film_coefficient_w_m2_k)
    for layer in range(len(mesh.z) - 1):
        lines.extend(f"{layer * elements_per_layer + eid},F{side},{ambient},{film}"
                     for eid, side in mesh.side_faces)
    lines.extend([
        "*NODE FILE", "NT",
        "*NODE PRINT, NSET=ALLNODES", "NT",
        "*NODE PRINT, NSET=BASE, TOTALS=YES", "RFL", "*END STEP",
    ])
    return "\n".join(lines) + "\n"


def build_fin_input(model: Mapping[str, Any], path: Path) -> dict[str, Any]:
    """Write the supported CalculiX input and return its geometry and identity."""
    parameters = _Model.parse(model)
    mesh = _Mesh.generate(parameters)
    edge_weights, area_weights = mesh.measurements()
    deck = _deck(parameters, mesh).encode("ascii")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(deck)
    return {
        "input_sha256": hashlib.sha256(deck).hexdigest(),
        "node_count": len(mesh.xy) * len(mesh.z),
        "element_count": len(mesh.quads) * (len(mesh.z) - 1),
        "lateral_film_face_count": len(mesh.side_faces) * (len(mesh.z) - 1),
        "axial_levels_m": mesh.z.tolist(),
        "cross_section_area_m2": float(area_weights.sum()),
        "cross_section_perimeter_m": float(edge_weights.sum()),
    }


def _dat_number(field: str) -> float:
    value = float(field.replace("D", "E").replace("d", "e"))
    if not math.isfinite(value):
        raise ValueError("CalculiX DAT contains a non-finite value")
    return value


def _read_tables(data: bytes) -> dict[tuple[str, str], list[list[str]]]:
    tables: dict[tuple[str, str], list[list[str]]] = {}
    current = None
    for line in data.decode("ascii").splitlines():
        match = _TABLE_HEADER.match(line)
        if match:
            name, node_set, time = match.groups()
            key = name.lower(), node_set.upper()
            current = None
            if key in _REQUIRED_TABLES:
                if key in tables or _dat_number(time) != 1.0:
                    raise ValueError(f"Expected one final steady CalculiX DAT table for {key}")
                current = tables[key] = []
        elif current is not None and line.strip():
            current.append(line.split())
    if tables.keys() != _REQUIRED_TABLES:
        raise ValueError(f"CalculiX DAT lacks final tables: {sorted(_REQUIRED_TABLES - tables.keys())}")
    return tables


def _nodal_values(rows: list[list[str]], count: int, name: str) -> np.ndarray:
    if len(rows) != count or any(len(row) != 2 for row in rows):
        raise ValueError(f"CalculiX DAT has incomplete {name} records")
    ids = np.array([int(row[0]) for row in rows])
    order = ids.argsort()
    if not np.array_equal(ids[order], np.arange(1, count + 1)):
        raise ValueError(f"CalculiX DAT has duplicate or unexpected {name} node IDs")
    return np.array([_dat_number(row[1]) for row in rows])[order]


def _base_conduction(mesh: _Mesh, temperatures: np.ndarray, conductivity: float) -> np.ndarray:
    """Integrate K*T on the base nodes of the first extruded element layer."""
    xy = mesh.xy[mesh.quads]
    # Only differences affect the conductive residual. Removing a constant
    # reduces roundoff for models close to isothermal.
    local_temperature = (temperatures[:2, mesh.quads] - temperatures[0, 0]).transpose(1, 0, 2)
    local_temperature = local_temperature.reshape(-1, 8)
    cross_sections = np.concatenate((xy, xy), axis=1)
    height = mesh.z[1] - mesh.z[0]
    z = np.broadcast_to([0.0] * 4 + [height] * 4, cross_sections.shape[:2])
    xyz = np.concatenate((cross_sections, z[..., None]), axis=2)
    nodal = np.zeros(mesh.quads.shape)
    for gauss in itertools.product(_GAUSS, repeat=3):
        _, derivative = _shape(_CUBE, gauss)
        # Jacobian rows are spatial directions and columns are natural directions.
        jacobian = np.einsum("end,nj->edj", xyz, derivative)
        physical_gradient = derivative @ np.linalg.inv(jacobian)
        temperature_gradient = np.einsum("en,enj->ej", local_temperature, physical_gradient)
        nodal += conductivity * np.einsum(
            "enj,ej->en", physical_gradient[:, :4], temperature_gradient
        ) * np.linalg.det(jacobian)[:, None]
    result = np.zeros(len(mesh.xy))
    np.add.at(result, mesh.quads.ravel(), nodal.ravel())
    return result


def _read_heat_rates(model: _Model, mesh: _Mesh, temperatures: np.ndarray,
                     rfl: np.ndarray, rfl_total: float, edge_weights: np.ndarray) -> tuple[dict, dict]:
    ambient = float(_solver_float(model.ambient_temperature_c))
    film = float(_solver_float(model.film_coefficient_w_m2_k))
    conductivity = float(_solver_float(model.conductivity_w_m_k))
    excess = temperatures - ambient
    dz = np.diff(mesh.z)
    perimeter_excess = excess @ edge_weights
    side = float(film * np.dot(dz / 2, perimeter_excess[:-1] + perimeter_excess[1:]))
    # Integrate film on the base test functions of the first side band. The
    # axial integrals of N_0*N_0 and N_0*N_1 are dz/3 and dz/6 respectively.
    edge_excess = (2 * excess[0] + excess[1]) * (film * dz[0] / 6)
    edges = mesh.perimeter_edges
    lengths = np.linalg.norm(mesh.xy[edges[:, 1]] - mesh.xy[edges[:, 0]], axis=1)
    shares = lengths[:, None] * (2 * edge_excess[edges] + edge_excess[edges[:, ::-1]]) / 6
    base_film = np.zeros(len(mesh.xy))
    np.add.at(base_film, edges.ravel(), shares.ravel())

    base_conduction = _base_conduction(mesh, temperatures, conductivity)
    base_full = base_conduction + base_film
    choices = {"conductive_KT": base_conduction, "full_KT_plus_film": base_full}
    convention = min(choices, key=lambda candidate: np.abs(rfl - choices[candidate]).sum())
    abs_rate_scale = max(abs(side), abs(rfl_total), float(np.abs(base_full).sum()), 1e-12)
    # DAT NT values have seven significant digits. Resolving the first-layer
    # temperature gradient amplifies their quantization as the layer gets thinner.
    temperature_precision = 1e-6 * max(float(np.abs(temperatures).max()), 1.0)
    gradient_precision_w = temperature_precision * conductivity * (
        math.pi * model.radius_m ** 2 / dz[0] + dz[0] * model.cross_section_divisions ** 2
    )
    residual_error = float(np.abs(rfl - choices[convention]).sum())
    convention_matches = residual_error <= 0.001 * abs_rate_scale + gradient_precision_w
    corrected = rfl_total + float(base_film.sum()) if convention == "conductive_KT" else rfl_total
    imbalance = corrected - side
    rfl_sum = float(rfl.sum())
    checks = {
        "finite_temperatures": True,
        "prescribed_base_matches": bool(np.all(np.abs(
            temperatures[0] - float(_solver_float(model.base_temperature_c))
        ) <= temperature_precision)),
        "raw_base_nodal_sum_matches_total": abs(rfl_sum - rfl_total) <= 0.0001 * abs_rate_scale,
        "native_reaction_matches_integrated_nodal_reaction": bool(convention_matches),
        "corrected_supply_matches_lateral_loss": abs(imbalance) <= 0.001 * abs_rate_scale,
        "reintegrated_supply_matches_lateral_loss": bool(
            abs(float(base_full.sum()) - side) <= 0.001 * abs_rate_scale + gradient_precision_w
        ),
    }
    heat = {
        "raw_base_RFL_total": rfl_total,
        "raw_base_RFL_nodal_sum": rfl_sum,
        "raw_RFL_matches": convention if convention_matches else None,
        "reintegrated_base_conduction_KT": float(base_conduction.sum()),
        "shared_lateral_sink_on_base_nodes": float(base_film.sum()),
        "reintegrated_base_full_including_film": float(base_full.sum()),
        "corrected_base_supply": corrected,
        "lateral_loss_integral": side,
        "corrected_energy_imbalance_w": imbalance,
        "relative_corrected_energy_imbalance": imbalance / abs(side) if side != 0 else None,
    }
    return heat, {"passed": all(checks.values()), **checks}


def read_fin_dat(input_path: Path, dat_path: Path,
                 sample_positions_m: Sequence[float] = ()) -> dict[str, Any]:
    """Read whole-rod nodal NT and signed base RFL from the original DAT file."""
    inp = Path(input_path).read_bytes()
    text = inp.decode("ascii").replace("\r\n", "\n")
    first_line = text.split("\n", 1)[0]
    if not first_line.startswith(_MODEL_PREFIX):
        raise ValueError("Fin input is missing generated circular-fin model metadata")
    model = _Model.parse(json.loads(first_line.removeprefix(_MODEL_PREFIX)))
    mesh = _Mesh.generate(model)
    if text != _deck(model, mesh):
        raise ValueError("Fin input differs from the deck described by its model metadata")
    positions = [_number(position, "sample_positions_m") for position in sample_positions_m]
    if any(position < 0 or position > model.length_m for position in positions):
        raise ValueError("Samples must lie between zero and length_m")
    dat = Path(dat_path).read_bytes()
    tables = _read_tables(dat)
    plane_nodes = len(mesh.xy)
    temp = _nodal_values(tables["temperatures", "ALLNODES"], len(mesh.z) * plane_nodes, "temperature")
    temp = temp.reshape(len(mesh.z), plane_nodes)
    reaction = _nodal_values(tables["heat generation", "BASE"], plane_nodes, "base reaction")
    total = tables["total heat generation", "BASE"]
    if len(total) != 1 or len(total[0]) != 1:
        raise ValueError("CalculiX DAT lacks a single total base reaction")
    edged, area = mesh.measurements()
    heat, closure = _read_heat_rates(model, mesh, temp, reaction, _dat_number(total[0][0]), edged)

    def profiles(stations: Sequence[float], values: np.ndarray) -> list[dict[str, Any]]:
        exterior = values[:, edged > 0]
        return [
            {
                "z_m": z,
                "lateral_mean_c": float(lateral),
                "area_mean_c": float(mean),
                "lateral_range_c": [float(lo), float(hi)],
            }
            for z, lateral, mean, lo, hi in zip(
                stations, (values @ edged) / edged.sum(), (values @ area) / area.sum(),
                exterior.min(axis=1), exterior.max(axis=1),
            )
        ]

    # Interpolate each boundary node before finding the range. The same node
    # need not hold the minimum or maximum on both bracketing planes.
    upper = np.searchsorted(mesh.z, positions).clip(1, len(mesh.z) - 1)
    lower = upper - 1
    fraction = ((np.asarray(positions) - mesh.z[lower]) / (mesh.z[upper] - mesh.z[lower])).clip(0, 1)
    sampled = temp[lower] + fraction[:, None] * (temp[upper] - temp[lower])
    return {
        "input_sha256": hashlib.sha256(inp).hexdigest(),
        "dat_sha256": hashlib.sha256(dat).hexdigest(),
        "node_count": int(temp.size),
        "base_reaction_count": int(reaction.size),
        "temperature_range_c": [float(temp.min()), float(temp.max())],
        "sections": profiles(mesh.z.tolist(), temp),
        "samples": profiles(positions, sampled),
        "heat_w": heat,
        "energy_closure": closure,
    }
