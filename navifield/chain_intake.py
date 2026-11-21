"""Discover reviewed equipment chains from a local description or structured physics terms."""

import json
import re
import unicodedata
from decimal import Decimal
from math import isfinite

from . import device_chains, paper_cases, strip_control, thermal_intake


def _terms(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _terms(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _terms(item)


def _normalize(text):
    return re.sub(r"[\s_–-]+", " ", unicodedata.normalize("NFKC", text).casefold())


def _mentions(term, text):
    term = _normalize(term)
    pattern = re.escape(term)
    if term.isascii():
        pattern = r"(?<![a-z0-9])" + pattern + r"(?![a-z0-9])"
    return any(not re.search(r"(?:不关心|不评估|不讨论|不校核|不研究|不分析|不关注|不要求|无需考虑|非|不|没|没有|不是|并非|不用|不使用|无需|不清楚|未知|未定|不需要|不采用|不计算|不比较|不比较测温|并非接触式|不是接触式|without |not )\s*$", text[:m.start()])
               for m in re.finditer(pattern, text))


def _pattern_mentions(pattern, text):
    return any(not re.search(r"(?:没有|无|不关心|不评估|不要求|排除|不算|不分析|不考虑|无需|不计算|不要|不是|并非|非|不)\s*$",
                             text[:match.start()])
               for match in re.finditer(pattern, text, re.I))


def _signal_applies(text, rule):
    """In a mixed-instrument request, distinguish the transducer from a reference or parasitic."""
    if not rule or not any(_mentions(word, text) for word in rule["competing"]):
        return True
    clauses = re.split(r"[。；;，,！？!?\n]", text)
    signal_drives_readout = False
    reference_sensor = False
    has_sensor_signal = False
    for clause in clauses:
        if any(_mentions(word, clause) for word in rule["signal"]):
            incidental = any(_mentions(word, clause) for word in rule["incidental"])
            has_sensor_signal |= not incidental
            if not incidental and any(_mentions(word, clause) for word in rule["readout"]):
                signal_drives_readout = True
        if any(_mentions(word, clause) for word in rule["competing"]):
            reference_sensor |= any(_mentions(word, clause) for word in rule["reference"])
    return signal_drives_readout or (reference_sensor and has_sensor_signal)



_MEASURED_PAIR = re.compile(
    r"(?:(?:进出口|入口和出口|上游和下游|上游与下游)(?:的)?|(?:管路)?两(?:端|头|边)(?:的)?)\s*"
    r"(全压|总压|静压)(?:传感器|压力表|表头)?(?:已经|已)?(?:依次|分别)?(?:是|为|读到|测得|等于|[:：])?\s*"
    r"([+\-−]?\d+(?:\.\d+)?)\s*(kPa|Pa|千帕|帕)?\s*(?:和|与|、|及)\s*"
    r"([+\-−]?\d+(?:\.\d+)?)\s*(kPa|Pa|千帕|帕)(?![A-Za-z])",
    re.I,
)


def measured_pressure_difference(request):
    """Return pressure subtraction or an uncertainty for explicitly measured data."""
    text = unicodedata.normalize("NFKC", " ".join(
        str(request.get(key, "")) for key in ("original_request", "description")
    ))
    comparing = re.search(
        r"能.{0,8}比较|比较.{0,8}(?:吗|是否)|能.{0,5}相减|能.{0,5}作差|"
        r"(?:能否|能不能).{0,20}(?:算|计算|用于)|能.{0,8}(?:说|说明|判断|认为).{0,14}(?:压降|压损|变化)",
        text, re.I,
    )
    different_conditions = re.search(
        r"不同时刻|不同工况|不同转速|不同流量|不是同.{0,4}工况|时刻不一样|不同批次", text, re.I
    ) or (re.search(r"停机|停运|关机", text, re.I) and re.search(r"(?:满|带|有)负荷|运行时|运转时", text, re.I))
    different_measurements = re.search(r"静压(?:表|孔|读数)", text, re.I) and re.search(r"皮托管|迎风探头|总压(?:表|读数)", text, re.I)
    if comparing and (different_conditions or different_measurements) and re.search(r"记录|台账|读数|表数|量完|实测|测量", text, re.I):
        return {"status": "needs_comparable_measurements", "missing": ["common_operating_point_and_pressure_datum"],
                "reason": "在同工况和同一时刻或统计窗取得两个测站读数，确认仪表测量的是同一种压力并采用共同基准，才能比较压降。"}
    if not re.search(r"只(?:用|算|做差)|不建模|别建模|别建模型|不(?:起|用)模拟|不仿真|别起模拟|no simulation",
                     text, re.I):
        return None
    observations = list(_MEASURED_PAIR.finditer(text))
    if len(observations) != 1 or re.search(r"例如|示例|备选|待测|未确定|基准不一致|不同(?:的)?零点|different datum", text, re.I):
        return None
    same_datum = bool(re.search(r"同一(?:个)?(?:零点|基准)|都按一个零点|同基准|same datum", text, re.I))
    subtract_as_given = bool(re.search(r"前一个.{0,5}减.{0,5}后一个|第一个.{0,5}减.{0,5}第二个|只.{0,8}算术", text, re.I))
    if not same_datum and not subtract_as_given:
        return {"status": "needs_comparable_measurements", "missing": ["common_operating_point_and_pressure_datum"],
                "reason": "需要确认参考基准。也可明确要求纯算术差。"}

    quantity, upstream, first_unit, downstream, second_unit = observations[0].groups()
    def to_pa(number, unit):
        value = Decimal(number.replace("−", "-"))
        return value * 1000 if unit.casefold() in {"kpa", "千帕"} else value

    up = to_pa(upstream, first_unit or second_unit)
    down = to_pa(downstream, second_unit)
    numbers = (float(up), float(down), float(up - down))
    if not all(map(isfinite, numbers)):
        return None
    return {"status": "calculated_from_measured_values" if same_datum else "arithmetic_difference_only", "unit": "Pa",
            "pressure_definition": ("static_difference" if quantity == "静压" else "total_loss") if same_datum else "numeric_difference",
            "upstream_Pa": numbers[0], "downstream_Pa": numbers[1], "difference_Pa": numbers[2],
            "direction": "upstream_minus_downstream" if same_datum else "first_minus_second",
            "same_datum_confirmed": same_datum}


_DUCT_NUMBER = r"[+\-]?\d+(?:\.\d+)?(?:[Ee][+\-]?\d+)?"
_DUCT_FLOW_UNITS = {
    "m3/s": ("volume_flow_m3_s", Decimal(1)), "m3/h": ("volume_flow_m3_s", Decimal(1) / 3600),
    "m3/min": ("volume_flow_m3_s", Decimal(1) / 60), "cfm": ("volume_flow_m3_s", Decimal("0.0004719474432")),
    "l/min": ("volume_flow_m3_s", Decimal(1) / 60000), "l/s": ("volume_flow_m3_s", Decimal(".001")),
    "kg/s": ("mass_flow_kg_s", Decimal(1)), "kg/h": ("mass_flow_kg_s", Decimal(1) / 3600),
}


_DUCT_NONOPERATING_FLOW = re.compile(
    r"\b(?:(?:anr|scfm)\b|[ns]m\^?3\s*/)|标方|标[准况态](?:体积|送风|进风|入口|抽气)?(?:风量|流量|量)?"
    r"|额定(?:体积|送风|进风|入口|抽气)?(?:风量|流量|量)?",
    re.I,
)


_DUCT_FLOW_BACKREFERENCE = re.compile(
    r"(?:上述|上面|上文|前述|前面|前一(?:个|项)|该|此|这个|那个)"
    r"(?:的)?(?:这个|那个)?(?:数(?:字|值)?|值|读数|流量|风量|量)?"
)
_DUCT_ACTUAL_FLOW_UNKNOWN = re.compile(
    r"(?:现场|实际|实测|工况)(?:的)?(?:气体)?(?:体积)?(?:流量|风量)"
    r"[^。；;，,\n]{0,12}(?:没测|未测|未知|不明|待测|尚无|没有|未(?:知|获|确认)|未折算)"
)
_DUCT_EXPLICIT_ACTUAL_FLOW = re.compile(r"(?:实测|实际|工况)(?:的)?(?:体积)?(?:流量|风量)")
_INTERNAL_FORCED_CHT = "internally_heated_forced_cht"


def _duct_actual_flow_text(raw):
    # Attach an explicit backward qualification to the preceding numeric statement.
    # If actual flow is explicitly unknown, only independent, qualified values count.
    clauses = re.split(r"[。；;，,、\n]|(?:而|但|另)(?=(?:现场|实际|实测|风机|入口))", raw)
    actual_unknown = any(_DUCT_ACTUAL_FLOW_UNKNOWN.search(clause) for clause in clauses)
    eligible = []
    last_flow_statement = None
    for clause in clauses:
        text = unicodedata.normalize("NFKC", clause)
        if _DUCT_NONOPERATING_FLOW.search(text):
            if last_flow_statement is not None and _DUCT_FLOW_BACKREFERENCE.match(text.lstrip()):
                eligible[last_flow_statement] = ""
            continue
        if actual_unknown and not _DUCT_EXPLICIT_ACTUAL_FLOW.search(text):
            continue
        if re.search(_DUCT_NUMBER, text) and re.search(r"流量|风量|抽量|m\^?3\s*/|cfm|立方|每秒", text):
            last_flow_statement = len(eligible)
        eligible.append(clause)
    return "\n".join(eligible)


def _duct_operating_inputs(raw, known):
    text = unicodedata.normalize("NFKC", raw).casefold()
    flow_text = _duct_actual_flow_text(text)
    values = {}

    def record(key, number, scale=Decimal(1)):
        value = float(Decimal(number) * scale)
        if isfinite(value) and value > 0:
            values.setdefault(key, set()).add(value)

    if "inlet_flow_rate" in known:
        for match in re.finditer(
            rf"(?:入口(?:风量|流量|体积流量|质量流量)?|(?:实际|实测)(?:体积)?(?:风量|流量)|送风量|抽量|进风量|给定(?:风量|流量))"
            rf"[^。；;，,\n\d]{{0,10}}({_DUCT_NUMBER})\s*"
            r"(m\^?3\s*/\s*(?:min|h|s)|(?:kg|l)\s*/\s*(?:min|h|s)|cfm)\b", flow_text
        ):
            unit = re.sub(r"\s|\^", "", match[2])
            if unit in _DUCT_FLOW_UNITS:
                key, scale = _DUCT_FLOW_UNITS[unit]
                record(key, match[1], scale)
        for match in re.finditer(
            rf"(?:入口(?:风量|流量|体积流量)|(?:实际|实测)(?:体积)?(?:风量|流量)|送风量|抽量|进风量|给定(?:风量|流量))"
            rf"[^。；;，,\n\d]{{0,10}}({_DUCT_NUMBER})\s*立方米\s*(?:每|/)\s*(小时|秒|分钟)", flow_text
        ):
            record("volume_flow_m3_s", match[1], Decimal(1) / {"小时": 3600, "分钟": 60, "秒": 1}[match[2]])
        for match in re.finditer(
            rf"(?:抽量|风量|流量|送风量)[^。；，\n]{{0,8}}每(小时|秒|分钟)\s*({_DUCT_NUMBER})\s*立方(?:米)?", flow_text
        ):
            record("volume_flow_m3_s", match[2], Decimal(1) / {"小时": 3600, "分钟": 60, "秒": 1}[match[1]])
        for match in re.finditer(
            rf"(?:轴向|入口(?:平均|轴向)?(?:流速|速度)|平均入口速度)\D{{0,5}}({_DUCT_NUMBER})\s*(?:米每秒|m\s*/\s*s\b)",
            flow_text,
        ):
            record("inlet_speed_m_s", match[1])
    if "fluid_density" in known:
        for match in re.finditer(rf"({_DUCT_NUMBER})\s*(?:千克每立方米|kg\s*/\s*m3)", text):
            record("density_kg_m3", match[1])
    parsed = {key: next(iter(observations)) for key, observations in values.items() if len(observations) == 1}
    conflicting = {key for key, observations in values.items() if len(observations) > 1}
    return parsed, conflicting


def _internally_heated_cht_inputs(raw):
    """Read explicitly scoped air drive and heating values; leave project closures local."""
    text = unicodedata.normalize("NFKC", raw).casefold()
    flow, conflicts = _duct_operating_inputs(raw, {"inlet_flow_rate"})
    air = {k: number for k, number in flow.items() if k in {"volume_flow_m3_s", "mass_flow_kg_s"}}
    curve = _pattern_mentions(
        r"(?:风机|鼓风机|送风机)[^。；，,\n]{0,20}(?:压流|p\s*[-–—]\s*q|性能曲线|压力[^。；，,\n]{0,5}流量曲线)|"
        r"(?:压升|压差|全压|静压)[^。；，,\n]{0,5}(?:风量|流量)[^。；，,\n]{0,5}(?:曲线|表)", raw,
    )
    measured_flow = any(re.search(
        r"(?:实测|已测)(?:的)?(?:入口|实际|安装态|供风)?(?:风量|流量)|"
        r"(?:供风量|入口流量|风量|流量)[^。；，,\n]{0,12}(?:测过|测好|测得|已测|已记录)", clause,
    ) and not re.search(r"(?:额定|自由送风|未测|尚未|还未|没测|没有|未知|待测|待给)", clause)
        for clause in re.split(r"[。；，,;\n]", raw))
    air_drive = None
    if not conflicts:
        if curve and not (air or measured_flow):
            air_drive = "fan_curve"
        elif (air or measured_flow) and not curve:
            air_drive = "prescribed_flow"

    heating = re.findall(
        rf"(?:生热|发热|产热|热源(?:强度|密度)?|(?:热|耗散)功率|热耗散)"
        rf"(?:率|量|功率|密度|为|是|共|合计|[:=]|\s){{0,10}}({_DUCT_NUMBER})\s*"
        r"(kw|w|千瓦|瓦)\s*(?:\s*(/)\s*m(?:\^?3)\b)?", text,
    )
    values = set()
    for number, unit, per_volume in heating:
        if re.search(rf"{re.escape(number)}\s*{unit}\s*/\s*(?:m\^?2|平方米)", text):
            continue
        number = float(Decimal(number) * (1000 if unit in {"kw", "千瓦"} else 1))
        if isfinite(number) and number >= 0:
            values.add(("volumetric_heat_generation_W_m3" if per_volume else "heat_generation_power_W", number))
    recorded_power = any(re.search(
        r"(?:每(?:个|块|件|组)|各(?:区|块|件|自)|按区域)[^。；，,\n]{0,16}(?:热功率|发热|耗散|损耗|功率)"
        r"[^。；，,\n]{0,16}(?:随时间|时序|功率曲线|时程)[^。；，,\n]{0,8}(?:记录|实测|已给)|"
        r"(?:已给|已测|记录了)[^。；，,\n]{0,12}(?:发热|热功率|耗散功率|损耗)[^。；，,\n]{0,12}(?:时序|随时间|时程)", clause,
    ) and not re.search(r"(?:未测|没有|待测|尚未|未知)", clause)
        for clause in re.split(r"[。；，,;\n]", raw))
    if recorded_power and not values:
        return air, air_drive, "provided_power_histories"
    if len(values) != 1:
        return air, air_drive, None
    key, value = values.pop()
    return {**air, key: value}, air_drive, (
        "volumetric_generation" if key == "volumetric_heat_generation_W_m3" else "total_thermal_power"
    )


def _single_measured_capacitance(raw):
    text = unicodedata.normalize("NFKC", raw)
    # A range or alternatives are not a single measured value.
    if re.search(r"\d\s*(?:pF|皮法(?:拉)?)?\s*(?:或|到|至|[-–—~～])\s*\d", text, re.I):
        return None
    # Keep only clauses that describe an observation or a numeric input to inference.
    clauses = re.split(r"[。；;，,\n]", text)
    supplied = "\n".join(clause for clause in clauses if re.search(
        r"读(?:到|出|数)|录得|记录|报(?:出|告)?|测(?:到|得|量|值)|采集|显示|已知|给定|(?:由|凭|从|根据)\s*\d", clause
    ))
    readings = re.findall(rf"(?<![\d.])({_DUCT_NUMBER})\s*(?:pF\b|皮法(?:拉)?)", supplied, re.I)
    values = {Decimal(value) for value in readings}
    if len(values) == 1:
        value = float(values.pop() * Decimal("1e-12"))
        if isfinite(value) and value > 0:
            return value
    return None


def _single_indentation_displacement(raw):
    text = unicodedata.normalize("NFKC", raw)
    unit = r"毫米|厘米|米|mm\b|cm\b|m\b"
    label = r"(?:压入(?:位移|行程|深度)?|(?:压头|压缩)(?:位移|行程|深度))"
    assign = r"\s*(?:(?:为|到|至|达|设为|定为)\s*)?"
    if re.search(rf"{label}{assign}{_DUCT_NUMBER}\s*(?:{unit})?\s*(?:或|到|至|[-–—~～])\s*{_DUCT_NUMBER}", text, re.I):
        return None
    scale = {"毫米": Decimal(".001"), "mm": Decimal(".001"), "厘米": Decimal(".01"),
             "cm": Decimal(".01"), "米": Decimal(1), "m": Decimal(1)}
    values = {Decimal(value) * scale[length_unit.casefold()]
              for value, length_unit in re.findall(rf"{label}{assign}({_DUCT_NUMBER})\s*({unit})", text, re.I)}
    if len(values) == 1:
        result = float(values.pop())
        if isfinite(result) and result > 0:
            return result
    return None


_PRESSURE_SCALE = {"mpa": Decimal(1000000), "kpa": Decimal(1000), "pa": Decimal(1),
                   "兆帕": Decimal(1000000), "千帕": Decimal(1000), "帕": Decimal(1)}
_PRESSURE_UNIT = r"MPa|kPa|Pa|兆帕|千帕|帕"
_PRESSURE_NUMBER = r"[+\-]?\s*\d+(?:\.\d+)?(?:[Ee][+\-]?\d+)?"
_PRESSURE_ASSIGN = r"\s*(?:(?:为|是|=|:|已经稳在|稳在|保持在|保持|达到|达)\s*)?"
_CHAMBER_PRESSURE_LOCATION = r"腔内(?!外)|腔里|内腔|腔压|内压|气室|气腔|囊内|指腔"
_EXTERIOR_PRESSURE_LOCATION = r"腔外|外界|环境|周围|外部|试验箱|测试箱|(?:外面|外侧)(?:的(?:检验|试验)?舱(?:内|里|中))?"
_PRESSURE_DIFFERENCE = r"腔(?:内外|里外)(?:的)?(?:净压差|压差|差压|压力差)|内外(?:净压差|压差|差压|压力差)|相对外界压差"
_PNEUMATIC_STATIC = "soft_pneumatic_static_actuator"
_PNEUMATIC_TRANSIENT = "soft_pneumatic_transient_gas_structure"
_PNEUMATIC_COMPLIANT = r"(?:软|柔性|硅胶|橡胶|弹性)[^。；，,\n]{0,6}(?:夹指|手指|气(?:腔|囊|室)|波纹|执行器|薄膜)"
_PNEUMATIC_ANGLE = r"(?:端盖|末端|尖端|前端|自由端|标记)[^。；，,\n]{0,36}(?:夹角|转(?:了|过)?多少度|弯曲角|转角|转了几度)"
_PNEUMATIC_SENSOR_LOAD = r"(?:测力板|定位板|止挡|挡块|限位块|接触件)[^。；]{0,15}(?:测力计|(?:单轴)?力传感器|负荷传感器)[^。；]{0,22}(?:力|读数|增量|载荷)"
_PNEUMATIC_EQUAL_REFERENCE = r"等压|同压|(?:内外|两表)[^。；，,\n]{0,6}(?:相等|一致)|(?:内外)?压差(?:为|是)零"
_PNEUMATIC_PRESSURE_HISTORY = r"(?:腔压|(?:腔内|腔里|内腔|气腔|袋内|囊内)[^。；，,\n]{0,12}(?:压力|压强|绝压))[^。；，,\n]{0,8}(?:曲线|时序|历程|随时间|时间响应)"


def _specified_pneumatic_pressure(raw, *, upstream=False):
    text = unicodedata.normalize("NFKC", raw).replace("−", "-")
    text = re.sub(r"负\s*(?=\d)", "-", text)
    text = re.sub(rf"零(?=\s*(?:{_PRESSURE_UNIT}))", "0", text, flags=re.I)
    number_unit = rf"({_PRESSURE_NUMBER})\s*({_PRESSURE_UNIT})"
    separator = r"\s*(?:至|到|或者?|[-–—~±])\s*"
    if re.search(
        rf"{number_unit}(?:\s*(?:表压|绝压))?{separator}{_PRESSURE_NUMBER}|"
        rf"{_PRESSURE_NUMBER}{separator}{number_unit}",
        text, re.I,
    ):
        return {}

    observations = {}
    locations = (("upstream", r"阀前|阀上游|供气端"),) if upstream else (
        ("chamber", _CHAMBER_PRESSURE_LOCATION), ("exterior", _EXTERIOR_PRESSURE_LOCATION),
    )
    # A location shorthand describes the reading's owner, independently of its pressure basis.
    for place, location in locations:
        prefix = (
            rf"(?:{location})(?:侧|处|的|所测|稳态时|目前|压力|静压)*\s*"
            r"(?:(?:计|表(?!压)|传感器)\s*)?(?:(?:读数|读出|读到|读|显示|记录)\s*)?"
            rf"(?:(?:缓慢|慢)?(?:充气|充|抽气|抽|供气|加压)\s*(?:至|到|为)?\s*)?"
            rf"{_PRESSURE_ASSIGN}"
        )
        for basis, label in (("gauge", r"表压"), ("absolute", r"绝压|绝对压(?:力|强)?")):
            if upstream and basis != "gauge":
                continue
            key = f"{place}_{basis}_pressure_Pa"
            patterns = [
                rf"{prefix}(?:{label}){_PRESSURE_ASSIGN}{number_unit}",
                rf"{prefix}{number_unit}\s*(?:的)?(?:{label})",
            ]
            if basis == "absolute" and re.search(
                r"内外[^。；，,\n]{0,10}(?:绝压|绝对压力)(?:表|计|仪|传感器)", text,
            ):
                patterns.append(rf"{prefix}(?<![\d.+\-]){number_unit}(?!\s*(?:表压|压差))")
            for pattern in patterns:
                for value, unit in re.findall(pattern, text, re.I):
                    observations.setdefault(key, set()).add(
                        Decimal(value.replace(" ", "")) * _PRESSURE_SCALE[unit.casefold()]
                    )
            if re.search(rf"{prefix}(?:{label}){_PRESSURE_ASSIGN}(?:零|0)(?![\d.])", text, re.I):
                observations.setdefault(key, set()).add(Decimal(0))

    differential = "chamber_pressure_difference_Pa"
    if not upstream:
        for value, unit in re.findall(rf"(?:{_PRESSURE_DIFFERENCE}){_PRESSURE_ASSIGN}{number_unit}", text, re.I):
            observations.setdefault(differential, set()).add(
                Decimal(value.replace(" ", "")) * _PRESSURE_SCALE[unit.casefold()]
            )
        if re.search(rf"(?:{_PRESSURE_DIFFERENCE}){_PRESSURE_ASSIGN}(?:零|0)(?![\d.])", text):
            observations.setdefault(differential, set()).add(Decimal(0))
        for direction, value, unit in re.findall(
            rf"(?:{_CHAMBER_PRESSURE_LOCATION})(?:的?(?:压强|压力))?\s*(?:比|较)"
            rf"(?:{_EXTERIOR_PRESSURE_LOCATION})[^。；，,\n+\-\d]{{0,12}}?(高|低)\s*{number_unit}", text, re.I,
        ):
            magnitude = Decimal(value.strip())
            if magnitude >= 0:
                delta = magnitude * _PRESSURE_SCALE[unit.casefold()]
                observations.setdefault(differential, set()).add(delta if direction == "高" else -delta)
        if re.search(
            rf"(?:{_EXTERIOR_PRESSURE_LOCATION})\s*(?:为|是|就是|即|作为)\s*(?:表压零点|零表压)",
            text, re.I,
        ):
            observations.setdefault("exterior_gauge_pressure_Pa", set()).add(Decimal(0))

    for key in ("chamber_absolute_pressure_Pa", "exterior_absolute_pressure_Pa"):
        if any(value < 0 for value in observations.get(key, ())):
            return {}
    if any(len(values) != 1 for values in observations.values()):
        return {}

    pressures = {key: next(iter(values)) for key, values in observations.items()}
    chamber_abs = "chamber_absolute_pressure_Pa"
    exterior_abs = "exterior_absolute_pressure_Pa"
    if chamber_abs in pressures and exterior_abs in pressures:
        if re.search(r"基准不一致|不是同(?:一|个)基准|不同(?:的)?零点|不同时刻|不同工况", text):
            return {key: float(value) for key, value in pressures.items() if key != differential and isfinite(float(value))}
        delta = pressures[chamber_abs] - pressures[exterior_abs]
        if differential in pressures and pressures[differential] != delta:
            return {}
        pressures[differential] = delta
    return {key: float(value) for key, value in pressures.items() if isfinite(float(value))}


def _observed_chamber_traces(raw):
    """Match known chamber inlet mass-flow and chamber volume histories independently."""
    inlet = re.search(r"(?:气囊|气腔|囊|腔体|腔室)(?:的)?入口|入口(?:实测|测得)?质量流", raw)
    if not inlet:
        return False
    observed = r"实测|测得|量测|已(?:经|有|录|测|随)|录下|记录下|记录了|同步算出|已给定|给定"
    clauses = re.split(r"[。；;，,！？!?\n]", raw)
    measured = [clause for clause in clauses if re.search(observed, clause)
                and not re.search(r"未(?:录|测|给)|没有记录|未同步|待测|尚无|如果|假设", clause)]
    flow = any(re.search(r"质量(?:流量|流率)|质量进流", clause) and re.search(
        r"时间|时序|曲线|脉冲|[mṁ]\s*[\(（]\s*t\s*[\)）]", clause, re.I,
    ) for clause in measured)
    volume = any(re.search(r"(?:容积|体积)", clause) and re.search(
        r"时间|时序|曲线|[vV]\s*[\(（]\s*t\s*[\)）]", clause,
    ) for clause in measured)
    return flow and volume


def _pneumatic_static_signals(raw, pressures):
    """Return extra mechanical goals only when supported by compliant-device context."""
    if not re.search(_PNEUMATIC_COMPLIANT, raw):
        return set()
    goals = {goal for goal, pattern in (
        ("free_bending_angle", _PNEUMATIC_ANGLE),
        ("blocked_tip_force", _PNEUMATIC_SENSOR_LOAD),
    ) if _pattern_mentions(pattern, raw)}
    pressure_evidence = pressures or re.search(r"充气|抽气|内压|压差|腔压|绝压表", raw)
    return goals if pressure_evidence else set()


def _specified_trajectory_window(raw):
    text = unicodedata.normalize("NFKC", raw)
    units = {"ms": Decimal(".001"), "毫秒": Decimal(".001"), "s": Decimal(1), "秒": Decimal(1)}
    spans = re.findall(rf"(?:前|头|最初|起始|(?<![\d.])0\s*(?:毫秒|ms|秒|s)?\s*(?:到|至|[-–—~]))\s*"
                       rf"({_DUCT_NUMBER})\s*(毫秒|ms\b|秒|s\b)", text, re.I)
    values = {float(Decimal(number) * units[unit.casefold()]) for number, unit in spans}
    if len(values) == 1:
        value = values.pop()
        if isfinite(value) and value > 0:
            return value
    return None


def _specified_pulse_window(raw):
    units = {"ms": .001, "毫秒": .001, "s": 1., "秒": 1.}
    durations = {float(number) * units[unit.casefold()]
                 for number, unit in re.findall(
                     rf"(?<![\d.])({_DUCT_NUMBER})\s*(毫秒|ms\b|秒|s\b)\s*(?:的)?(?:进气|充气|补气)脉冲",
                     unicodedata.normalize("NFKC", raw), re.I)}
    if len(durations) == 1:
        duration = durations.pop()
        if isfinite(duration) and duration > 0:
            return duration
    return None


def discover(db, request, solver=None, version=None, *, strip_review=None):
    # Keep descriptions, customer identifiers and numerical inputs local. All returned
    # content and research identifiers originate in the reviewed catalog.
    fields = ("original_request", "description", "goals", "mechanisms", "conditions",
              "operating_conditions", "materials", "transfers", "stages", "boundaries")
    phrases = (s for key in fields for s in _terms(request.get(key)))
    raw = "\n".join(re.sub(r"(?:仅供参考|背景示例)[：:][^。；;\n]*", "", s) for s in phrases)
    text = _normalize(raw)
    static_pressure = _specified_pneumatic_pressure(raw)
    static_signals = _pneumatic_static_signals(raw, static_pressure)
    measured_gas_inputs = (
        _pattern_mentions(_PNEUMATIC_PRESSURE_HISTORY, raw) and _observed_chamber_traces(raw)
    )
    dois = paper_cases.explicit_dois(raw)
    plans = []
    for cid, rules in db.execute(
        "SELECT chain,json_extract(body,'$.intake') FROM chain_specs "
        "WHERE json_type(body,'$.intake')='object' ORDER BY chain"
    ):
        rules = json.loads(rules)
        conflicts = set(request.get("operating_conditions", [])) & set(rules.get("incompatible_conditions", []))
        if conflicts or any(_mentions(term, text) for term in rules.get("exclude", [])):
            continue
        if _pattern_mentions(rules.get("exclude_pattern", r"(?!)"), raw):
            continue
        if "include_pattern" in rules and not _pattern_mentions(rules["include_pattern"], raw):
            continue
        if not _signal_applies(text, rules.get("signal_roles")):
            continue
        adapter = rules.get("adapter")
        if adapter == "confirmed_axial_strip":
            if strip_review is None:
                strip_review = strip_control.review(request)
            if not strip_review or strip_review["next_action"] in {"review_applicability", "resolve_conflicts"}:
                continue
        pattern_groups = rules.get("pattern_groups", {})
        mechanism_match = (
            (cid == _PNEUMATIC_STATIC and bool(static_signals))
            or (cid == _PNEUMATIC_TRANSIENT and measured_gas_inputs)
        )
        if not mechanism_match and not all(any(_mentions(term, text) for term in group) or
                                           _pattern_mentions(pattern_groups.get(str(index), r"(?!)"), raw)
                                           for index, group in enumerate(rules.get("all_of", []))):
            continue
        for plan in device_chains.inventory(db, solver, version, cid):
            if plan["implementation"] not in ("configured", "custom"):
                continue
            stated = dict(request.get("conditions", {}))
            for key, alternatives in rules.get("condition_evidence", {}).items():
                patterns = rules.get("condition_patterns", {}).get(key, {})
                inferred = {value for value, terms in alternatives.items()
                            if any(_mentions(term, text) for term in terms)
                            or _pattern_mentions(patterns.get(value, r"(?!)"), raw)}
                if len(inferred) == 1 and key not in stated:
                    stated[key] = inferred.pop()
            if any(key in stated and stated[key] not in values for key, values in plan["conditions"].items()):
                continue
            if cid == "steady_extended_surface_conduction":
                # Infer a declared boundary only when its clause does not negate it.
                clauses = re.split(r"[。；;，,！？!?\n]", raw)
                positive = lambda pattern: any(_pattern_mentions(pattern, clause) and not re.search(
                    r"(?:不|非|不能|无法|未|并非|不是)[^。；，,]{0,8}(?:绝热|隔热|辐射|换热)", clause)
                    for clause in clauses)
                if positive(r"(?:端|尖)[^。；，,\n]{0,12}(?:绝热|隔热)|(?:绝热|隔热)[^。；，,\n]{0,8}(?:端|尖)"):
                    stated.setdefault("tip_boundary", "adiabatic")
                if positive(r"(?:侧面|侧壁|表面)[^。；，,\n]{0,20}(?:综合|等效)[^。；，,\n]{0,8}(?:换热|散热)|(?:综合|等效)[^。；，,\n]{0,8}换热系数"):
                    stated.setdefault("side_exchange", "combined")
                if positive(r"(?:辐射)[^。；，,\n]{0,12}(?:算进|计入|包含|含在)|(?:包含|含有|包括)[^。；，,\n]{0,8}辐射"):
                    stated.setdefault("radiation", "included")
                if positive(r"(?:根部|基部|底部|根端)[^。；，,\n]{0,10}温度"):
                    stated.setdefault("base_boundary", "temperature")
            known = set(request.get("known_inputs", [])) | set(request.get("known_quantities", []))
            ambiguous = rules.get("ambiguous_input_evidence", {})
            for key, pattern in rules.get("input_evidence", {}).items():
                evidence = _duct_actual_flow_text(raw) if cid == "forced_duct_pressure_drop" and key == "inlet_flow_rate" else raw
                if re.search(pattern, evidence, re.I) and not (key in ambiguous and re.search(ambiguous[key], evidence, re.I)):
                    known.add(key)
            operating_inputs = thermal_intake.known_inputs(cid, raw)
            if cid == thermal_intake.HEATED_DUCT:
                duct, _ = _duct_operating_inputs(raw, {"inlet_flow_rate"})
                measured_flow = {k: v for k, v in duct.items() if k in {"volume_flow_m3_s", "mass_flow_kg_s"}}
                operating_inputs = thermal_intake.heated_duct_inputs(raw, stated, measured_flow)
                re_dh = operating_inputs.get("reynolds_Dh")
                if re_dh is not None and re_dh <= 1000:
                    stated.setdefault("momentum_regime", "laminar_candidate")
                if stated.get("sampling") == "manifold_static_taps":
                    stated.setdefault("pressure_definition", "static_difference")
                    stated.setdefault("local_losses", "within_stations")
            if cid == _INTERNAL_FORCED_CHT:
                operating_inputs, air_drive, heat_input = _internally_heated_cht_inputs(raw)
                if re.search(r"(?:忽略|不计|不含|不考虑|不算)[^。；，,\n]{0,5}辐射", raw):
                    if request.get("conditions", {}).get("radiation") is None:
                        stated["radiation"] = "neglected"
                if air_drive:
                    stated.setdefault("air_drive", air_drive)
                if heat_input:
                    stated.setdefault("heat_input", heat_input)
                enclosure = _pattern_mentions(r"(?:封闭|密闭)[^。；，,\n]{0,4}(?:罩|壳|箱|柜|回路|腔)|(?:罩|壳|箱|柜|腔)[^。；，,\n]{0,4}(?:封闭|密闭)", raw)
                if enclosure and _pattern_mentions(r"(?:内置|罩内|箱内|内部)[^。；，,\n]{0,12}(?:风机|回流|循环)|(?:空气|气体)[^。；，,\n]{0,6}(?:循环|回流)", raw):
                    stated.setdefault("air_circuit", "closed_recirculation")
            if cid == "forced_duct_pressure_drop":
                operating_inputs, conflicting = _duct_operating_inputs(raw, known)
                if conflicting & {"volume_flow_m3_s", "mass_flow_kg_s", "inlet_speed_m_s"}:
                    known.discard("inlet_flow_rate")
                if "density_kg_m3" in conflicting:
                    known.discard("fluid_density")
            if cid == "tactile_pad_inverse":
                measured = _single_measured_capacitance(raw)
                if measured is not None:
                    known.add("measured_capacitance")
                    operating_inputs["measured_capacitance_F"] = measured
            if cid == "tactile_pad_forward":
                indentation = _single_indentation_displacement(raw)
                if indentation is not None:
                    operating_inputs["indentation_displacement_m"] = indentation
            if cid == _PNEUMATIC_STATIC:
                operating_inputs.update(static_pressure)
            if cid == _PNEUMATIC_TRANSIENT:
                operating_inputs.update(_specified_pneumatic_pressure(raw, upstream=True))
                window = _specified_trajectory_window(raw)
                if window is None and measured_gas_inputs:
                    window = _specified_pulse_window(raw)
                if window is not None:
                    operating_inputs["trajectory_window_s"] = window
            excitation = rules.get("excitation_evidence", {})
            # Interpret explicit drive phrases and values only for the matched device.
            # A numerical value by itself does not override the other stated drive.
            supplied = {mode for mode, rule in excitation.items()
                        if re.search(rule["value_pattern"], text)}
            if "excitation" not in stated:
                indicated = {mode for mode, rule in excitation.items()
                             if any(_mentions(term, text) for term in rule["mode_terms"])}
                inferred = indicated or supplied
                if len(inferred) == 1:
                    stated["excitation"] = next(iter(inferred))
            mode = stated.get("excitation")
            if mode in supplied:
                known.add(excitation[mode]["input"])
            needed = {i["key"]: i for i in plan["required_inputs"]}
            if cid == "steady_extended_surface_conduction":
                # Preserve known boundary kinds while requesting missing numeric values.
                details = []
                if stated.get("base_boundary") == "temperature":
                    details.append("基端温度已说明，请给温度数值和单位")
                else:
                    details.append("给基端温度或热流的类型、数值和单位")
                if stated.get("side_exchange") == "combined":
                    details.append("侧面综合换热已说明，请给系数数值和室内环境温度")
                else:
                    details.append("给侧面换热系数、环境温度及其适用范围")
                if stated.get("radiation") == "included":
                    details.append("综合系数已包含辐射，不再重复施加辐射")
                else:
                    details.append("说明系数是否已包含辐射，未包含时给辐射条件")
                if stated.get("tip_boundary") == "adiabatic":
                    details.append("末端绝热已说明")
                else:
                    details.append("给末端绝热、换热或定温条件")
                needed["fin_thermal_boundaries"] = {"key":"fin_thermal_boundaries", "meaning":"；".join(details)+"。"}
            if cid == thermal_intake.SOLID and "heat_generation_power_W" in operating_inputs:
                needed["solid_heat_source"] = {
                    "key": "solid_heat_source",
                    "meaning": "净发热功率已给。确认实际发热域与空间分布；仅在均匀假设下用平均体积源，等效面源只施加一次。需要非均匀电损耗时另做电热映射。",
                }
            if cid == thermal_intake.MOVING and "absorbed_surface_flux_W_m2" in operating_inputs:
                needed["surface_heating_history"] = {
                    "key": "surface_heating_history",
                    "meaning": "已给净吸收表面热通量。确认受照侧、加热区内是否近似均匀或给位置分布，核对时序映射。",
                }
            if cid == thermal_intake.REACTION and "jacket_coolant_bulk_temperature_K" in operating_inputs:
                needed["reacting_bed_jacket_wall"] = {
                    "key": "reacting_bed_jacket_wall",
                    "meaning": "已给夹套体温。补夹套侧膜系数或流量和物性、管壁厚度/导热率和床侧传热；夹套体温不等同于反应管内壁温度。",
                }
            if cid == _INTERNAL_FORCED_CHT:
                if stated.get("air_drive") == "unconfirmed":
                    needed["air_drive_choice"] = rules["condition_questions"]["air_drive"]
                if stated.get("air_circuit") == "closed_recirculation":
                    needed["air_thermal_boundaries"] = {
                        "key": "air_thermal_boundaries",
                        "meaning": "封闭内循环没有外部质量入口。给内循环空气物性和温压基准、外壳几何/导热、外侧环境空气温度及对流条件；有外界辐射时另给辐射环境温度和表面发射率。",
                    }
            if "upstream_gauge_pressure_Pa" in operating_inputs and "valve_mass_flow_characteristic" in needed:
                needed["valve_mass_flow_characteristic"] = {
                    "key": "valve_mass_flow_characteristic",
                    "meaning": "已给阀前表压；补参考环境压力以换算入口绝压、入口温度、阀的开启时序和质量流量特性，以及软管尺寸或标定阻力。",
                }
            if "chamber_pressure_difference_Pa" in operating_inputs and "pressure_schedule" in needed:
                needed["pressure_schedule"] = {
                    "key": "pressure_schedule",
                    "meaning": "已给内外净压差；补受压腔室、压差是否均匀及加载顺序。",
                }
            elif "chamber_gauge_pressure_Pa" in operating_inputs and "pressure_schedule" in needed:
                needed["pressure_schedule"] = {
                    "key": "pressure_schedule",
                    "meaning": "已给腔内表压；补表压参考是否为受压面的外界压力、所属腔室、压力是否均匀及加载顺序。",
                }
            elif "chamber_absolute_pressure_Pa" in operating_inputs and "pressure_schedule" in needed:
                needed["pressure_schedule"] = {
                    "key": "pressure_schedule",
                    "meaning": (
                        "已给内外两侧的绝压；确认读数属于同一工况和时刻，并核对受压面、分布和加载顺序。"
                        if "exterior_absolute_pressure_Pa" in operating_inputs else
                        "已给腔内绝压；补作用于外露面的外界绝压、所属腔室、压力是否均匀及加载顺序。"
                    ),
                }
            if cid == _PNEUMATIC_STATIC and (
                operating_inputs.get("chamber_pressure_difference_Pa", 0) < 0
                or operating_inputs.get("chamber_gauge_pressure_Pa", 0) < 0
            ):
                needed["vacuum_loading_boundary"] = {
                    "key": "vacuum_loading_boundary",
                    "meaning": "确认分别暴露于腔内和外界压力的实际表面及载荷路径；采用几何非线性，检查塌陷、自接触、失稳及求解路径。只有物理边界允许时才用净压差等效。",
                }
            if "indentation_displacement_m" in operating_inputs and "mechanical_bc" in needed:
                needed["mechanical_bc"] = {
                    "key": "mechanical_bc",
                    "meaning": "给支承、压头及接触面、压入方向、行程起点/预载、载荷路径与采样点；已给位移只定义行程量，不能代替完整加载历程。",
                }
            for rule in plan["conditional_inputs"]:
                if stated.get(rule["condition"]) in rule["values"]:
                    needed.update((item["key"], item) for item in rule["inputs"])
            if cid == _INTERNAL_FORCED_CHT and stated.get("air_drive") == "prescribed_flow" and (
                    operating_inputs.keys() & {"volume_flow_m3_s", "mass_flow_kg_s"}):
                needed["installed_inlet_flow"] = {
                    "key": "installed_inlet_flow",
                    "meaning": "已给入口流量数值；确认安装工况、测量截面与对应密度/参考温压，并补实际入口速度分布和湍流设定。",
                }
            if cid == _INTERNAL_FORCED_CHT and stated.get("air_circuit") == "closed_recirculation":
                if "installed_inlet_flow" in needed:
                    needed["installed_inlet_flow"] = {
                        "key": "installed_inlet_flow",
                        "meaning": "提供内部循环测量截面的流量、流向、实际参考状态及分布；作为内部约束，外壁无质量入口。",
                    }
                if "fan_characteristics" in needed:
                    needed["fan_characteristics"] = {
                        "key": "fan_characteristics",
                        "meaning": "提供已有转速和压流曲线的数值、密度/压力口径及安装方向，与内循环系统阻力联合确定实际工作点。",
                    }
            for condition, question in rules.get("condition_questions", {}).items():
                if condition not in stated:
                    needed[question["key"]] = question
            scope_terms = rules.get("scope_terms", [])
            conditional = bool(scope_terms) and not any(_mentions(term, text) for term in scope_terms)
            if conditional:
                question = rules["scope_question"]
                needed[question["key"]] = question
            aliases = rules.get("goal_aliases", {})
            explicit_goals = {aliases.get(goal, goal) for goal in request.get("goals", [])}
            goals = explicit_goals.copy()
            goals.update(goal for goal, terms in rules.get("goal_terms", {}).items()
                         if any(_mentions(term, text) for term in terms) or
                         _pattern_mentions(rules.get("goal_patterns", {}).get(goal, r"(?!)"), raw))
            if cid == _PNEUMATIC_STATIC:
                goals.update(static_signals)
                if "free_bending_angle" in goals and stated.get("angle_reference") in (None, "relative_unpressurized"):
                    if re.search(rf"(?:相对|比|以|较)[^。；，,\n]{{0,10}}(?:{_PNEUMATIC_EQUAL_REFERENCE})", raw):
                        stated["angle_reference"] = "relative_equal_pressure"
                if "blocked_tip_force" in goals:
                    if _pattern_mentions(_PNEUMATIC_SENSOR_LOAD, raw):
                        stated.setdefault("output_boundary", "blocked_tip")
                        stated.setdefault("force_direction", "sensor_axis")
                    if re.search(r"(?:法向|法线)[^。；，,\n]{0,6}(?:不重合|不平行|不同|不一致)|不沿(?:板面)?法线", raw):
                        stated["force_direction"] = "sensor_axis"
                    if re.search(r"(?:测力|传感器|读数|载荷)[^。；，,\n]{0,18}(?:竖直|垂直)向上", raw):
                        stated["sensor_axis"] = "vertical_up"
                    if re.search(rf"(?:{_PNEUMATIC_EQUAL_REFERENCE})[^。；，,\n]{{0,10}}(?:清零|归零|为零|作零点)", raw):
                        stated["force_reference"] = "relative_equal_pressure"
                        needed["force_reference_baseline"] = {
                            "key": "force_reference_baseline",
                            "meaning": "给同一安装、重力、止挡位姿和预载下的内外等压零点或初态，核对沿测力轴的带符号反力增量。",
                        }
            if cid == _PNEUMATIC_TRANSIENT and measured_gas_inputs:
                goals.add("chamber_pressure_history")
            goals.difference_update(
                goal for goal, pattern in rules.get("goal_exclude_patterns", {}).items()
                if _pattern_mentions(pattern, raw) and goal not in explicit_goals
            )
            for goal, inputs in plan["goal_inputs"].items():
                if goal in goals:
                    needed.update((i["key"], i) for i in inputs)
            if cid == thermal_intake.HEATED_DUCT:
                if "section_area_m2" in operating_inputs:
                    needed["internal_flow_geometry"] = {
                        "key": "internal_flow_geometry",
                            "meaning": "已绑定流通截面与可推导的宽高比；确认实际受热覆盖、未受热壁以及两测站间的变截面、粗糙度和局部构件。",
                    }
                if {"bulk_velocity_m_s", "inlet_temperature_K"} <= operating_inputs.keys():
                    needed["internal_flow_operating_point"] = {
                        "key": "internal_flow_operating_point",
                        "meaning": "已绑定流量或平均速度及入口温度。确认测量截面、边界条件和实际运行状态；若已明确充分发展，可直接用对应入口形状。",
                    }
                if {"density_kg_m3", "dynamic_viscosity_Pa_s", "specific_heat_capacity_J_kg_K",
                    "fluid_conductivity_W_m_K"} <= operating_inputs.keys():
                    needed.pop("internal_flow_fluid_properties", None)
                if "known_net_heat_to_fluid_W" in operating_inputs:
                    needed["internal_flow_wall_values"] = {
                        "key": "internal_flow_wall_values",
                        "meaning": "已绑定净传入流体的热功率，均布时由实际受热面积换算热流；确认未供热边条件及入口温度发展口径。",
                    }
                elif "electrical_input_power_W" in operating_inputs:
                    needed["internal_flow_wall_values"] = {
                        "key": "internal_flow_wall_values",
                        "meaning": "已给电输入功率。补净传热功率或热损失与基线校准；电功率不能直接作为入水热量。",
                    }
                elif "wall_temperature_K" in operating_inputs and stated.get("wall_control") == "prescribed_wall_temperature":
                    needed["internal_flow_wall_values"] = {
                        "key": "internal_flow_wall_values",
                        "meaning": "已绑定所述受热段内壁温度；保留加热起止和未加热段边界，恒壁温工况不再指定同一壁面的热流。",
                    }
                if "pressure_drop" in goals and stated.get("sampling") == "manifold_static_taps":
                    needed["internal_flow_pressure_stations"] = {
                        "key": "internal_flow_pressure_stations",
                        "meaning": "两腔静压差涵盖转弯、缩扩口、发展段及腔内损失。补集流腔、汇流腔与过流口尺寸和取压高度/位置，或提供对应局损标定。",
                    }
            for condition, alternatives in rules.get("condition_input_refinements", {}).items():
                for key, meaning in alternatives.get(stated.get(condition), {}).items():
                    if key in needed:
                        needed[key] = {"key": key, "meaning": meaning}
            steps = plan["steps"]
            terminal_steps = rules.get("goal_terminal_steps", {})
            scoped_goals = [goal for goal in plan["goals"] if goal in goals and goal in terminal_steps]
            if scoped_goals:
                selected = {terminal_steps[goal] for goal in scoped_goals}
                for step in reversed(steps):
                    if step["id"] in selected:
                        selected.update(step["depends_on"])
                steps = [step for step in steps if step["id"] in selected]
            capabilities = rules["capability_ids"]
            if cid == thermal_intake.HEATED_DUCT:
                capabilities = [capability for capability in capabilities
                                if capability.startswith(plan["solver"] + ".") and (
                                    "external_probe_temperature" in goals or
                                    capability not in {"openfoam_opencfd.steady_cht", "elmer.conjugate_heat_transfer"})]
            if cid == _INTERNAL_FORCED_CHT:
                excluded = {
                    "openfoam_opencfd.fan_curve_operating_point": stated.get("air_drive") == "prescribed_flow",
                    "openfoam_opencfd.surface_view_factor_exchange": stated.get("radiation") != "included",
                    "openfoam_opencfd.steady_cht": stated.get("operation") == "transient",
                    "openfoam_opencfd.transient_cht": stated.get("operation") == "steady",
                }
                capabilities = [capability for capability in capabilities if not excluded.get(capability)]
            step_capabilities = rules.get("step_capabilities")
            if step_capabilities:
                capabilities = [cap for step in steps for cap in step_capabilities[step["id"]]]
            scope = rules.get("terminal_scope", {}).get(steps[-1]["id"], plan["scope"])
            if cid == _INTERNAL_FORCED_CHT and stated.get("air_circuit") == "closed_recirculation":
                scope = "封闭罩内空气由风机循环，气流与发热固体、壳体联立换热。外界散热通过壳壁外侧对流和按需启用的辐射闭合。"
                steps = [
                    {**step,
                     "equation": "循环气流工作点由同转速 Δp_fan(Q)=Δp_internal_system(Q) 给出。"
                                 "保持外壳质量通量为零；传热路径包含固体、内循环空气、壳壁和壳外换热。",
                     "receives": ["内循环风机曲线或现场循环流量、内部阻力、固体导热与接口、壳外热边界"]}
                    if step["id"] == "air_and_interfaces" else step for step in steps
                ]
            custom_work, checks = plan["custom_work"], plan["checks"]
            solver_role = rules.get("solver_role", "component")
            if cid == thermal_intake.HEATED_DUCT:
                if "approx_laminar_thermal_entry_length_m" in operating_inputs:
                    checks = [*checks, "入口尺度 L_h≈0.05 Re_Dh Dh、L_th≈0.05 Re_Dh Pr Dh 仅为层流量级估计；"
                                       "矩形宽高比、受热边、入口剖面和共轭导热会改变局部发展。"]
                if "asymptotic_laminar_constant_flux_Nu_Dh" in operating_inputs:
                    checks = [*checks, "圆管出口内壁以充分发展恒热流渐近 Nu 估算；从加热起点计算发展距离，"
                                       "入口段不可沿程使用同一个渐近值。"]
                if "calibrated_length_mean_htc_LMTD_W_m2_K" in operating_inputs:
                    checks = [*checks, "采用同工况量热标定段平均 h，NTU=hA/(ṁcp)，(Tw−Tout)/(Tw−Tin)=exp(−NTU)，"
                                       "Q=ṁcp(Tout−Tin)。用同一受热面积、壁温边界、物性和流量；"
                                       "标定 h 已包含该段入口效应，既不当局部 h，也不替换成充分发展 Nu。"]
                if "conditional_all_electrical_power_to_fluid_rise_K" in operating_inputs:
                    checks = [*checks, "全部所给电功率转化为入水热且无其他热源，才得到该条件化温升上界；外贴探头不由此确定。"]
                if any(key.startswith("conditional_outlet_") for key in operating_inputs):
                    checks = [*checks, "条件化热算术按同一恒定截面、给定热边界、定比热且无其他热交换；"
                                       "给定 h 的温差或壁温假设采用相同位置/区段的 h。"]
                if "conditional_straight_friction_loss_Pa" in operating_inputs:
                    checks = [*checks, "条件化压损使用给定达西因子或低 Re 充分发展光滑直段的无滑移解析因子，以及同截面和代表性常物性；"
                                       "附加局损仅适用于同参考速度，并不等同于任意两点的实测静压差。"]
            if cid == _PNEUMATIC_STATIC and "blocked_tip_force" in scoped_goals and stated.get(
                    "force_reference") == "relative_equal_pressure":
                steps = [
                    {**step, "equation": "在匹配的内外等压参考与目标压力工况分别求止挡载荷。"
                     "保留法向接触与摩擦切向力，将两个矢量载荷的差投影至指定测力轴，并核对符号与预载。"}
                    if step["id"] == "blocked_force" else step for step in steps
                ]
            if (cid == _PNEUMATIC_TRANSIENT and measured_gas_inputs and scoped_goals == ["chamber_pressure_history"]):
                stated["coupling"] = "prescribed_chamber_histories"
                if re.search(r"(?:腔内|囊内|气腔)[^。；，,\n]{0,10}(?:绝压|绝对压力)", raw):
                    stated["pressure_reference"] = "absolute"
                if re.search(r"出口[^。；，,\n]{0,8}(?:封死|封闭|封堵|密闭)", raw):
                    stated["outlet_boundary"] = "sealed"
                if _mentions("氮气", text):
                    stated["gas_species"] = "nitrogen"
                scope = "用同步实测的气腔入口质量流量和腔体积时序闭合气体质量与能量，计算腔压时序。适用性取决于腔内压力均匀近似和采样时间分辨率。"
                solver_role = "catalog_reference_only"
                custom_work = ["对实测输入重采样并复核同步；实现气腔质量和能量积分及所选状态方程。"]
                checks = ["检查气腔均匀压力近似、质量与能量守恒、采样和积分步敏感性；有独立腔压记录时进行比较。"]
                steps = [
                    {**step, "operation": "用测得的入口质量流量和体积时序计算腔压",
                     "equation": "dm/dt=ṁ_in−ṁ_out，封闭出口时 ṁ_out=0；d(mu)/dt=Σṁ_in h_in−Σṁ_out h_out+Q̇−p dV/dt。由气体状态方程用 m、V 和温度求绝压。",
                     "receives": ["同一时间轴的腔入口质量流量与腔体积、初始状态、气体物性、入口焓、换热条件"],
                     "produces": ["腔内绝对压力时序"]} if step["id"] == "chamber_pressure" else step
                    for step in steps
                ]
                needed.pop("valve_mass_flow_characteristic", None)
                needed.pop("chamber_volume_history_or_model", None)
                outlet = ("出口已封闭，按无外流处理。" if stated.get("outlet_boundary") == "sealed"
                          else "确认出口封闭或提供出口流量时序。")
                needed["chamber_flow_and_volume_traces"] = {
                    "key": "chamber_flow_and_volume_traces",
                    "meaning": f"提供同步的腔入口质量流量与腔体积时间序列、单位、采样率和共同时间零点；检查腔内压力均匀近似和时间分辨率。{outlet}",
                }
                gas = ("气体已指定为氮气；选择适用的状态方程。"
                       if stated.get("gas_species") == "nitrogen" else "确认气体种类或组分及适用的状态方程。")
                needed["chamber_initial_state"] = {
                    "key": "chamber_initial_state",
                    "meaning": f"给初始绝压与气温，或能确定初始质量和温度的等价数据。{gas}",
                }
                needed["thermal_energy_closure"] = {
                    "key": "thermal_energy_closure",
                    "meaning": "给入口气体温度或比焓时间序列和壁面换热条件，或说明可采用的绝热或等温近似及依据。",
                }
                if stated.get("pressure_reference") == "absolute":
                    needed["pressure_history_observation"] = {
                        "key": "pressure_history_observation",
                        "meaning": "绝压已指定；给腔内读回或对照测站、期望输出采样和动作起点。",
                    }
            closures, sources = plan["closures"], plan["sources"]
            if len(steps) < len(plan["steps"]):
                evidence = {item for step in steps for item in step["evidence"]}
                closures = [closure for closure in closures if evidence.intersection(closure["evidence"])]
                evidence.update(item for closure in closures for item in closure["evidence"])
                sources = [source for source in sources if source["id"] in evidence]
            research_terms = rules.get("research_scope_terms", [])
            research_needs = rules.get("research_needs", {})
            reference = rules.get("research_reference")
            if reference:
                if dois:
                    research_terms = reference["intent_terms"] if dois == {reference["doi"]} else []
                    if not research_terms:
                        research_needs = {}
            research_pattern = rules.get("research_scope_pattern")
            if (research_terms or research_pattern) and not (
                any(_mentions(term, text) for term in research_terms)
                or (research_pattern and _pattern_mentions(research_pattern, raw))
            ):
                research_needs = {}
            topic_patterns = rules.get("research_topic_patterns", {})
            topics = {key for key, pattern in topic_patterns.items() if _pattern_mentions(pattern, raw)}
            limited = re.search(r"(?:只|仅)(?:需|想|要)?(?:请)?(?:核查|核实|查证|查阅|查|确认)[^。；;！？!?\n]*", raw)
            if topics and limited:
                topics = {key for key in topics if _pattern_mentions(topic_patterns[key], limited[0])} or topics
            if topics:
                research_needs = {key: question for key, question in research_needs.items() if key in topics}
            if cid == thermal_intake.HEATED_DUCT and research_needs:
                named_solvers = {owner for name, owner in (("OpenFOAM", "openfoam_opencfd"), ("Elmer", "elmer"))
                                 if re.search(name, raw, re.I)}
                if named_solvers and plan["solver"] not in named_solvers:
                    research_needs = {}
            for rule in rules.get("research_needs_by_condition", []):
                if stated.get(rule["condition"]) in rule["values"]:
                    research_needs = rule["needs"]
            if cid == thermal_intake.HEATED_DUCT and thermal_intake.requires_outflow_enthalpy_review(
                raw, request, plan["solver"], plan["version"], scoped_goals
            ) and (not limited or re.search(r"混合|回流|焓|热", limited[0])):
                research_needs = {
                    **research_needs,
                    "heated_internal_flow_positive_outflow_enthalpy":
                        "在所选固定版本官方手册和源码核查：回流出口上，能否对只向外流出的面质量通量积分焓并除正向质量流，"
                        "再由变比热状态关系反求混合杯温度；区分内置条件掩码、输入字段预处理及需要自研的集成。",
                }
            paper_doi = next(iter(dois)) if len(dois) == 1 else None
            case_references = [case for case, terms in rules.get("paper_case_terms", {}).items()
                               if any(_mentions(term, text) for term in terms)]
            status = "verify_local" if plan["implementation"] == "configured" else "needs_development"
            inputs = [i for key, i in needed.items() if key not in known]
            if adapter == "confirmed_axial_strip":
                status = "awaiting_confirmation" if strip_review["next_action"] == "confirm" else "needs_input"
                inputs = strip_review["missing"]
            target_selection = rules.get("require_target_selection")
            selected_goals = scoped_goals if target_selection else scoped_goals or plan["goals"]
            applications = [choice for choice in rules.get("application_choices", [])
                            if all(stated.get(key, value) == value for key, value in choice.items()
                                   if key != "application")]
            plans.append(dict(
                chain=cid, domain=plan["domain"], solver=plan["solver"], version=plan["version"],
                implementation=plan["implementation"], status=status,
                scope=scope, goals=selected_goals, steps=steps, closures=closures,
                **({"scoped_goals": scoped_goals} if scoped_goals else {}),
                **({"available_goals": plan["goals"]} if target_selection and not selected_goals else {}),
                **({"excitation": mode or "unconfirmed"} if excitation else {}),
                **({"native_adapter": plan["native_adapter"]} if adapter else {}),
                **({"explicit_paper_doi": paper_doi} if paper_doi else {}),
                **({"explicit_paper_cases": case_references} if case_references else {}),
                **({"application_choices": applications} if applications else {}),
                **({"normalized_inputs": operating_inputs} if operating_inputs else {}),
                **({"selected_conditions": {key: stated[key] for key in rules["condition_evidence"] if key in stated}}
                   if rules.get("condition_evidence") and (cid != _PNEUMATIC_TRANSIENT or stated) else {}),
                required_inputs=inputs,
                custom_work=custom_work, checks=checks, sources=sources,
                capability_ids=capabilities, goal_aliases=rules.get("goal_aliases", {}),
                research_needs=research_needs, solver_role=solver_role,
                quantity_aliases=rules.get("quantity_aliases", {}), scope_confirmation_required=conditional or bool(adapter),
            ))
    if any(plan["chain"] == thermal_intake.HEATED_DUCT and set(plan["goals"]) -
           {"pressure_drop", "reynolds_number"} for plan in plans):
        return [plan for plan in plans if plan["chain"] != "forced_duct_pressure_drop"]
    return plans


def compact(plans):
    # Keep the direction, equations, engineering limits and source locators in the first
    # result. Full evidence and closure detail remain available through chain-inventory.
    return [dict(
        chain=p["chain"], domain=p["domain"], solver=p["solver"], version=p["version"],
        implementation=p["implementation"], status=p["status"], scope=p["scope"], solver_role=p["solver_role"],
        goals=p["goals"], capability_ids=p["capability_ids"], scope_confirmation_required=p["scope_confirmation_required"],
        **({"excitation": p["excitation"]} if "excitation" in p else {}),
        **({"available_goals": p["available_goals"]} if "available_goals" in p else {}),
        **({"selected_conditions": p["selected_conditions"]} if "selected_conditions" in p else {}),
        **({"application_choices": p["application_choices"]} if "application_choices" in p else {}),
        **({"normalized_inputs": p["normalized_inputs"]} if "normalized_inputs" in p else {}),
        **({"native_adapter": p["native_adapter"]} if "native_adapter" in p else {}),
        steps=[{k: step[k] for k in ("id", "operation", "depends_on", "equation", "receives", "produces", "implementation", "evidence")}
               for step in p["steps"]],
        required_inputs=p["required_inputs"], checks=p["checks"], custom_work=p["custom_work"],
        sources=[{k: source[k] for k in ("id", "url", "locator", "claim")} for source in p["sources"]],
    ) for p in plans]
