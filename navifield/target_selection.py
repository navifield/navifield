"""Versioned engineering output selection and explicit coupling of target stages."""

import json
from pathlib import Path
from urllib.parse import urlsplit

from .model_selection import _conditions, _identifier, _text_fields, _tokens

TARGETS = ("pressure_drop", "heat_duty", "wall_temperature", "thermal_stress")
ROLES = ("configured", "custom", "supporting_role", "insufficient_evidence")
RANK = ("matched", "needs_input", "needs_development", "partial_scope")
# Definitions apply to every implementation; closure-specific inputs remain on its card.
DEFINITIONS = {
    "pressure_drop": {
        "pressure_stations": "哪两个测压截面、哪个流向？要静压差还是总压损失，如何平均，是否扣除高差或速度头？"
    },
    "heat_duty": {
        "heat_basis": "需要瞬时或稳态换热率 W，还是某一时段累计热量 J？以哪个流体或壁面、什么方向计正？"
    },
    "wall_temperature": {
        "wall_readout": "需要哪一侧、哪个位置的壁温，是局部峰值、面积平均还是测点温度，在哪个时刻或时段？"
    },
    "thermal_stress": {
        "temperature_reference": "材料处于无热应力时的参考温度和初始状态是什么？",
        "mechanical_constraints": "安装和连接怎样限制膨胀，是否有预紧、间隙、机械载荷或接触？",
        "stress_measure": "关注哪个部位、应力分量或等效应力、时刻与评价准则？",
    },
}
QUESTIONS = {
    "geometry": "请给出流道、传热面或构件的尺寸、截面、长度、高差和分区。",
    "flow_rate": "入口流量或可驱动流动的边界给定多少，单位、基准与各支路分配是什么？",
    "fluid_properties": "工质、组成、相态、物性随温度和压力的变化分别是什么？",
    "roughness": "流道粗糙度和局部阻力构件如何确定？",
    "thermal_boundary": "各侧温度、热流或外部换热条件是什么，哪些是已知，哪些待求？",
    "solid_properties": "固体导热率、密度、热容及其温度依赖是什么？",
    "thermal_contact": "层间接触、污垢或涂层热阻有无数据？",
    "elastic_properties": "各材料的弹性模量、泊松比或所需本构和温度依赖是什么？",
    "expansion_coefficient": "各材料的热膨胀系数及适用温区是什么？",
    "inlet_temperature": "各流体入口温度、压力或焓的基准与时间变化是什么？",
    "time_history": "初始状态、载荷或边界历程、关注时段与采样间隔是什么？",
    "heat_sources": "实际输入功率、作用区域、空间和时间分布以及损耗分配是什么？",
    "interface_definition": "两侧接口位置、面法向、空间映射与单位约定是什么？",
    "structural_geometry": "受力构件、厚度、构件连接和载荷作用位置是什么？",
    "temperature_field": "结构各部位的温度场如何取得？需给出时刻、网格和温度基准。",
    "flow_regime": "流态依据是什么，能否用尺寸、流速和物性估算特征数？",
    "wall_treatment": "近壁模型及网格分辨率怎样确定与核验？",
    "permeability": "多孔体的渗透率、各向异性和饱和状态是什么？",
    "heat_transfer_coefficient": "膜系数如何取得，采用哪一侧参考温度，适用的流态与几何是什么？",
    "roughness_and_losses": "管壁粗糙度、各管件的尺寸及局部损失系数如何给定？",
}
CONDITION_QUESTIONS = {
    "fluid_kind": "支路中是气体还是液体？若为气体，请确认工况或标准体积流量的基准温压。",
    "flow_domain": "流体经过开放流道、多孔介质还是可以一维等效的管网？",
    "flow_phase": "工质相态是什么，是否有气液共存？",
    "compressibility": "压力、温度和速度变化是否允许采用不可压近似？",
    "operation": "关注稳定工况还是启动、停机等瞬态过程？",
    "thermal_mode": "需要解析流固两侧、只算固体导热还是采用已知流体换热边界？",
    "mechanics": "变形是否足够小，材料能否视为线弹性，有无塑性或接触开合？",
    "pressure_definition": "需要两截面静压差，还是消除动压和高度影响后的损失？",
    "coupling": "变形或材料随温度变化是否会反馈改变流道、接触或热边界？",
    "phase_change": "工作温度压力范围内有无相变？",
    "interface_model": "流固界面由两侧求解还是给定换热系数？",
}
# A link can supply a required model input only for these field transfers.
PORTS = {
    ("wall_temperature", "thermal_stress", "temperature_field"): ("K", "solid_volume"),
    ("heat_duty", "wall_temperature", "normal_heat_flux"): (
        "W/m2",
        "interface_surface",
    ),
    ("wall_temperature", "heat_duty", "interface_temperature"): (
        "K",
        "interface_surface",
    ),
    ("pressure_drop", "heat_duty", "velocity_field"): ("m/s", "fluid_volume"),
    ("pressure_drop", "wall_temperature", "velocity_field"): ("m/s", "fluid_volume"),
    ("pad_deformation", "tactile_capacitance", "pad_deformation"): ("m", "sensor_layer"),
    ("pad_deformation", "capacitance_change", "pad_deformation"): ("m", "sensor_layer"),
    ("tactile_capacitance", "capacitance_change", "tactile_capacitance"): ("F", "sensor_readout"),
    ("battery_electrical_power", "adhesion_operating_envelope", "operating_case_battery_power"): ("W", "per_operating_point"),
}


