"""Compile and execute a confirmed one-dimensional electrothermal expansion control."""

import argparse
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
from tempfile import mkdtemp

from . import strip_language

MODEL = "steady_dc_uniform_adiabatic_1d_fixed_free"
FIELDS = {
    "length_m": "轴向长度 [m]",
    "width_m": "均匀矩形截面宽度 [m]",
    "thickness_m": "均匀矩形截面厚度 [m]",
    "resistivity_ohm_m": "恒定电阻率 [Ω·m]",
    "conductivity_w_m_k": "恒定导热率 [W/(m·K)]",
    "expansion_per_k": "恒定线膨胀系数 [1/K]",
    "young_pa": "轴向弹性模量 [Pa]",
    "poisson": "各向同性泊松比 [1]",
    "reference_k": "均匀初始及热膨胀参考温度 [K]",
    "left_voltage_v": "x=0 电极电势 [V]",
    "right_voltage_v": "x=L 电极电势 [V]",
    "left_temperature_k": "x=0 端面定温 [K]",
    "right_temperature_k": "x=L 端面定温 [K]",
}
SCOPE = (
    "一维均匀矩形直条，电压驱动直流稳态，常物性，无接触电阻，侧面绝热，无其他体热源。"
    "左端轴向固定，右端轴向无外载且自由。小变形杆只解析轴向位移，截面温度均匀，不解析横移或局部泊松变形。"
)
TARGETS = ("device_power", "maximum_strip_temperature", "free_end_axial_displacement")


def _as_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _prestress_requested(text):
    for datum in re.finditer(r"预应力|pre[- ]?stress", text):
        clause = re.split(r"[。；;，,！？!?\n]", text[:datum.start()])[-1]
        markers = re.findall(r"不忽略|不得忽略|不能忽略|考虑|保留|施加|存在|计入|不计入|不考虑|不计|忽略|没有|无|"
                             r"do not ignore|without|neglect|ignore|\bno\b|include|retain", clause)
        if not markers or markers[-1] in {"不忽略", "不得忽略", "不能忽略", "考虑", "保留", "施加", "存在", "计入",
                                         "do not ignore", "include", "retain"}:
            return True
    return False


