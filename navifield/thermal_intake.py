"""Local SI readback for known thermal inputs. A parse never supplies a missing boundary."""
import re
import unicodedata
from decimal import Decimal
from math import exp, isfinite, pi, tanh


SOLID = "internally_heated_solid_conduction"
MOVING = "moving_solid_surface_heating"
REACTION = "reacting_bed_thermal_transport"
HEATED_DUCT = "steady_heated_internal_flow"
FIXED_SOLID = "steady_fixed_temperature_solid_conduction"

_NUMBER = r"[+\-]?\d+(?:\.\d+)?(?:[Ee][+\-]?\d+)?"
_ASSIGN = r"\s*(?:(?:为|是|达|长|约|保持在|保持|稳定在|稳定为|维持在|共|一共|合计|[:=])\s*)?"
_POWER = {"w": 1, "kw": 1000, "瓦": 1, "千瓦": 1000}
_LENGTH = {"m": Decimal(1), "mm": Decimal(".001"), "cm": Decimal(".01"),
           "米": Decimal(1), "毫米": Decimal(".001"), "厘米": Decimal(".01")}
_AREA = {"m2": Decimal(1), "cm2": Decimal(".0001"), "mm2": Decimal(".000001"),
         "平方米": Decimal(1), "平方厘米": Decimal(".0001"), "平方毫米": Decimal(".000001")}
_HEAT_ENERGY = {"j": 1, "kj": 1000, "焦": 1, "千焦": 1000}
_POWER_UNIT = r"kw|w|千瓦|瓦"
_AREA_UNIT = r"m\^?2|cm\^?2|mm\^?2|平方米|平方厘米|平方毫米"
_LENGTH_UNIT = r"mm|cm|m(?![A-Za-z0-9^])|毫米|厘米|(?<!秒)米"


def _value(text, pattern, scale, *, positive=True):
    found = {
        Decimal(number) * scale[re.sub(r"[\s^]", "", unit.casefold())]
        for number, unit in re.findall(pattern, text, re.IGNORECASE)
    }
    if len(found) != 1:
        return None
    value = found.pop()
    return value if isfinite(float(value)) and (not positive or value > 0) else None


def _single_at(text, label, unit, scale, suffix=""):
    return _value(text, rf"(?:{label}){_ASSIGN}({_NUMBER})\s*({unit}){suffix}", scale)


def _with_number(values):
    return {name: float(value) for name, value in values.items() if value is not None}


def _solid_input(text):
    power = _single_at(text, r"发热(?:功率)?|生热(?:功率)?|产热(?:功率)?|净热功率|耗散热功率",
                       _POWER_UNIT, _POWER, r"(?![A-Za-z])(?!(?:\s*/|\s*每))")
    # Mean rates are stated only for an explicitly localized planar source and its own thickness.
    area_label = r"(?:发热|生热|热源)(?:层|区|体)?(?:的)?面积|铺开(?:面积)?|发热面(?:的)?面积"
    area = _single_at(text, area_label, _AREA_UNIT, _AREA)
    source_thickness = (r"(?:发热层|生热层|热源层)(?:的)?厚(?:度)?|"
                        rf"(?:{area_label}){_ASSIGN}{_NUMBER}\s*(?:{_AREA_UNIT})\s*[，,]\s*厚(?:度)?")
    thickness = _single_at(text, source_thickness, _LENGTH_UNIT, _LENGTH)
    values = {"heat_generation_power_W": power, "source_area_m2": area, "source_thickness_m": thickness}
    if power is not None and area is not None and thickness is not None:
        values.update(uniform_source_assumption_W_m3=power / (area * thickness),
                      equivalent_single_surface_heat_flux_W_m2=power / area)
    return _with_number(values)


