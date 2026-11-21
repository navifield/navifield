"""Recognize explicit, unit-bearing facts for the axial strip model."""

import math
import re

_NUMBER = r"[+\-−]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+\-−]?\d+|\s*[×x*]\s*10\s*(?:\^\s*[+\-−]?\d+|[⁺⁻⁰¹²³⁴⁵⁶⁷⁸⁹]+))?"
_SUPERSCRIPTS = str.maketrans("⁺⁻⁰¹²³⁴⁵⁶⁷⁸⁹−", "+-0123456789-")
_SEPARATOR = r"\s*(?:为|是|为约|=|[：:]|of\b)?\s*"
_V = r"毫伏|伏特|伏|mV|volts?|V"
_T = r"摄氏度|°\s*C|℃|开尔文|K"
_LENGTH = r"毫米|微米|纳米|米|mm|[μµu]m|nm|m"
_LABELS = {
    "length_m": (r"轴向长度|长度|(?<![a-z])length", _LENGTH),
    "width_m": (r"宽度|(?<![a-z])width", _LENGTH),
    "thickness_m": (r"薄膜厚度|厚度|(?<![a-z])thickness", _LENGTH),
    "resistivity_ohm_m": (r"电阻率|(?<![a-z])(?:electrical\s+)?resistivity", r"(?:Ω|ohms?|欧姆)\s*[·⋅*.\-]?\s*(?:m|米)"),
    "conductivity_w_m_k": (r"导热(?:系数|率)|thermal\s+conductivity", r"W\s*/\s*(?:[（(]\s*m\s*[·⋅*\s]?\s*K\s*[）)]|m\s*[·⋅*\s]?\s*K)"),
    "expansion_per_k": (r"(?:线|热)?膨胀系数|(?:linear\s+)?(?:thermal\s+)?expansion\s+coefficient", r"/\s*K|K\s*(?:\^\s*[-−]1|[-−]1|⁻¹)|ppm\s*/\s*K"),
    "young_pa": (r"弹性模量|杨氏模量|Young[’']?s\s+modulus", r"GPa|MPa|kPa|Pa"),
    "poisson": (r"泊松比|Poisson[’']?s\s+ratio", r""),
    "reference_k": (r"无应力(?:参考|初始)?温度|热膨胀参考温度|stress[- ]free\s+(?:reference\s+)?temperature", _T),
    "left_voltage_v": (r"(?:左端(?:电极)?(?:电势|电位|电压)|left(?:\s+end)?\s+(?:potential|voltage))", _V),
    "right_voltage_v": (r"(?:右端(?:电极)?(?:电势|电位|电压)|right(?:\s+end)?\s+(?:potential|voltage))", _V),
    "left_temperature_k": (r"左端(?:温度|定温|维持)|left(?:\s+end)?\s+temperature", _T),
    "right_temperature_k": (r"右端(?:温度|定温|维持)|right(?:\s+end)?\s+temperature", _T),
}
_FACTORS = {
    "毫米": 1e-3, "mm": 1e-3, "微米": 1e-6, "μm": 1e-6, "um": 1e-6,
    "纳米": 1e-9, "nm": 1e-9, "millivolt": 1e-3, "mv": 1e-3, "毫伏": 1e-3,
    "gpa": 1e9, "mpa": 1e6, "kpa": 1e3, "ppm/k": 1e-6,
}


def _value(number, unit):
    number = number.translate(_SUPERSCRIPTS)
    parts = re.split(r"\s*[×x*]\s*10\s*\^?\s*", number, maxsplit=1, flags=re.I)
    numeric = float(parts[0])
    if len(parts) == 2:
        # Parse as scientific notation in one conversion, including large signed exponents.
        numeric = float(parts[0] + "e" + parts[1])
    unit = re.sub(r"\s+", "", unit).casefold()
    converted = numeric * _FACTORS.get(unit, 1.0)
    if unit in {"°c", "℃", "摄氏度"}:
        converted += 273.15
    if not math.isfinite(converted):
        raise ValueError("Non-finite value in explicit strip requirement")
    return converted