def review(request):
    text = "\n".join(str(request.get(k, "")) for k in ("original_request", "description")).casefold()
    candidate = (
        any(word in text for word in ("直条", "直杆", "铝膜条", "铝条", "薄膜条", "straight strip", "straight rod"))
        and any(word in text for word in ("轴向", "自由热伸长", "沿条长", "沿长度", "横截面平均", "axial", "free expansion"))
        and any(word in text for word in ("电压", "直流", "接电", "通电", "恒压", "voltage", " dc", "joule"))
    )
    if "strip" not in request and not candidate:
        return None
    raw = request.get("strip", {})
    if not isinstance(raw, dict):
        raise ValueError("strip must be an object")
    unknown = sorted(raw.keys() - FIELDS.keys() - {"model", "elements"})
    if unknown:
        raise ValueError(f"Unknown strip fields: {', '.join(unknown)}")
    conditions = set(request.get("operating_conditions", []))
    boundary = set(request.get("boundaries", []))
    outside = conditions & {"transient"} or boundary & {"current_drive", "convection"}
    contradicted = (
        "temperature_dependent_resistivity" in request.get("materials", [])
        or any(word in text for word in (
            "冷热臂", "横向偏移", "两端轴向固定", "两端机械固定", "两端夹紧", "两端沿轴向都刚性夹住",
            "两端都固定轴向", "两端均固定轴向", "双端轴向夹持", "热压应力", "沿轴向都被刚性夹住",
            "器件不接电", "没有接电源", "未接电源", "不通电", "不加电", "外加热源",
            "外部光斑加热", "外部激光加热", "已知温度场", "外测温度", "实测温度已经给出",
            "非稳态", "非恒物性", "不忽略对流", "计入对流", "考虑表面对流", "考虑接触电阻",
            "非一维", "三维模型", "3d model", "右端轴向受力", "右端施加拉力",
            "变截面", "锥形", "逐渐变窄", "逐渐变宽", "tapered", "nonuniform cross section", "脉冲",
            "给定电流", "固定电流", "功率驱动", "侧面强制对流", "考虑物性温变", "hot and cold arm",
            "both ends clamped", "prescribed temperature field", "unpowered", "external heater", "current driven", "current drive", "pulsed voltage", "variable cross section",
        ))
    )
    result = dict(status="partial", scope=SCOPE, targets=TARGETS, missing=[], next_action="ask_user")
    if outside or contradicted or _prestress_requested(text) or ("model" in raw and raw["model"] != MODEL):
        return {**result, "next_action": "review_applicability", "reason": "工况与直条模型适用范围冲突。"}
    original = "\n".join(str(request.get(k, "")) for k in ("original_request", "description"))
    parsed, sources, conflicts, unresolved = strip_language.extract(original, MODEL, raw)
    for key in raw.keys() & parsed.keys():
        if key == "model":
            continue
        if not strip_language.same_value(raw[key], parsed[key]):
            conflicts.add(key)
    if conflicts:
        return {**result, "next_action": "resolve_conflicts", "conflicting_fields": sorted(conflicts),
                "normalized_inputs": parsed, "input_sources": sources}
    for key in raw.keys() - unresolved:
        sources.setdefault(key, []).append(f"structured strip.{key}")
    raw = {**parsed, **{key: raw[key] for key in raw.keys() - unresolved}}
    result.update(normalized_inputs=raw, input_sources=sources)
    if unresolved:
        result["unresolved_fields"] = sorted(unresolved)
    needed = {field: meaning for field, meaning in FIELDS.items() if field not in raw}
    if "elements" in unresolved:
        needed["elements"] = "确认沿轴向划分的线性单元数量。"
    if "model" not in raw:
        needed["model"] = f"采用该理想化工况时填写 {MODEL}，逐项核对适用范围。"
    clean = {}
    for field, description in FIELDS.items():
        if field not in raw:
            continue
        value = raw[field]
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f"{field} must be a finite SI number: {description}")
        if field == "poisson":
            if not -1 < value < .5:
                raise ValueError("poisson must be in (-1, 0.5)")
        elif field not in ("left_voltage_v", "right_voltage_v", "expansion_per_k") and value <= 0:
            raise ValueError(f"{field} must be positive")
        clean[field] = float(value)
    elements = raw.get("elements", 100)
    if type(elements) is not int or not 2 <= elements <= 1000:
        raise ValueError("elements must be an integer in [2, 1000]")
    if needed:
        result["missing"] = [{"key": field, "meaning": meaning} for field, meaning in needed.items()]
        supplied_text = any(request.get(k) for k in ("original_request", "description"))
        if supplied_text and not raw:
            result["next_action"] = "normalize_then_confirm"
        return result
    if not text.strip():
        raise ValueError("Retain the original requirement or description for confirmation")
    if max(clean["width_m"], clean["thickness_m"]) >= clean["length_m"]:
        return {**result, "next_action": "review_applicability", "reason": "直条轴向长度需大于截面尺寸。"}
    area = clean["width_m"] * clean["thickness_m"]
    dx = clean["length_m"] / elements
    if not all(math.isfinite(v) and v > 0 for v in (
        area, area * 1e6, dx, dx * 1e3, clean["young_pa"] / 1e6,
        area / clean["resistivity_ohm_m"], area * clean["conductivity_w_m_k"],
    )):
        raise ValueError("Strip dimensions/materials exceed the numerical range")
    summary = {"model": MODEL, **clean, "elements": elements, "scope": SCOPE,
               "request_sha256": _request_hash(request)}
    token = hashlib.sha256(_as_json(summary).encode()).hexdigest()
    return {**result, "next_action": "confirm", "confirmation_sha256": token, "summary": summary}


def _request_hash(request):
    identity = {k: request[k] for k in ("original_request", "description") if k in request}
    return hashlib.sha256(_as_json(identity).encode()).hexdigest()