def _moving_input(text):
    length = _single_at(text, r"(?:照射|加热|辐照)(?:区|段)?(?:的)?长度|(?:沿|顺)[^。；，,\n]{0,10}方向\s*(?:长(?:度)?)?",
                        _LENGTH_UNIT, _LENGTH)
    speed_values = {name + "/s": factor for name, factor in _LENGTH.items()} | {
        name + "/min": factor / 60 for name, factor in _LENGTH.items()
    }
    speed_text = re.sub(r"(毫米|厘米|米)\s*(?:每|/)\s*(秒|分钟)", lambda m: m[1] + ("/s" if m[2] == "秒" else "/min"), text)
    speed_text = re.sub(r"(mm|cm|m)\s*/\s*(s|min)\b", r"\1/\2", speed_text)
    speed = _single_at(speed_text, r"线速(?:度)?|走带速度|送料速度|运行速度|移动速度",
                       r"(?:mm|cm|m|毫米|厘米|米)/(?:min|s)\b", speed_values)
    # Canonicalize a fronted per-area qualifier without confusing incident flux with absorbed flux.
    flux_text = re.sub(rf"每\s*平方米\s*({_NUMBER})\s*({_POWER_UNIT})",
                       lambda m: f"{m[1]} {m[2]}/平方米", text)
    flux = _single_at(flux_text, r"实际吸收(?:的)?(?:热流(?:密度)?|功率密度)?|吸收(?:热流(?:密度)?|功率密度)",
                      _POWER_UNIT, _POWER, r"\s*/\s*(?:m\^?2|平方米)")
    values = {"heated_travel_length_m": length, "material_speed_m_s": speed, "absorbed_surface_flux_W_m2": flux}
    if length is not None and speed is not None:
        values["residence_time_s"] = length / speed
        if flux is not None:
            values["uniform_irradiation_dose_J_m2"] = flux * length / speed
    return _with_number(values)


def _reaction_input(text):
    energy_units = r"kj|j|千焦|焦"
    release = _single_at(text, r"每(?:摩尔|mol)(?:反应)?(?:释放热量|放热)|单位摩尔反应放热",
                         energy_units, _HEAT_ENERGY)
    if release is None:
        release = _single_at(text, r"反应放热(?:量)?|放热(?:量)?", energy_units, _HEAT_ENERGY, r"\s*/\s*(?:mol\b|摩尔)")
    signed_h = _value(text, rf"反应焓{_ASSIGN}({_NUMBER})\s*({energy_units})\s*/\s*(?:mol\b|摩尔)", _HEAT_ENERGY, positive=False)
    jacket = _value(text,
        rf"夹套[^。；，,\n]{{0,12}}(?:保持(?:在)?|温度(?:为)?|设定(?:为)?|稳定在)\s*({_NUMBER})\s*(摄氏度|°\s*c|℃|k\b)",
        {"摄氏度": 1, "°c": 1, "℃": 1, "k": 1}, positive=False)
    if jacket is not None and re.search(r"夹套[^。；，,\n]{0,22}(?:摄氏度|°\s*c|℃)", text, re.I):
        jacket += Decimal("273.15")
    return _with_number({"exothermic_heat_release_J_mol": release,
                         "reaction_enthalpy_J_mol": signed_h,
                         "jacket_coolant_bulk_temperature_K": jacket})


