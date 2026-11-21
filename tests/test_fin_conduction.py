"""Regression checks for the generated fin deck and signed CalculiX DAT readback."""
from __future__ import annotations

import math

import pytest

from navifield.fin_conduction import build_fin_input, read_fin_dat


def _model(**overrides):
    return {
        "radius_m": 1.0,
        "length_m": 1.0,
        "conductivity_w_m_k": 2.0,
        "base_temperature_c": 0.0,
        "ambient_temperature_c": 0.0,
        "film_coefficient_w_m2_k": 1.0,
        "cross_section_divisions": 2,
        "axial_divisions": 1,
        **overrides,
    }


def _write_dat(path, temperatures, reactions, *, node_ids=None):
    if node_ids is None:
        node_ids = range(1, len(temperatures) + 1)
    rows = ["         S T E P       1", "          INCREMENT     1", "",
            " temperatures for set ALLNODES and time 0.1000000D+01", ""]
    rows.extend(f"{i}  {value:.12e}" for i, value in zip(node_ids, temperatures))
    rows.extend(["", " heat generation for set BASE and time 0.1000000E+01", ""])
    rows.extend(f"{i} {value:.12e}" for i, value in enumerate(reactions, 1))
    rows.extend(["", " total heat generation for set BASE and time 0.1000000E+01", "",
                 f"{sum(reactions):.12e}", ""])
    path.write_text("\n".join(rows), encoding="ascii")


def test_mesh_subdivides_each_anchor_interval_and_excludes_tip_film(tmp_path):
    path = tmp_path / "model.inp"
    mesh = build_fin_input(_model(axial_divisions=2, axial_breakpoints_m=[0.2, 0.7]), path)
    assert mesh["axial_levels_m"] == pytest.approx([0, 0.1, 0.2, 0.45, 0.7, 0.85, 1])
    assert mesh["node_count"] == 63
    assert mesh["element_count"] == 24
    assert mesh["lateral_film_face_count"] == 48
    body = path.read_text(encoding="ascii").split("*FILM\n")[1].split("*NODE FILE")[0]
    assert len(body.splitlines()) == 48
    assert {row.split(",")[1] for row in body.splitlines()} == {"F3", "F4", "F5", "F6"}


@pytest.mark.parametrize("breakpoints", [[0.5, 0.5], [1.0], [0.6, 0.4], [float("nan")]])
def test_declines_invalid_breakpoints(tmp_path, breakpoints):
    with pytest.raises(ValueError, match="axial_breakpoints_m"):
        build_fin_input(_model(axial_breakpoints_m=breakpoints), tmp_path / "model.inp")


@pytest.mark.parametrize("sign", [1, -1])
@pytest.mark.parametrize("full", [True, False])
def test_signed_reaction_conventions_and_interpolation(tmp_path, sign, full):
    inp = tmp_path / "model.inp"
    dat = tmp_path / "model.dat"
    # With two divisions the cross-section is an inscribed regular octagon.
    # The bottom test-function areas come from the linear Jacobian of each
    # bilinear quad; the bottom side test-function integral shares are uniform.
    offset = math.sqrt(0.5) - 1
    corner = 0.25 + offset / 3
    edge = 2 * (0.25 + offset / 4)
    center = 4 * (0.25 + offset / 6)
    nodal_areas = [corner, edge, corner, edge, center, edge, corner, edge, corner]
    perimeter = 16 * math.sin(math.pi / 8)
    conductivity = (2 * perimeter / 3) / (4 * math.sqrt(0.5))
    side = sign * 1.5 * perimeter
    shared = sign * (5 / 6) * perimeter
    conductive_reactions = [sign * conductivity * area for area in nodal_areas]
    reactions = [
        reaction + (shared / 8 if full and i != 4 else 0)
        for i, reaction in enumerate(conductive_reactions)
    ]
    build_fin_input(_model(conductivity_w_m_k=conductivity, base_temperature_c=2 * sign), inp)
    _write_dat(dat, [2 * sign] * 9 + [sign] * 9, reactions)
    report = read_fin_dat(inp, dat, [0.25, 0, 1])
    assert report["energy_closure"]["passed"]
    assert report["heat_w"]["raw_RFL_matches"] == ("full_KT_plus_film" if full else "conductive_KT")
    assert report["heat_w"]["corrected_base_supply"] == pytest.approx(side)
    assert report["heat_w"]["lateral_loss_integral"] == pytest.approx(side)
    assert [sample["lateral_mean_c"] for sample in report["samples"]] == pytest.approx(
        [1.75 * sign, 2 * sign, sign]
    )

    # Exchange signed reactions between two nodes while retaining the total.
    reactions[0] += 0.5
    reactions[4] -= 0.5
    _write_dat(dat, [2 * sign] * 9 + [sign] * 9, reactions)
    invalid = read_fin_dat(inp, dat)
    assert not invalid["energy_closure"]["passed"]
    assert not invalid["energy_closure"]["native_reaction_matches_integrated_nodal_reaction"]


def test_isothermal_and_invalid_native_rows(tmp_path):
    inp = tmp_path / "model.inp"
    dat = tmp_path / "model.dat"
    build_fin_input(_model(base_temperature_c=17, ambient_temperature_c=17), inp)
    _write_dat(dat, [17] * 18, [0] * 9)
    report = read_fin_dat(inp, dat)
    assert report["energy_closure"]["passed"]
    assert report["heat_w"]["corrected_base_supply"] == 0
    assert report["heat_w"]["relative_corrected_energy_imbalance"] is None
    assert report["samples"] == []

    _write_dat(dat, [17] * 18, [0] * 9, node_ids=[1] * 18)
    with pytest.raises(ValueError, match="node IDs"):
        read_fin_dat(inp, dat)
    _write_dat(dat, [17] * 17 + [float("nan")], [0] * 9)
    with pytest.raises(ValueError, match="non-finite"):
        read_fin_dat(inp, dat)


def test_rejects_changed_deck_physics(tmp_path):
    deck = tmp_path / "model.inp"
    build_fin_input(_model(), deck)
    deck.write_bytes(deck.read_bytes().replace(b"BASE,11,11,0", b"BASE,11,11,1"))
    with pytest.raises(ValueError, match="differs from"):
        read_fin_dat(deck, tmp_path / "model.dat")