def _mesh(spec, per_element=False):
    count = spec["elements"]
    size = spec["length_m"]
    lines = ["$MeshFormat", "2.2 0 8", "$EndMeshFormat", "$Nodes", str(count + 1)]
    lines.extend(f"{n + 1} {size * (n / count):.17g} 0 0" for n in range(count + 1))
    lines.extend(["$EndNodes", "$Elements", str(count + 2), "1 15 2 1 1 1",
                  f"2 15 2 2 2 {count + 1}"])
    lines.extend(f"{e + 3} 1 2 {e + 1000 if per_element else 10} 10 {e + 1} {e + 2}" for e in range(count))
    return "\n".join([*lines, "$EndElements", ""])


def _formulation(spec, sources=None):
    thermal = sources is not None
    regions = "10" if not thermal else "{" + ",".join(str(i + 1000) for i in range(len(sources))) + "}"
    area = spec["width_m"] * spec["thickness_m"]
    conductivity = spec["conductivity_w_m_k"] if thermal else 1 / spec["resistivity_ohm_m"]
    left = spec["left_temperature_k"] if thermal else spec["left_voltage_v"]
    right = spec["right_temperature_k"] if thermal else spec["right_voltage_v"]
    source_values = "" if not thermal else " ".join(f"q[Region[{i + 1000}]]={q:.17g};" for i, q in enumerate(sources))
    source_term = "" if not thermal else "Integral { [-q[]*A[], {f}]; In Strip; Jacobian J; Integration I; }"
    return f"""Group {{ Left=Region[1]; Right=Region[2]; Strip=Region[{regions}]; }}
Function {{ A[]={area:.17g}; c[]={conductivity:.17g}; {source_values} }}
Constraint {{ {{ Name Ends; Type Assign; Case {{
  {{ Region Left; Value {left:.17g}; }} {{ Region Right; Value {right:.17g}; }} }} }} }}
Jacobian {{ {{ Name J; Case {{ {{ Region All; Jacobian Vol; }} }} }} }}
Integration {{ {{ Name I; Case {{ {{ Type Gauss; Case {{ {{ GeoElement Line; NumberOfPoints 3; }} }} }} }} }} }}
FunctionSpace {{ {{ Name H; Type Form0;
  BasisFunction {{ {{ Name sn; NameOfCoef fn; Function BF_Node; Support Strip; Entity NodesOf[All]; }} }}
  Constraint {{ {{ NameOfCoef fn; EntityType NodesOf; NameOfConstraint Ends; }} }} }} }}
Formulation {{ {{ Name Field; Type FemEquation; Quantity {{ {{ Name f; Type Local; NameOfSpace H; }} }} Equation {{
  Integral {{ [c[]*A[]*Dof{{d f}}, {{d f}}]; In Strip; Jacobian J; Integration I; }}
  {source_term}
}} }} }}
Resolution {{ {{ Name SolveField; System {{ {{ Name S; NameOfFormulation Field; }} }}
  Operation {{ Generate[S]; Solve[S]; SaveSolution[S]; }} }} }}
PostProcessing {{ {{ Name FieldPost; NameOfFormulation Field; PostQuantity {{
  {{ Name scalar; Value {{ Term {{ [{{f}}]; In Strip; Jacobian J; }} }} }}
  {{ Name gradient; Value {{ Term {{ [{{d f}}]; In Strip; Jacobian J; }} }} }}
}} }} }}
PostOperation {{ {{ Name Readback; NameOfPostProcessing FieldPost; Operation {{
  Print[scalar, OnElementsOf Strip, Format Table, File "values.txt"];
  Print[gradient, OnElementsOf Strip, Format Table, File "gradient.txt"];
}} }} }}
"""