def _fixed_solid_input(text):
    """Derive an ideal slab only when its physical and geometric inputs are unambiguous."""
    dim = re.findall(rf"(?:尺寸|规格)\s*(?:为|是|[:=])?\s*({_NUMBER})\s*[×x*]\s*({_NUMBER})\s*[×x*]\s*({_NUMBER})\s*(mm|cm|m|毫米|厘米|米)", text, re.I)
    temps = re.findall(rf"({_NUMBER})\s*(?:°\s*c|摄氏度)", text, re.I)
    conductivity = _single_at(text, r"导热系数(?:按)?", r"w/\s*\(?m[·*]k\)?", {"w/(m·k)": 1, "w/m·k": 1, "w/(m*k)": 1, "w/m*k": 1})
    if len(dim) != 1 or len(temps) != 2 or conductivity is None:
        return {}
    if re.search(r"(?:不|非|不能|无法|未|无需|并非|不是)[^。；，,]{0,8}绝热|绝热[^。；，,]{0,8}(?:不成立|不能|无效)", text):
        return {}
    if not (re.search(r"侧(?:面|壁)[^。；]{0,12}绝热|绝热[^。；]{0,12}侧(?:面|壁)", text) and re.search(r"没有热源|无(?:内|体积)?热源", text) and re.search(r"稳态|稳定后|稳定保持|稳定维持", text) and re.search(r"两面|两侧|相对面", text)):
        return {}
    a,b,c,unit = dim[0]
    factor = _LENGTH[unit.casefold()]
    dimensions = [Decimal(x)*factor for x in (a,b,c)]
    temperatures = [Decimal(v) for v in temps]
    if min(dimensions) <= 0 or min(temperatures) <= Decimal('-273.15'):
        return {}
    if not re.search(r"板厚方向|厚度方向|穿厚", text):
        return {}
    # Explicit thickness disambiguates dimensions; conflicting or duplicated dimensions
    # cannot establish which pair forms the prescribed-temperature surfaces.
    thicknesses = {Decimal(n)*_LENGTH[u.casefold()] for n,u in re.findall(
        rf"(?:板厚|厚度|厚)\s*(?:为|是|[:=])?\s*({_NUMBER})\s*({_LENGTH_UNIT})", text, re.I)}
    if len(thicknesses) > 1:
        return {}
    if thicknesses:
        thickness = thicknesses.pop()
        matches = [i for i, d in enumerate(dimensions) if d == thickness]
        if len(matches) != 1:
            return {}
        thickness_index = matches[0]
    else:
        if re.search(r"(?:第三(?:个)?(?:尺寸|维度|方向)|最后(?:一个|一项|一维))[^。；，,]{0,12}(?:长|宽|高|面内)", text):
            return {}
        thickness_index = 2
        thickness = dimensions[2]
    if re.search(r"(?:第三(?:个)?(?:尺寸|维度|方向)|最后(?:一个|一项|一维))[^。；，,]{0,12}(?:长|宽|高|面内)", text) and thickness_index == 2:
        return {}
    face = [d for i,d in enumerate(dimensions) if i != thickness_index]
    area = face[0]*face[1]
    hot,cold = max(temperatures),min(temperatures)
    flux = conductivity*(hot-cold)/thickness
    result = _with_number({"plate_area_m2":area,"plate_thickness_m":thickness,"conductivity_W_mK":conductivity,"ideal_heat_rate_W":flux*area,"ideal_heat_flux_W_m2":flux,"ideal_midplane_temperature_K":(hot+cold)/2+Decimal('273.15')})
    result["ideal_heat_flow_direction"] = "hot_to_cold" if hot != cold else "zero_no_direction"
    return result


def known_inputs(chain, raw):
    reader = {FIXED_SOLID: _fixed_solid_input, SOLID: _solid_input, MOVING: _moving_input, REACTION: _reaction_input}.get(chain)
    if reader is None:
        return {}
    text = unicodedata.normalize("NFKC", raw).replace("−", "-")
    return reader(text)


def requires_outflow_enthalpy_review(raw, request, solver, version, goals):
    """Identify an unresolved version-bound outlet readback rather than private inputs."""
    if not {"bulk_temperature", "wall_bulk_temperature_difference", "internal_heat_transfer_coefficient"}.intersection(goals):
        return False
    owner = {"openfoam_opencfd": r"OpenFOAM|OpenCFD", "elmer": "Elmer"}.get(solver)
    if not owner:
        return False
    version_pattern = re.escape(version.lstrip("vV"))
    mentions = re.findall(rf"(?:{owner})\s*(?:版本|version)?\s*v?(\d+(?:\.\d+)*)", raw, re.I)
    if mentions and set(mentions) != {version.lstrip("vV")}:
        return False
    selected = (request.get("solver") == solver and request.get("version") in (None, version)
                or re.search(rf"(?:{owner})\s*(?:版本|version)?\s*v?{version_pattern}(?![\d.])", raw, re.I))
    if not selected:
        return False
    reversal = any(
        re.search(r"回流|倒灌|反向(?:流动|通量)", clause)
        and not re.search(r"(?:无|没有|不存在|不发生|不出现|不会)(?:任何)?\s*(?:回流|倒灌|反向)", clause)
        for clause in re.split(r"[。；，,;!?！？\n]", raw)
    )
    forward_only = re.search(
        r"(?:只|仅|单独)(?:需|要|看|算|计|统计|取|积分|使用|保留|按|对|用){0,4}[^。；，,\n]{0,8}"
        r"(?:流出|排出|外流|正向(?:质量)?通量)|(?:扣除|排除|去掉|过滤)[^。；，,\n]{0,8}(?:回流|倒灌|反向通量)|"
        r"(?:按|用)(?:流出|出流|正向)(?:面)?(?:质量流量|质量流|质量通量)(?:加权|积分)",
        raw,
    )
    return bool(reversal and forward_only)