def load_targets(db, source):
    db.execute(
        "CREATE TABLE target_readouts(id TEXT PRIMARY KEY,target TEXT NOT NULL,body TEXT NOT NULL)"
    )
    for f in sorted((Path(source) / "readouts").glob("*.json")):
        record = json.loads(f.read_bytes())
        _text_fields(
            record, ("id", "target", "label", "applicability", "conditional_minimum")
        )
        if (
            record["target"] not in TARGETS
            or not record["evidence"]
            or not record["relation"]
            or not record["input_routes"]
        ):
            raise ValueError("Invalid output measurement method")
        for route in record["input_routes"].values():
            if not set(_tokens(route)) <= record["questions"].keys():
                raise ValueError("Missing question for measurement route")
        db.execute(
            "INSERT INTO target_readouts VALUES(?,?,?)",
            (record["id"], record["target"], json.dumps(record, ensure_ascii=False)),
        )
    db.execute("""CREATE TABLE target_profiles(solver TEXT,version TEXT,target TEXT, role TEXT NOT NULL,
                  body TEXT NOT NULL,PRIMARY KEY(solver,version,target))""")
    db.execute(
        "CREATE INDEX target_goal ON target_profiles(target,role,solver,version)"
    )
    directory = Path(source) / "targets"
    for path in sorted(directory.glob("*.json")):
        for row in json.loads(path.read_bytes()):
            for item in [row, *row.get("variants", [])]:
                if any(item[k] != row[k] for k in ("solver", "version", "target")):
                    raise ValueError("Variant changes solver version or target")
                validate_card(db, item)
            target, role = row["target"], row["role"]
            db.execute(
                "INSERT INTO target_profiles VALUES (?,?,?,?,?)",
                (
                    row["solver"],
                    row["version"],
                    target,
                    role,
                    json.dumps(row, ensure_ascii=False),
                ),
            )


def validate_card(db, row):
    target, role = row["target"], row["role"]
    if role not in ROLES or (target not in TARGETS and not db.execute(
        "SELECT 1 FROM chain_goals WHERE goal=?", (target,)
    ).fetchone()):
        raise ValueError("Invalid target or software role")
    _text_fields(
        row,
        (
            "solver",
            "version",
            "model",
            "applicability",
            "exclusions",
            "rationale",
            "custom_work",
        ),
    )
    if not db.execute(
        "SELECT 1 FROM capabilities WHERE solver=? AND version=? LIMIT 1",
        (row["solver"], row["version"]),
    ).fetchone():
        raise ValueError("Unrecognised solver version")
    _tokens(row["inputs"])
    _tokens(row["mechanisms"])
    _conditions(row["conditions"])
    for key in ("checks", "evidence", "exchanges"):
        if not isinstance(row[key], list) or not row[key]:
            raise ValueError(f"Missing {key} for {target}")
    if not all(isinstance(s, str) and s.strip() for s in row["checks"]):
        raise ValueError("Expected engineering checks")
    for link in row["exchanges"]:
        _text_fields(link, ("from", "to", "quantity", "unit", "method"))
    for ref in row["evidence"]:
        _text_fields(ref, ("url", "locator", "claim"))
        if (
            urlsplit(ref["url"]).scheme not in ("https", "http")
            or not urlsplit(ref["url"]).hostname
        ):
            raise ValueError("Invalid source")
    for cap in row.get("related_capabilities", []):
        if not db.execute(
            "SELECT 1 FROM capabilities WHERE id=? AND solver=? AND version=?",
            (cap, row["solver"], row["version"]),
        ).fetchone():
            raise ValueError("Invalid related engineering record")