def prepare(request, confirmation, output):
    reviewed = review(request)
    if not reviewed or not reviewed.get("confirmation_sha256"):
        return reviewed or {"status": "partial", "next_action": "review_applicability"}
    if confirmation != reviewed["confirmation_sha256"]:
        return {**reviewed, "reason": "确认摘要缺失、已变化或哈希不一致。"}
    from . import capability_kb, chain_intake

    with closing(capability_kb.connect()) as db:
        plans = chain_intake.discover(db, request, request.get("solver"), request.get("version"), strip_review=reviewed)
    if not any(plan.get("native_adapter", {}).get("id") == "confirmed_axial_strip" for plan in plans):
        return {**reviewed, "next_action": "review_applicability",
                "reason": "当前索引及所选版本没有匹配的可编译直条路线。"}
    spec = reviewed["summary"]
    mesh, deck = _mesh(spec), _formulation(spec)
    target = Path(output).resolve()
    target.mkdir()
    (target / "strip.json").write_text(_as_json({
        "summary": spec, "confirmation_sha256": confirmation,
        "input_sources": reviewed["input_sources"],
    }) + "\n", encoding="utf-8")
    (target / "electric.msh").write_text(mesh, encoding="ascii")
    (target / "electric.pro").write_text(deck, encoding="ascii")
    return {"status": "prepared", "case": str(target), "inputs": ["strip.json", "electric.msh", "electric.pro"],
            "worker_module": __name__, "getdp_version": "3.5.0", "calculix_version": "2.22",
            "worker_options": ["--getdp", "--calculix"], "confirmation_sha256": confirmation,
            "outputs": ["strip-native-*/result.json"], "next_action": "run_via_navifield_run"}



def _connectivity(spec):
    count = spec["elements"]
    x = [spec["length_m"] * (i / count) for i in range(count + 1)]
    return x, {i + 3: i for i in range(count)}


def _read_field(path, spec, vector=False):
    x, elements = _connectivity(spec)
    rows = {}
    for line in path.read_text(encoding="ascii").splitlines():
        if not line.strip():
            continue
        data = [float(value) for value in line.split()]
        if len(data) != (17 if vector else 13) or not all(map(math.isfinite, data)):
            raise ValueError(f"Invalid GetDP element row in {path.name}")
        _, number, x0, y0, z0, x1, y1, z1 = data[:8]
        if (number not in elements or number in rows or any((y0, z0, y1, z1)) or data[0] != 1):
            raise ValueError(f"Unexpected or duplicate GetDP element in {path.name}: {number}")
        i = elements[number]
        if not (math.isclose(x0, x[i], rel_tol=1e-12) and math.isclose(x1, x[i + 1], rel_tol=1e-12)):
            raise ValueError(f"GetDP element coordinates disagree for element {number}")
        if vector:
            if any(data[j] for j in (12, 13, 15, 16)) or not math.isclose(data[11], data[14], rel_tol=1e-10):
                raise ValueError(f"Unexpected transverse or nonconstant gradient for element {number}")
            rows[int(number)] = data[11]
        else:
            rows[int(number)] = tuple(data[11:13])
    if rows.keys() != elements.keys():
        raise ValueError(f"Incomplete element coverage in {path.name}")
    values = [rows[i] for i in elements]
    if vector:
        return values
    if any(not math.isclose(a[1], b[0], rel_tol=1e-10, abs_tol=1e-11) for a, b in zip(values, values[1:])):
        raise ValueError(f"Discontinuous nodal readback in {path.name}")
    return [values[0][0], *(pair[1] for pair in values)]


# CalculiX 2.22 input cards limit numeric field width. Twelve significant
# digits preserve the strip mapping and stay within 20 columns including exponent.
def _mechanical(spec, temperatures):
    x, elements = _connectivity(spec)
    area_mm2 = spec["width_m"] * spec["thickness_m"] * 1e6
    lines = ["*HEADING", "Confirmed 1D axial strip; mm, N, MPa, K", "*NODE"]
    lines.extend(f"{i + 1},{position * 1e3:.12g},0,0" for i, position in enumerate(x))
    lines.extend(["*ELEMENT,TYPE=T3D2,ELSET=STRIP"])
    lines.extend(f"{eid},{i + 1},{i + 2}" for eid, i in elements.items())
    lines.extend([
        "*NSET,NSET=ALLN,GENERATE", f"1,{len(x)},1", "*MATERIAL,NAME=STRIP",
        "*ELASTIC", f"{spec['young_pa']/1e6:.12g},{spec['poisson']:.12g}",
        f"*EXPANSION,ZERO={spec['reference_k']:.12g}", f"{spec['expansion_per_k']:.12g}",
        "*SOLID SECTION,ELSET=STRIP,MATERIAL=STRIP", f"{area_mm2:.12g}",
        "*BOUNDARY", "ALLN,2,3,0", "1,1,1,0",
        "*INITIAL CONDITIONS,TYPE=TEMPERATURE", f"ALLN,{spec['reference_k']:.12g}",
        "*STEP", "*STATIC", "*TEMPERATURE",
    ])
    lines.extend(f"{i + 1},{value:.12g}" for i, value in enumerate(temperatures))
    return "\n".join([*lines, "*NODE PRINT,NSET=ALLN", "U", "*END STEP", ""])