def heated_duct_inputs(raw, conditions, supplied_flow):
    """Bind SI inputs on the declared section and derive only geometry-based quantities."""
    text = unicodedata.normalize("NFKC", raw).replace("−", "-")
    text = re.sub(r"(?<![一两\d])半\s*米", "0.5 米", text)
    width = _single_at(text, r"(?:截面|流道|通道|内)?宽(?:度)?", _LENGTH_UNIT, _LENGTH)
    height = _single_at(text, r"(?:截面|流道|通道|内)?(?:高(?:度)?|深(?:度)?)", _LENGTH_UNIT, _LENGTH)
    diameter = _single_at(text, r"内径|管径|直径", _LENGTH_UNIT, _LENGTH)
    dimensions = re.findall(
        rf"(?:截面(?:尺寸)?|断面(?:尺寸)?|矩形(?:管|通道|流道))\s*(?:为|是|[:=])?\s*"
        rf"({_NUMBER})\s*({_LENGTH_UNIT})?\s*[×x*]\s*({_NUMBER})\s*({_LENGTH_UNIT})",
        text, re.IGNORECASE,
    )
    if len(dimensions) == 1 and width is None and height is None:
        a, first_unit, b, second_unit = dimensions[0]
        width = Decimal(a) * _LENGTH[(first_unit or second_unit).casefold()]
        height = Decimal(b) * _LENGTH[second_unit.casefold()]
        if not (width > 0 and height > 0):
            width = height = None
    area = wetted = hydraulic = None
    shape = conditions.get("section_shape")
    if shape == "rectangular" and width is not None and height is not None:
        area, wetted = width * height, 2 * (width + height)
    elif shape == "circular" and diameter is not None:
        fraction = Decimal(str(pi))
        area, wetted = fraction * diameter ** 2 / 4, fraction * diameter
    if area is not None:
        hydraulic = 4 * area / wetted
    supplied_hydraulic = _single_at(text, r"水力直径|D_?h(?![A-Za-z])", _LENGTH_UNIT, _LENGTH)
    if hydraulic is None:
        hydraulic = supplied_hydraulic
    heated = _single_at(text, r"受热周长|加热周长|P_?h(?![A-Za-z])", _LENGTH_UNIT, _LENGTH)
    if heated is None and conditions.get("heated_wall_extent") == "full_perimeter":
        heated = wetted

    speed = _single_at(text, r"(?:入口)?(?:截面|断面|横截面)平均(?:流速|速度)|入口平均(?:流速|速度)|U_?b(?![A-Za-z])",
                       r"m/s|米每秒", {"m/s": 1, "米每秒": 1})
    density = _single_at(text, r"密度|ρ", r"kg/m\^?3|千克每立方米|g/cm\^?3|克每立方厘米",
                         {"kg/m3": 1, "千克每立方米": 1, "g/cm3": 1000, "克每立方厘米": 1000})
    viscosity = _single_at(text, r"动力(?:黏|粘)度|(?:黏|粘)度|μ",
                           r"(?:m)?pa[·*\s]*s|毫帕秒|帕秒",
                           {"pa·s": 1, "pa*s": 1, "pas": 1, "帕秒": 1, "毫帕秒": Decimal(".001"),
                            "mpa·s": Decimal(".001"), "mpa*s": Decimal(".001"), "mpas": Decimal(".001")})
    if re.search(r"国际单位|SI\s*(?:制|单位)", text, re.I):
        density = density or _unitless_at(text, r"密度|ρ")
        viscosity = viscosity or _unitless_at(text, r"动力(?:黏|粘)度|(?:黏|粘)度|μ")
    heat_flux = _single_at(text, r"(?:恒定|均匀|给定)?(?:热通量|热流密度)|q''", r"(?:kw|w)/m\^?2|瓦每平方米",
                           {"kw/m2": 1000, "w/m2": 1, "瓦每平方米": 1})
    inlet = _temperature_at(text, r"入口(?:温度)?|进口(?:温度)?|入流温度|进(?:水|气)(?:温度)?|起点的?(?:混合温度|混合杯温度)")
    wall = _temperature_at(text, r"壁面温度|管壁温度|恒壁温|定壁温|壁温|内表面(?:温度)?|内壁(?:温度)?|T_?w(?![A-Za-z])")
    measured_q = supplied_flow.get("volume_flow_m3_s")
    if measured_q is None and not re.search(r"标况|标方|额定|STP|SCFM|尚未测|未实测", text, re.I):
        q_by_minute = _value(text, rf"(?:流量|来流|流体|水|空气|气体|液体)[^。；，,\n]{{0,10}}?每分钟\s*({_NUMBER})\s*(毫升|升|ml\b|l\b)",
                             {"毫升": Decimal("1e-6") / 60, "ml": Decimal("1e-6") / 60,
                              "升": Decimal(".001") / 60, "l": Decimal(".001") / 60})
        if q_by_minute is not None:
            measured_q = float(q_by_minute)
            supplied_flow = {**supplied_flow, "volume_flow_m3_s": measured_q}
    measured_mass = supplied_flow.get("mass_flow_kg_s")
    if measured_mass is None:
        rate = _value(text,
            rf"(?:称重|称量|电子秤|秤上|质量流率|质量流量)[^。；，,\n]{{0,10}}?每秒"
            rf"(?:少|减(?:少)?|流出|出料|流入|进料)?\s*({_NUMBER})\s*(千克|克|kg\b|g\b)",
            {"千克": 1, "kg": 1, "克": Decimal(".001"), "g": Decimal(".001")})
        if rate is not None:
            measured_mass = float(rate)
            supplied_flow = {**supplied_flow, "mass_flow_kg_s": measured_mass}
    if measured_q is None and measured_mass is not None and density is not None:
        measured_q = float(Decimal(str(measured_mass)) / density)
        supplied_flow = {**supplied_flow, "volume_flow_m3_s": measured_q}
    if speed is None and area is not None and measured_q is not None:
        speed = Decimal(str(measured_q)) / area
    if speed is None and area is not None and density is not None and measured_mass is not None:
        speed = Decimal(str(measured_mass)) / (density * area)
    reynolds = None
    if hydraulic is not None and viscosity is not None:
        if measured_mass is not None and area is not None:
            reynolds = Decimal(str(measured_mass)) * hydraulic / (viscosity * area)
        elif speed is not None and density is not None:
            reynolds = density * speed * hydraulic / viscosity
    mass_flow = Decimal(str(measured_mass)) if measured_mass is not None else None
    if mass_flow is None and all(v is not None for v in (density, area, speed)):
        mass_flow = density * area * speed
    thermal = _conditional_heated_duct_quantities(
        text, conditions, heated, heat_flux, mass_flow, inlet, wall, reynolds, hydraulic, viscosity
    )
    pressure = _conditional_duct_loss(text, conditions, hydraulic, speed, density, reynolds, width, height,
                                      thermal.get("heated_length_m"))
    return {**supplied_flow, **_with_number({
        "section_width_m": width, "section_height_m": height, "inside_diameter_m": diameter,
        "rectangular_beta_short_over_long": min(width, height) / max(width, height)
        if shape == "rectangular" and width is not None and height is not None else None,
        "rectangular_aspect_long_over_short": max(width, height) / min(width, height)
        if shape == "rectangular" and width is not None and height is not None else None,
        "section_area_m2": area, "wetted_perimeter_m": wetted, "heated_perimeter_m": heated,
        "hydraulic_diameter_m": hydraulic, "bulk_velocity_m_s": speed, "density_kg_m3": density,
        "dynamic_viscosity_Pa_s": viscosity, "reynolds_Dh": reynolds, "heat_flux_W_m2": heat_flux,
        "inlet_temperature_K": inlet, "wall_temperature_K": wall,
    }), **thermal, **pressure}