def inventory(db, solver=None, version=None, target=None):
    filters = {
        k: v
        for k, v in (("solver", solver), ("version", version), ("target", target))
        if v is not None
    }
    where = " WHERE " + " AND ".join(k + "=?" for k in filters) if filters else ""
    return [
        json.loads(r[0])
        for r in db.execute(
            "SELECT body FROM target_profiles"
            + where
            + " ORDER BY target,solver,version",
            tuple(filters.values()),
        )
    ]


def stages_for(req):
    if req.get("domain") != "engineering_targets" or set(req) - {
        "domain",
        "goals",
        "mechanisms",
        "conditions",
        "known_inputs",
        "stages",
        "links",
        "description",
    }:
        raise ValueError("Invalid engineering target request")
    goals = _tokens(req["goals"])
    for key in ("mechanisms", "known_inputs"):
        _tokens(req.get(key, []), empty=True)
    common = req.get("conditions", {})
    if not isinstance(common, dict):
        raise ValueError("Expected condition mapping")
    stages = req.get("stages", [{"goal": g} for g in goals])
    if not isinstance(stages, list) or not stages:
        raise ValueError("Expected target stages")
    ids, result = set(), []
    for s in stages:
        if not isinstance(s, dict) or set(s) - {
            "id",
            "goal",
            "mechanisms",
            "conditions",
            "known_inputs",
        }:
            raise ValueError("Invalid stage")
        goal = _identifier(s["goal"])
        ident = _identifier(s.get("id", goal))
        if ident in ids:
            raise ValueError("Duplicate stage identifier")
        ids.add(ident)
        conditions = {**common, **s.get("conditions", {})}
        if any(
            k in common and common[k] != v for k, v in s.get("conditions", {}).items()
        ):
            raise ValueError("Conflicting common and stage conditions")
        for k, v in conditions.items():
            _identifier(k)
            _identifier(v)
        inputs = set(req.get("known_inputs", [])) | set(
            _tokens(s.get("known_inputs", []), empty=True)
        )
        mechs = set(req.get("mechanisms", [])) | set(
            _tokens(s.get("mechanisms", []), empty=True)
        )
        result.append(
            dict(
                id=ident,
                goal=goal,
                mechanisms=sorted(mechs),
                conditions=conditions,
                known_inputs=sorted(inputs),
            )
        )
    if not set(goals) <= {s["goal"] for s in result}:
        raise ValueError("Missing target stage")
    # Customers request end quantities. Internal fields can be necessary without
    # being deliverables; follow declared transfers back from all requested outputs.
    needed = {s["id"] for s in result if s["goal"] in goals}
    if needed != ids:
        edges = links_for(req, result)
        while True:
            parents = {e["source"] for e in edges if e["target"] in needed}
            if parents <= needed:
                break
            needed.update(parents)
        if needed != ids:
            raise ValueError("An intermediate stage does not feed a requested output; supply directed links with source, target, quantity, unit and location, or omit unconnected stages")
    return result


def links_for(req, stages, aliases=None):
    aliases = aliases or {}
    goals = {s["id"]: aliases.get(s["goal"], {s["goal"]}) for s in stages}
    links = req.get("links", [])
    if not isinstance(links, list):
        raise ValueError("Expected links list")
    result = []
    for link in links:
        if not isinstance(link, dict) or set(link) != {
            "source",
            "target",
            "quantity",
            "unit",
            "location",
        }:
            raise ValueError(
                "Links require source, target, quantity, unit and location"
            )
        a, b, q = (_identifier(link[k]) for k in ("source", "target", "quantity"))
        if a not in goals or b not in goals or a == b:
            raise ValueError("Invalid stage endpoints")
        compatible = all(
            PORTS.get((left, right, q)) == (link["unit"], link["location"])
            for left in goals[a] for right in goals[b]
        )
        result.append(
            dict(
                link,
                status="verify_local" if compatible else "research",
                compatible=compatible,
            )
        )
    return result