def _measurement(name, number, unit):
    suffix = rf"(?P<{unit}>{name})" if name else rf"(?P<{unit}>)"
    return rf"(?P<{number}>{_NUMBER})(?![\d.eE×])\s*{suffix}(?![A-Za-zμµ])"


def same_value(a, b):
    return type(a) in (int, float) and type(b) in (int, float) and math.isclose(a, b, rel_tol=1e-12, abs_tol=0.0)


_RANGE = r"(?:或者|或|到|至|or\b|to\b|[-–—~～])"
_PENDING = r"(?:仍待实测|尚待实测|待实测|待测|待定|未定|未测|未确认|无法确定|不能选定|不确定|to be measured|not yet known|undetermined|unconfirmed)"
_CONTEXT = re.compile(
    r"上一版|旧版|历史|原先|先前|previous|old revision|historical|"
    r"例如|比如|譬如|示例|仅供参考|for example|example|"
    r"备选|可选值|暂用|候选值|tentative|candidate value|"
    r"本版|新版|当前|实测值|已选定|current|this revision|selected",
    re.I,
)
_REJECT_CONTEXT = re.compile(
    r"上一版|旧版|历史|原先|先前|previous|old revision|historical|"
    r"例如|比如|譬如|示例|仅供参考|for example|example|"
    r"备选|可选值|暂用|候选值|tentative|candidate value", re.I
)


def _noncurrent(text, start):
    context = re.split(r"[。；;！？!?\n]", text[:start])[-1]
    markers = list(_CONTEXT.finditer(context))
    return bool(markers and _REJECT_CONTEXT.fullmatch(markers[-1].group()))


