"""Bounded ASCII volume-flux readback from retained native fields."""
import math
import re

_FLOAT_TOKEN = r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"

class CfdResultError(ValueError):
    pass

class ChtDiagnosticError(ValueError):
    pass

def fail(code, message):
    raise ChtDiagnosticError(message)

def _hold(code):
    raise CfdResultError("无法读取完整边界流量场；原因：" + code)

def _flux_require(condition, code):
    if not condition:
        raise ValueError("无法计算边界流量；原因：CFD_STATIONARY_FLUX_HOLD: " + code)

def foam_dictionary(text):
    """Parse a literal dictionary subset only; includes/macros/code are unsupported."""
    text = re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S)
    if "#" in text or "$" in text:
        fail("PHYSICS", "controlDict includes/macros are unsupported")
    tokens = re.findall(r'"[^"\n]*"|[{}();]|[^\s{}();]+', text)
    pos = 0

    def block(nested=False):
        nonlocal pos
        result = {}
        while pos < len(tokens):
            key = tokens[pos]
            pos += 1
            if key == "}":
                if not nested:
                    fail("PHYSICS", "unexpected dictionary closing brace")
                return result
            if key in result or key in ("{", ";", "(", ")") or pos >= len(tokens):
                fail("PHYSICS", "ambiguous dictionary key")
            if tokens[pos] == "{":
                pos += 1
                result[key] = block(True)
                if pos < len(tokens) and tokens[pos] == ";":
                    pos += 1
            else:
                values = []
                while pos < len(tokens) and tokens[pos] != ";":
                    if tokens[pos] in ("{", "}"):
                        fail("PHYSICS", "missing dictionary semicolon")
                    values.append(tokens[pos])
                    pos += 1
                if pos == len(tokens) or not values:
                    fail("PHYSICS", "incomplete dictionary value")
                pos += 1
                result[key] = values
        if nested:
            fail("PHYSICS", "unclosed dictionary block")
        return result

    return block()

def _foam_field(data: bytes, *, name: str, field_class: str,
                label: str, dimensions: str) -> dict:
    try:
        source = data.decode("ascii")
        if "#" in source or "$" in source:
            _hold("CFD_PORT_FIELD_HOLD")
        source = re.sub(r"/\*.*?\*/|//[^\n]*", "", source, flags=re.S)
        header_match = re.search(r"FoamFile\s*\{([^{}]*)\}", source, re.S)
        boundary_matches = list(re.finditer(
            r"(?m)^\s*boundaryField\s*\{", source))
        if header_match is None or len(boundary_matches) != 1:
            _hold("CFD_PORT_FIELD_HOLD")
        boundary_start = boundary_matches[0].start()
        if header_match.end() >= boundary_start:
            _hold("CFD_PORT_FIELD_HOLD")
        dimensions_match = re.search(
            r"(?m)^\s*dimensions\s+\[[^\]]+\]\s*;",
            source[header_match.end():boundary_start],
        )
        if dimensions_match is None:
            _hold("CFD_PORT_FIELD_HOLD")
        boundary_text = source[boundary_start:]
        if len(boundary_text) > 16 * 1024 * 1024:
            _hold("CFD_PORT_FIELD_HOLD")
        for index, _ in enumerate(re.finditer(r"\S+", boundary_text), 1):
            if index > 1_000_000:
                _hold("CFD_PORT_FIELD_HOLD")
        parsed = foam_dictionary(
            source[header_match.start():header_match.end()] + "\n"
            + dimensions_match.group(0) + "\n" + boundary_text
        )
        header = parsed["FoamFile"]
        actual_dimensions = " ".join(parsed["dimensions"]).strip("[]")
        if (header.get("format") != ["ascii"]
                or header.get("class") != [field_class]
                or header.get("object") != [name]
                or header.get("location") != [f'"{label}"']
                or actual_dimensions != dimensions
                or not isinstance(parsed["boundaryField"], dict)):
            _hold("CFD_PORT_FIELD_HOLD")
        return parsed
    except (UnicodeError, KeyError, TypeError, RecursionError,
            ChtDiagnosticError) as exc:
        raise CfdResultError("无法读取完整边界流量场；原因：CFD_PORT_FIELD_HOLD") from exc