def _read_displacement(path, count):
    header = re.compile(r"displacements\s+\(vx,vy,vz\) for set ALLN\b", re.IGNORECASE)
    sections = header.split(path.read_text(encoding="ascii"))
    if len(sections) != 2:
        raise ValueError("Expected one native CalculiX displacement table for ALLN")
    rows = {}
    for line in sections[1].splitlines()[1:]:
        if not line.strip():
            if rows:
                break
            continue
        node, axial, y, z = line.split()
        number = int(node)
        if number in rows:
            raise ValueError(f"Duplicate CalculiX displacement node {node}")
        components = [float(v) * 1e-3 for v in (axial, y, z)]
        if not all(map(math.isfinite, components)):
            raise ValueError(f"Non-finite CalculiX result at node {node}")
        rows[number] = components[0]
    if set(rows) != set(range(1, count + 1)) or abs(rows[1]) > 1e-12:
        raise ValueError("Incomplete CalculiX result or incorrect fixed-node displacement")
    return rows[count]


def _version(path, argument, expected):
    with subprocess.Popen([str(path), argument], stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as child:
        output = child.communicate()[0].decode(errors="replace")
    if not re.search(r"(?<![\d.])" + re.escape(expected) + r"(?![\d.])", output):
        raise ValueError(f"Expected native solver version {expected}: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(path, cwd, argv, log):
    with (cwd / log).open("xb") as output:
        completed = subprocess.run([str(path), *argv], cwd=cwd, stdout=output, stderr=subprocess.STDOUT)
    if completed.returncode:
        raise ValueError(f"Native solver exited {completed.returncode}; see {cwd / log}")


def _check_ends(values, spec, suffix, unit):
    expected = (spec[f"left_{suffix}_{unit}"], spec[f"right_{suffix}_{unit}"])
    if not all(math.isclose(v, target, rel_tol=1e-10, abs_tol=1e-12)
               for v, target in zip((values[0], values[-1]), expected)):
        raise ValueError(f"Solved {suffix} at the ends disagrees with the confirmed boundary")


def _write_json(path, value):
    path.write_text(_as_json(value) + "\n", encoding="utf-8")


def execute(folder, getdp, ccx):
    receipt = json.loads((folder / "strip.json").read_text(encoding="utf-8"))
    spec = receipt["summary"]
    confirmation = hashlib.sha256(_as_json(spec).encode()).hexdigest()
    if confirmation != receipt["confirmation_sha256"] or spec["model"] != MODEL or spec["scope"] != SCOPE:
        raise ValueError("Prepared strip summary does not match the confirmation")
    input_names = ("electric.msh", "electric.pro")
    expected = (_mesh(spec), _formulation(spec))
    if any((folder / name).read_text(encoding="ascii") != content
           for name, content in zip(input_names, expected)):
        raise ValueError("Native inputs differ from the confirmed strip compilation")
    getdp_hash = _version(getdp, "-version", "3.5.0")
    ccx_hash = _version(ccx, "-v", "2.22")
    native = Path(mkdtemp(prefix="strip-native-", dir=folder))
    electric = native / "electric"
    thermal = native / "thermal"
    structural = native / "structural"
    for stage in (electric, thermal, structural):
        stage.mkdir()
    for name, content in zip(input_names, expected):
        (electric / name).write_text(content, encoding="ascii")
    _run(getdp, electric, ["electric.pro", "-msh", "electric.msh", "-solve", "SolveField", "-pos", "Readback"], "getdp.log")
    grad_v = _read_field(electric / "gradient.txt", spec, vector=True)
    volts = _read_field(electric / "values.txt", spec)
    _check_ends(volts, spec, "voltage", "v")
    rho = spec["resistivity_ohm_m"]
    q = [gradient * gradient / rho for gradient in grad_v]
    if not all(map(math.isfinite, q)):
        raise ValueError("Non-finite Joule sources")
    x, elements = _connectivity(spec)
    widths = [b - a for a, b in zip(x, x[1:])]
    area = spec["width_m"] * spec["thickness_m"]
    power = math.fsum(source * area * dx for source, dx in zip(q, widths))
    signed_left, signed_right = area * grad_v[0] / rho, -area * grad_v[-1] / rho
    terminal = -(volts[0] * signed_left + volts[-1] * signed_right)
    _write_json(electric / "source-map.json", [
        {"electric_element": eid, "thermal_region": i + 1000, "nodes": [i + 1, i + 2],
         "x_m": [x[i], x[i + 1]], "dV_dx_V_m": grad_v[i], "joule_W_m3": q[i]} for eid, i in elements.items()
    ])
    (thermal / "thermal.msh").write_text(_mesh(spec, per_element=True), encoding="ascii")
    (thermal / "thermal.pro").write_text(_formulation(spec, sources=q), encoding="ascii")
    _run(getdp, thermal, ["thermal.pro", "-msh", "thermal.msh", "-solve", "SolveField", "-pos", "Readback"], "getdp.log")
    temperatures = _read_field(thermal / "values.txt", spec)
    _check_ends(temperatures, spec, "temperature", "k")
    grad_t = _read_field(thermal / "gradient.txt", spec, vector=True)
    # Consistent reaction of a linear end element includes its half-element source load.
    k_a = spec["conductivity_w_m_k"] * area
    heat_left = k_a * grad_t[0] + 0.5 * q[0] * area * widths[0]
    heat_right = -k_a * grad_t[-1] + 0.5 * q[-1] * area * widths[-1]
    _write_json(thermal / "temperature-map.json", [
        {"node": i + 1, "x_m": position, "temperature_K": temperatures[i]} for i, position in enumerate(x)
    ])
    (structural / "axial.inp").write_text(_mechanical(spec, temperatures), encoding="ascii")
    _run(ccx, structural, ["-i", "axial"], "ccx.log")
    displacement = _read_displacement(structural / "axial.dat", len(x))
    displacement_integral = spec["expansion_per_k"] * math.fsum(
        ((t0 + t1) * 0.5 - spec["reference_k"]) * dx for t0, t1, dx in zip(temperatures, temperatures[1:], widths)
    )
    peak = max(range(len(temperatures)), key=temperatures.__getitem__)
    result = {
        "status": "native_readback", "scope": SCOPE, "confirmation_sha256": confirmation,
        "reference_validation": "not_assessed",
        "tools": {"getdp": {"version": "3.5.0", "sha256": getdp_hash},
                  "calculix": {"version": "2.22", "sha256": ccx_hash}},
        "electrical": {"terminal_power_W": terminal, "integrated_joule_power_W": power,
                       "left_outward_current_A": signed_left, "right_outward_current_A": signed_right,
                       "outward_current_sum_A": signed_left + signed_right,
                       "terminal_minus_joule_W": terminal - power},
        "thermal": {"maximum_K": temperatures[peak], "maximum_x_m": x[peak], "maximum_node": peak + 1,
                    "left_end_K": temperatures[0], "right_end_K": temperatures[-1],
                    "left_outward_heat_W": heat_left, "right_outward_heat_W": heat_right,
                    "outward_heat_minus_joule_W": heat_left + heat_right - power},
        "structural": {"free_end_axial_displacement_m": displacement, "free_end_x_m": x[-1],
                       "free_end_node": len(x), "integral_of_mapped_thermal_strain_m": displacement_integral,
                       "displacement_minus_integral_m": displacement - displacement_integral},
    }
    _write_json(native / "result.json", result)
    return native / "result.json"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run a prepared strip in navifield run's working directory")
    parser.add_argument("--getdp", type=Path, required=True)
    parser.add_argument("--calculix", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = execute(Path.cwd(), args.getdp.resolve(strict=True), args.calculix.resolve(strict=True))
        print(_as_json({"status": "native_readback", "result": str(result)}))
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as error:
        print(_as_json({"status": "failed", "message": str(error)}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