def extract(text, model, known):
    values, sources, conflicts, unresolved = {}, {}, set(), set()

    def remember(key, value, source):
        if key in values and not same_value(values[key], value):
            conflicts.add(key)
        else:
            values[key] = value
            sources.setdefault(key, []).append(source.strip())

    def accept(match, *keys):
        if _noncurrent(text, match.start()):
            return False
        if re.match(rf"\s*[，,、]?\s*(?:仍|尚|目前|暂时|当前)?\s*{_PENDING}", text[match.end():], re.I):
            unresolved.update(keys)
            return False
        return True

    for key, (label, unit) in _LABELS.items():
        prefix = rf"(?:{label}){_SEPARATOR}"
        measurement = _measurement(unit, "num", "unit")
        awaiting = rf"(?:{label})\s*(?:[（(](?:本版|当前)[）)])?{_SEPARATOR}(?:目前|仍|尚)?\s*{_PENDING}"
        if any(not _noncurrent(text, match.start()) for match in re.finditer(awaiting, text, re.I)):
            unresolved.add(key)
        # A dimensionally incompatible endpoint is still an unresolved range.
        generic_unit = r"[A-Za-zμµΩ℃°·⋅/*()²³^]+|微米|毫米|纳米|千克|米|秒|伏|摄氏度"
        optional_unit = rf"(?:\s*(?:{unit}|{generic_unit})(?![A-Za-zμµ]))?" if unit else ""
        range_prefix = rf"(?:{label})\s*(?:范围|区间|range\b)?{_SEPARATOR}"
        range_expression = rf"{range_prefix}{_NUMBER}{optional_unit}\s*{_RANGE}\s*{_NUMBER}{optional_unit}"
        if any(not _noncurrent(text, match.start()) for match in re.finditer(range_expression, text, re.I)):
            conflicts.add(key)
        for match in re.finditer(prefix + measurement, text, re.I):
            if accept(match, key):
                remember(key, _value(*match.group("num", "unit")), match.group())

    v1 = _measurement(_V, "num", "unit")
    v2 = _measurement(_V, "num2", "unit2")
    for match in re.finditer(
        rf"(?:两端(?:电极)?(?:分别施加|电势分别为|电位分别为|电压分别为)|end potentials are|left and right potentials are)"
        rf"{_SEPARATOR}{v1}\s*(?:和|与|、|and)\s*{v2}", text, re.I
    ):
        remember("left_voltage_v", _value(*match.group("num", "unit")), match.group())
        remember("right_voltage_v", _value(*match.group("num2", "unit2")), match.group())
    for match in re.finditer(
        rf"(?:跨条端电压|端电压差|端间电压差|voltage drop across the strip){_SEPARATOR}{v1}", text, re.I
    ):
        if not accept(match, "left_voltage_v", "right_voltage_v"):
            continue
        if re.match(rf"\s*{_RANGE}\s*{_NUMBER}", text[match.end():], re.I):
            conflicts.update(("left_voltage_v", "right_voltage_v"))
            continue
        delta = _value(*match.group("num", "unit"))
        left = values.get("left_voltage_v", known.get("left_voltage_v"))
        right = values.get("right_voltage_v", known.get("right_voltage_v"))
        if type(left) in (int, float) and type(right) in (int, float):
            if not same_value(right - left, delta):
                conflicts.update(("left_voltage_v", "right_voltage_v"))
        elif type(right) in (int, float):
            remember("left_voltage_v", right - delta, match.group())
        elif type(left) in (int, float):
            remember("right_voltage_v", left + delta, match.group())
        else:
            remember("left_voltage_v", 0.0, match.group() + " [电势参考采用左端 0 V]")
            remember("right_voltage_v", delta, match.group())

    t = _measurement(_T, "num", "unit")
    for match in re.finditer(
        rf"(?:两端(?:温度都维持|固定温度|恒温|定温|温度均为|温度为)|both ends(?:\s+are)?\s+(?:held at|at))"
        rf"{_SEPARATOR}{t}", text, re.I
    ):
        if not accept(match, "left_temperature_k", "right_temperature_k"):
            continue
        if re.match(rf"\s*{_RANGE}\s*{_NUMBER}", text[match.end():], re.I):
            conflicts.update(("left_temperature_k", "right_temperature_k"))
            continue
        temperature = _value(*match.group("num", "unit"))
        remember("left_temperature_k", temperature, match.group())
        remember("right_temperature_k", temperature, match.group())

    for match in re.finditer(r"(?<![\d.,])(\d+)\s*(?:个\s*)?(?:(?:等长|均匀|线性|轴向)\s*)*(?:单元|(?:linear\s+)?elements\b)", text, re.I):
        if accept(match, "elements"):
            remember("elements", int(match[1]), match.group())

    checks = (
        r"均匀|等截面|uniform|constant cross.section",
        r"矩形|rectangular",
        r"直流|steady\s+dc|\bdc\b",
        r"稳态|steady",
        r"恒物性|常物性|忽略[^。；]{0,32}物性温变|constant (?:material )?properties",
        r"侧面绝热|其余表面绝热|lateral(?:ly)? adiabatic|adiabatic sides",
        r"左端(?:只限制轴向位移|轴向固定)|左端固定|left(?: end)?(?: is)? axially fixed",
        r"右端轴向自由|右端[^。；，]{0,8}自由滑动|right(?: end)?(?: is)? axially free",
        r"小变形|小应变|small (?:strain|deformation)",
        r"一维|(?<![a-z0-9])1D\b|one.dimensional",
        r"忽略[^。；]{0,45}接触电阻|无接触电阻|(?:ignore|no|neglect)\s+(?:the\s+)?contact resistance",
    )
    evidence = [re.search(pattern, text, re.I) for pattern in checks]
    if all(evidence):
        values["model"] = model
        sources["model"] = [match.group() for match in evidence]
    for key in conflicts | unresolved:
        values.pop(key, None)
        sources.pop(key, None)
    return values, sources, conflicts, unresolved