def _mesh_patches(data: bytes, *, region=None) -> dict[str, tuple[str, int]]:
    """Read a frozen native polyMesh/boundary table and its full patch coverage."""
    try:
        source = data.decode("ascii")
    except UnicodeError:
        _hold("CFD_PORT_MESH_HOLD")
    source = re.sub(r"/\*.*?\*/|//[^\n]*", "", source, flags=re.S)
    header = re.search(r"FoamFile\s*\{([^{}]*)\}", source, re.S)
    if header is None:
        _hold("CFD_PORT_MESH_HOLD")
    fields = header.group(1)
    if region is not None and re.fullmatch(r'fluid_[0-9a-f]{24}', region) is None:
        _hold('CFD_PORT_MESH_HOLD')
    location = 'constant/'+('' if region is None else region+'/')+'polyMesh'
    for key, value in (
        ("format", "ascii"), ("class", "polyBoundaryMesh"),
        ('location', '"'+location+'"'), ("object", "boundary"),
    ):
        if re.findall(rf"(?m)^\s*{key}\s+([^;]+);", fields) != [value]:
            _hold("CFD_PORT_MESH_HOLD")
    tail = source[header.end():].strip()
    table = re.fullmatch(r"([0-9]+)\s*\((.*)\)", tail, re.S)
    if table is None or len(table.group(1)) > 7:
        _hold("CFD_PORT_MESH_HOLD")
    entry = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\{([^{}]*)\}", re.S)
    matches = list(entry.finditer(table.group(2)))
    if (len(matches) != int(table.group(1))
            or not (1 if region is not None else 2) <= len(matches) <= 4_096
            or entry.sub("", table.group(2)).strip()):
        _hold("CFD_PORT_MESH_HOLD")
    patches: dict[str, tuple[str, int]] = {}
    previous_end = None
    total_faces = 0
    for match in matches:
        name, body = match.groups()
        properties = {}
        for key in ("type", "nFaces", "startFace"):
            found = re.findall(rf"(?m)^\s*{key}\s+([^;]+);", body)
            if len(found) != 1:
                _hold("CFD_PORT_MESH_HOLD")
            properties[key] = found[0].strip()
        count, start = properties["nFaces"], properties["startFace"]
        if (name in patches or not count.isdigit() or not start.isdigit()
                or len(count) > 7 or len(start) > 10
                or int(count) <= 0 or int(count) > 4_000_000
                or (previous_end is not None and int(start) != previous_end)):
            _hold("CFD_PORT_MESH_HOLD")
        total_faces += int(count)
        if total_faces > 500_000:
            _hold("CFD_PORT_MESH_HOLD")
        previous_end = int(start) + int(count)
        patches[name] = (properties["type"], int(count))
    return patches

def _patch_scalar_values(field: dict, patch: str, count: int) -> tuple[float, ...]:
    values = field.get("boundaryField", {}).get(patch, {}).get("value")
    if not isinstance(values, list):
        _hold("CFD_PORT_PATCH_HOLD")
    if len(values) == 2 and values[0] == "uniform":
        payload = (values[1],)
    elif (len(values) >= 6 and values[:2] == ["nonuniform", "List<scalar>"]
          and values[2].isdigit() and len(values[2]) <= 7
          and int(values[2]) == count
          and values[3] == "(" and values[-1] == ")"
          and len(values) == count + 5):
        payload = values[4:-1]
    else:
        _hold("CFD_PORT_PATCH_HOLD")
    if any(re.fullmatch(_FLOAT_TOKEN, value) is None for value in payload):
        _hold("CFD_PORT_VALUE_HOLD")
    numbers = tuple(float(value) for value in payload)
    if not all(math.isfinite(value) for value in numbers):
        _hold("CFD_PORT_VALUE_HOLD")
    return numbers * count if len(values) == 2 else numbers

def _patch_flux_sum(field: dict, patch: str, count: int) -> float:
    try:
        result = math.fsum(_patch_scalar_values(field, patch, count))
    except OverflowError:
        _hold("CFD_PORT_VALUE_HOLD")
    if not math.isfinite(result):
        _hold("CFD_PORT_VALUE_HOLD")
    return result

def _flux_arithmetic(phi, boundary, *, iteration_index, inlet_patch, outlet_patch, region=None):
    _flux_require(type(iteration_index) is int and iteration_index > 0
             and type(phi) is bytes and type(boundary) is bytes
             and len(phi) <= 64*1024**2 and len(boundary) <= 16*1024**2,
             "BOUNDED_STATIONARY_SAMPLE_REQUIRED")
    patches = _mesh_patches(boundary, region=region)
    _flux_require(inlet_patch != outlet_patch and {inlet_patch, outlet_patch} <= patches.keys()
             and all(patches[n][0] == "patch" for n in (inlet_patch, outlet_patch))
             and all(kind == "wall" for n, (kind, _) in patches.items()
                     if n not in (inlet_patch, outlet_patch)), "CONFIRMED_PATCH_COVERAGE")
    field = _foam_field(phi, name="phi", field_class="surfaceScalarField",
                        label=str(iteration_index)+('' if region is None else '/'+region),
                        dimensions="0 3 -1 0 0 0 0")
    _flux_require(set(field["boundaryField"]) == set(patches), "PHI_PATCH_COVERAGE")
    values = {n: _patch_flux_sum(field, n, count) for n, (_, count) in patches.items()}
    inlet, outlet = values[inlet_patch], values[outlet_patch]
    other = math.fsum(v for n, v in values.items() if n not in (inlet_patch, outlet_patch))
    net = math.fsum(values.values())
    denominator = max(abs(inlet), abs(outlet))
    ratio = None if denominator == 0 else abs(net) / denominator
    _flux_require(all(math.isfinite(v) for v in (inlet, outlet, other, net))
             and (ratio is None or math.isfinite(ratio)), "NONFINITE_INTEGRAL")
    return dict(axis="steady_iteration", iteration_index=iteration_index, unit="m^3/s",
        inlet_patch=inlet_patch, outlet_patch=outlet_patch,
        inlet_m3_s=inlet, outlet_m3_s=outlet, other_m3_s=other, net_m3_s=net,
        relative_imbalance=ratio, patch_integrals=values,
        direction="INLET_NEGATIVE_OUTLET_POSITIVE" if inlet < 0 < outlet else "REVERSED_OR_ZERO",
        method="sum_native_outward_phi_on_all_frozen_boundary_faces",
        evidence="ARITHMETIC_ONLY_NOT_ACCEPTANCE", engineering_acceptance="NOT_CLAIMED")