def _unitless_at(text, label):
    return _single_at(text, label, r"(?=\s*(?:[,，、。;；]|的国际单位|$))", {"": 1})


def _conditional_heated_duct_quantities(text, conditions, heated_perimeter, heat_flux, mass_flow, inlet, wall,
                                        reynolds, hydraulic, viscosity):
    length = _single_at(text, r"(?:受热段?|加热段?|热套|加热套|夹套)(?:的)?长(?:度)?|"
                              r"夹套(?:里|内)(?:的)?(?:那|这|该)?|L_?h(?![A-Za-z])", _LENGTH_UNIT, _LENGTH)
    if length is None and conditions.get("heated_wall_extent") == "partial_perimeter":
        length = _single_at(text, r"(?:槽|流道|通道)(?:的)?长(?:度)?|[，,、]\s*长(?:度)?", _LENGTH_UNIT, _LENGTH)
    heat_capacity = _single_at(text, r"定压比热|比热(?:容)?|c_?p(?![A-Za-z])",
        r"(?:kj|j)/\(?kg(?:[·⋅*]|\s*)k\)?|(?:kj|j)/kg/k|焦每千克每开(?:尔文)?",
        {"焦每千克每开": 1, "焦每千克每开尔文": 1,
         **{f"{prefix}{unit}": scale for prefix, scale in (("j", 1), ("kj", 1000))
            for unit in ("/(kg·k)", "/(kg⋅k)", "/(kg*k)", "/(kgk)", "/kg·k", "/kg⋅k", "/kg*k", "/kgk", "/kg/k")}})
    conductivity = _single_at(text, r"导热率|热导率|导热系数", r"w/\(?m(?:[·⋅*]|\s*)k\)?|w/m/k|瓦每米每开(?:尔文)?",
                              {x: 1 for x in ("w/(m·k)", "w/(m⋅k)", "w/(m*k)", "w/(mk)", "w/m·k",
                                               "w/m⋅k", "w/m*k", "w/mk", "w/m/k", "瓦每米每开", "瓦每米每开尔文")})
    if re.search(r"国际单位|SI\s*(?:制|单位)", text, re.I):
        heat_capacity = heat_capacity or _unitless_at(text, r"定压比热|比热(?:容)?")
        conductivity = conductivity or _unitless_at(text, r"导热率|热导率|导热系数")
    heat_to_fluid = _single_at(text, r"(?:实际|净)(?:输入|进入|传入|传给|入)(?:流体|水|液体|气体)(?:的)?(?:热功率|热量|功率)?|净入(?:水|流体)热功率",
                              _POWER_UNIT, _POWER, r"(?!\s*/)")
    electrical = _single_at(text, r"电功率|电输入功率", _POWER_UNIT, _POWER, r"(?!\s*/)")
    coefficient = _single_at(text, r"给定(?:的)?换热系数|已知(?:的)?换热系数|平均换热系数|对流换热系数|h(?![A-Za-z])",
        r"w/\(?m\^?2(?:[·⋅*]|\s*)k\)?|w/m\^?2/k",
        {unit: 1 for unit in ("w/(m2·k)", "w/(m2⋅k)", "w/(m2*k)", "w/(m2k)", "w/m2·k",
                              "w/m2⋅k", "w/m2*k", "w/m2k", "w/m2/k")})
    result = {"heated_length_m": length, "specific_heat_capacity_J_kg_K": heat_capacity,
              "fluid_conductivity_W_m_K": conductivity, "mass_flow_kg_s": mass_flow,
              "specified_heat_transfer_coefficient_W_m2_K": coefficient, "known_net_heat_to_fluid_W": heat_to_fluid,
              "electrical_input_power_W": electrical}
    heated_area = heated_perimeter * length if heated_perimeter is not None and length is not None else None
    result["heated_wall_area_m2"] = heated_area
    flow_capacity = mass_flow * heat_capacity if mass_flow is not None and heat_capacity is not None else None
    mode = conditions.get("wall_control")
    if all(v is not None for v in (viscosity, heat_capacity, conductivity)):
        pr = viscosity * heat_capacity / conductivity
        result["prandtl"] = pr
    else:
        pr = None
    if reynolds is not None and reynolds <= 1000 and hydraulic is not None:
        result["approx_laminar_hydraulic_entry_length_m"] = Decimal(".05") * reynolds * hydraulic
        if pr is not None:
            thermal_entry = Decimal(".05") * reynolds * pr * hydraulic
            result["approx_laminar_thermal_entry_length_m"] = thermal_entry
            if length is not None:
                result["exit_graetz_number"] = reynolds * pr * hydraulic / length
            if (conditions.get("section_shape") == "circular"
                and conditions.get("heated_wall_extent") == "full_perimeter"
                and mode == "prescribed_wall_heat_flux" and coefficient is None
                and (conditions.get("thermal_development") == "fully_developed" or length is not None and length >= thermal_entry)):
                nusselt = Decimal(48) / 11
                coefficient = nusselt * conductivity / hydraulic
                result["asymptotic_laminar_constant_flux_Nu_Dh"] = nusselt
                result["asymptotic_laminar_heat_transfer_coefficient_W_m2_K"] = coefficient
    if heat_to_fluid is not None and flow_capacity is not None:
        result["bulk_temperature_rise_from_known_net_heat_K"] = heat_to_fluid / flow_capacity
        if inlet is not None:
            result["outlet_Tb_from_known_net_heat_constant_cp_K"] = inlet + heat_to_fluid / flow_capacity
    elif electrical is not None and flow_capacity is not None:
        result["conditional_all_electrical_power_to_fluid_rise_K"] = electrical / flow_capacity
        result["mass_flow_heat_capacity_W_K"] = flow_capacity
    if mode == "prescribed_wall_heat_flux" and heat_flux is None and heat_to_fluid is not None and heated_area is not None:
        heat_flux = heat_to_fluid / heated_area
        result["uniform_heat_flux_from_net_fluid_power_W_m2"] = heat_flux
    if mode == "prescribed_wall_heat_flux" and heat_flux is not None:
        if heated_area is not None:
            result["uniform_heated_wall_power_W"] = heat_flux * heated_area
        if heated_area is not None and flow_capacity is not None and inlet is not None:
            result["conditional_outlet_Tb_uniform_flux_constant_cp_K"] = inlet + heat_flux * heated_area / flow_capacity
        if coefficient is not None:
            basis = "asymptotic_h" if "asymptotic_laminar_constant_flux_Nu_Dh" in result else "given_h"
            result[f"conditional_wall_minus_bulk_{basis}_K"] = heat_flux / coefficient
            if "conditional_outlet_Tb_uniform_flux_constant_cp_K" in result:
                result[f"conditional_outlet_wall_T_uniform_flux_{basis}_K"] = (
                    result["conditional_outlet_Tb_uniform_flux_constant_cp_K"] + heat_flux / coefficient
                )
    if mode == "prescribed_wall_temperature" and all(
        v is not None for v in (coefficient, heated_area, flow_capacity, inlet, wall)
    ):
        ntu = coefficient * heated_area / flow_capacity
        attenuation = Decimal(str(exp(-float(ntu))))
        outlet = wall - (wall - inlet) * attenuation
        result["conditional_outlet_Tb_constant_wall_temperature_and_h_K"] = outlet
        result["conditional_absorbed_heat_constant_wall_T_and_h_W"] = flow_capacity * (outlet - inlet)
        result["conditional_NTU_constant_wall_temperature_and_h"] = ntu
        if re.search(r"(?:对数平均温差|LMTD)", text, re.I) and re.search(r"量热|标定", text):
            result["calibrated_length_mean_htc_LMTD_W_m2_K"] = coefficient
    return _with_number(result)