def solve_order(stages, transfers):
    """Keep feedback components together, with their upstream dependencies first."""
    upstream = {s["stage"]: set() for s in stages}
    for link in transfers:
        upstream[link["target"]].add(link["source"])

    def ancestors(stage):
        reached, pending = set(), list(upstream[stage])
        while pending:
            node = pending.pop()
            if node not in reached:
                reached.add(node)
                pending.extend(upstream[node] - reached)
        return reached

    predecessors = {node: ancestors(node) for node in upstream}
    groups = {
        tuple(sorted({node} | {p for p in parents if node in predecessors[p]}))
        for node, parents in predecessors.items()
    }
    ordered = []
    done = set()
    while groups:
        ready = sorted(
            g for g in groups if all((upstream[n] - set(g)) <= done for n in g)
        )
        for g in ready:
            ordered.append(
                dict(
                    stages=list(g),
                    mode="iterate_and_check_conservation" if len(g) > 1 else "stage",
                )
            )
            done.update(g)
            groups.remove(g)
    return ordered


def select(db, req, solver=None, version=None, brief=False):
    from . import chain_intake

    stages = stages_for(req)
    plans = chain_intake.discover(db, req, solver, version)
    aliases = {}
    prompts = {i["key"]: i["meaning"] for plan in plans for i in plan["required_inputs"]}
    for plan in plans:
        for name, goal in plan["goal_aliases"].items():
            aliases.setdefault(name, set()).add(goal)
    links = links_for(req, stages, aliases)
    candidates, roles, stage_results = [], [], []
    questions = {}
    measurement_options = []
    available = {
        s["id"]: [
            card
            for canonical in sorted(aliases.get(s["goal"], {s["goal"]}))
            for row in inventory(db, solver, version, canonical)
            for card in (row, *row.get("variants", []))
        ]
        for s in stages
    }
    context = {s["id"]: s for s in stages}

    def compatible(r, s):
        return all(
            k not in s["conditions"] or s["conditions"][k] in vals
            for k, vals in r["conditions"].items()
        )

    for link in links:
        origin = context[link["source"]]
        if link["compatible"] and not any(
            r["role"] in ("configured", "custom") and compatible(r, origin)
            for r in available[origin["id"]]
        ):
            link.update(compatible=False, status="research")
    for s in stages:
        goal, known, cond = s["goal"], set(s["known_inputs"]), s["conditions"]
        if (
            goal == "heat_duty"
            and cond.get("thermal_mode") != "solid_only"
            and cond.get("flow_phase") in ("single_phase", None)
        ):
            for (body,) in db.execute(
                "SELECT body FROM target_readouts WHERE target=? ORDER BY id", (goal,)
            ):
                method = json.loads(body)
                alternatives = method["input_routes"]
                extra = (
                    {"mass_flow_out"} if cond.get("operation") == "transient" else set()
                )
                options = [
                    dict(route=k, missing=sorted((set(v) | extra) - known))
                    for k, v in alternatives.items()
                ]
                missing = min((o["missing"] for o in options), key=len)
                measurement_options.append(
                    dict(
                        stage=s["id"],
                        method=method,
                        available_routes=options,
                        data_status="needs_input" if missing else "inputs_declared",
                        minimum_followup=[method["questions"][k] for k in missing],
                    )
                )
        records = available[s["id"]]
        supplied = {
            l["quantity"] for l in links if l["target"] == s["id"] and l["compatible"]
        }
        group = []
        for r in records:
            base = dict(
                stage=s["id"],
                goal=goal,
                solver=r["solver"],
                version=r["version"],
                model=r["model"],
                implementation=r["role"],
            )
            if r["role"] in ("supporting_role", "insufficient_evidence"):
                roles.append(dict(base, scope=r["applicability"]))
                continue
            criteria = r["conditions"]
            if not compatible(r, s):
                continue
            missing_conditions = sorted(criteria.keys() - cond.keys())
            unknown = sorted(cond.keys() - criteria.keys())
            missing_mech = sorted(set(s["mechanisms"]) - set(r["mechanisms"]))
            # Output definitions and transient data cannot be guessed from a solver model.
            required = set(r["inputs"]) | set(DEFINITIONS.get(goal, {}))
            if cond.get("operation") == "transient":
                required.add("time_history")
            missing = sorted(required - known - supplied)
            if unknown or missing_mech:
                status = "partial_scope"
            elif missing or missing_conditions:
                status = "needs_input"
            else:
                status = "matched" if r["role"] == "configured" else "needs_development"
            group.append(
                dict(
                    base,
                    status=status,
                    conditions=criteria,
                    missing_inputs=missing,
                    missing_conditions=missing_conditions,
                    unreviewed_conditions=unknown,
                    missing_mechanisms=missing_mech,
                    planned_inputs=sorted(required & supplied - known),
                    applicability=r["applicability"],
                    exclusions=r["exclusions"],
                    rationale=r["rationale"],
                    exchanges=r["exchanges"],
                    custom_work=r["custom_work"],
                    checks=r["checks"],
                    evidence=r["evidence"],
                    related_capabilities=r.get("related_capabilities", []),
                )
            )

        def rank(row):
            return (
                RANK.index(row["status"]),
                row["implementation"] != "configured",
                len(row["unreviewed_conditions"]) + len(row["missing_mechanisms"]),
                len(row["missing_conditions"]) + len(row["missing_inputs"]),
            )

        group.sort(key=rank)
        winners = [r for r in group if rank(r) == rank(group[0])] if group else []
        fluid_undecided = (
            goal == "pressure_drop"
            and cond.get("flow_domain") == "network"
            and "fluid_kind" not in cond
            and any("fluid_kind" in r["conditions"] for r in group)
        )
        if fluid_undecided:
            winners = [r for r in group if "fluid_kind" in r["conditions"]]
        if group:
            state = group[0]["status"]
        elif records and all(r["role"] == "supporting_role" for r in records):
            state = "supporting_role"
        elif records and all(
            r["role"] in ("supporting_role", "insufficient_evidence") for r in records
        ):
            state = "insufficient_evidence"
        else:
            state = "no_match"
        stage_results.append(
            dict(
                stage=s["id"],
                goal=goal,
                status="needs_input" if fluid_undecided else state,
                decision_required=["fluid_kind"] if fluid_undecided else [],
                purpose="requested_output"
                if goal in req["goals"]
                else "intermediate_field",
                best=[
                    {
                        "solver": r["solver"],
                        "version": r["version"],
                        "model": r["model"],
                    }
                    for r in winners
                    if not fluid_undecided
                ],
            )
        )
        # Conflicting regimes can exclude every candidate. The output definition
        # remains a valid question even when no implementation is selected.
        needed = {k for r in winners for k in r["missing_inputs"]}
        needed.update(set(DEFINITIONS.get(goal, {})) - known)
        if not winners and goal in TARGETS:
            common_inputs = {
                "pressure_drop": ["geometry", "flow_rate", "fluid_properties"],
                "heat_duty": ["geometry", "thermal_boundary"],
                "wall_temperature": ["geometry", "thermal_boundary"],
                "thermal_stress": [
                    "structural_geometry",
                    "elastic_properties",
                    "expansion_coefficient",
                ],
            }
            needed.update(set(common_inputs[goal]) - known - supplied)
        for k in sorted(needed):
            label = (
                DEFINITIONS.get(goal, {}).get(k)
                or prompts.get(k)
                or QUESTIONS.get(k)
                or f"请明确 {k} 的数值、单位、位置和工况。"
            )
            questions[(s["id"], k)] = dict(stage=s["id"], key=k, question=label)
        for k in sorted({k for r in winners for k in r["missing_conditions"]}):
            questions.setdefault(
                (s["id"], k),
                dict(
                    stage=s["id"],
                    key=k,
                    question=CONDITION_QUESTIONS.get(k, f"请明确 {k} 适用条件。"),
                ),
            )
        candidates.extend(winners if brief else group)
    # A field interface is a proposed transfer requiring producer and consumer validation.
    # Keep a requested physical input visible until it is supplied or validated.
    states = [s["status"] for s in stage_results]
    status = states[0] if len(set(states)) == 1 else "partial_scope"
    if links and status == "matched":
        status = (
            "partial_scope"
            if any(not l["compatible"] for l in links)
            else "needs_verification"
        )
    return dict(
        status=status,
        domain="engineering_targets",
        stages=stage_results,
        candidates=candidates,
        other_roles=roles,
        transfers=links,
        questions=list(questions.values()),
        measurement_options=measurement_options,
        engineering_plans=chain_intake.compact(plans) if brief else plans,
    )