def _conditional_duct_loss(text, conditions, hydraulic, speed, density, reynolds, width, height, heated_length):
    length = _single_at(text, r"取压(?:段)?长(?:度)?|测压(?:段)?长(?:度)?|取压(?:孔)?间距|测站间距", _LENGTH_UNIT, _LENGTH)
    end_taps = re.search(r"(?:取压孔|取压嘴|静压孔|测压孔)[^。；，,\n]{0,16}(?:热套|加热段|受热段|夹套)"
                         r"(?:的)?(?:两端|起点(?:和|及|与)终点|起止点)", text)
    if length is None and end_taps and heated_length is not None:
        length = Decimal(str(heated_length))
    approach = _single_at(text, r"(?:入口|加热前|受热前)(?:的)?(?:未加热)?(?:直段|发展段)(?:的)?长(?:度)?|"
                                r"前[^。；，,\n]{0,8}(?:经过|通过|走过)",
                          _LENGTH_UNIT, _LENGTH)
    if approach is None:
        approach = _single_at(text, r"(?:前面|上游|前方)[^。；，,\n]{0,4}(?:另有|还有|设有)", _LENGTH_UNIT, _LENGTH,
                              r"(?=\s*(?:同径|直段))")
    darcy = _single_at(text, r"达西(?:摩擦因子|摩阻因子|摩阻系数)|f_?D(?![A-Za-z])",
                       r"(?:无量纲)?", {"": 1, "无量纲": 1})
    local = _single_at(text, r"局部损失系数(?:总和|之和|合计)|ΣK",
                       r"(?:无量纲)?", {"": 1, "无量纲": 1})
    result = {"pressure_station_spacing_m": length, "unheated_straight_approach_length_m": approach,
              "specified_darcy_friction_factor": darcy,
              "specified_total_local_loss_coefficient": local}
    inferred_fully_developed = (
        approach is not None and reynolds is not None and hydraulic is not None and end_taps
        and re.search(r"(?:光滑|平滑)[^。；，,\n]{0,6}(?:圆管|直管)|(?:圆管|直管)[^。；，,\n]{0,6}(?:光滑|平滑)", text)
        and re.search(r"水平", text) and approach >= Decimal(".05") * reynolds * hydraulic
    )
    if darcy is None and reynolds is not None and reynolds <= 1000 and (
        conditions.get("velocity_development") == "fully_developed" or inferred_fully_developed
    ):
        # The exact no-slip rectangular Poiseuille correction uses the narrow side.
        shape = conditions.get("section_shape")
        poiseuille = Decimal(64) if shape == "circular" else None
        if shape == "rectangular" and width is not None and height is not None:
            ratio = float(min(width, height) / max(width, height))
            if ratio > 0:
                series = sum(tanh(n * pi / (2 * ratio)) / n ** 5 for n in range(1, 100, 2))
                correction = 1 - 192 * ratio / pi ** 5 * series
                poiseuille = Decimal(str(96 / ((1 + ratio) ** 2 * correction)))
        if poiseuille is not None:
            darcy = poiseuille / reynolds
            result["conditional_laminar_Darcy_fully_developed"] = darcy
    if all(v is not None for v in (length, darcy, hydraulic, speed, density)):
        head = density * speed ** 2 / 2
        result["conditional_straight_friction_loss_Pa"] = darcy * length / hydraulic * head
        if local is not None and conditions.get("local_losses") == "within_stations":
            result["conditional_same_reference_velocity_friction_and_local_loss_Pa"] = (
                result["conditional_straight_friction_loss_Pa"] + local * head
            )
    return _with_number(result)


def _temperature_at(text, label):
    values = {
        Decimal(number) + (Decimal("273.15") if not unit.lower().startswith("k") else 0)
        for number, unit in re.findall(
            rf"(?:{label}){_ASSIGN}({_NUMBER})\s*(k\b|摄氏度|°\s*c)", text, re.IGNORECASE
        )
    }
    if len(values) == 1:
        value = values.pop()
        if value > 0 and isfinite(float(value)):
            return value
    return None
