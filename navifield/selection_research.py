"""Attach privacy-minimized, context-bound gaps to selection results."""

import hashlib
import json
import re

_TOKEN = re.compile(r"[A-Za-z0-9_.-]+\Z")
_UNIT = re.compile(r"[A-Za-z0-9_./^+-]+\Z")


def _id(v):
    if not isinstance(v, str) or not _TOKEN.fullmatch(v):
        raise ValueError("Expected controlled identifier")
    return v


def _ids(value):
    if not isinstance(value, list):
        raise ValueError("Expected controlled identifier list")
    return sorted(set(_id(x) for x in value))


def _conditions(value):
    if not isinstance(value, dict):
        raise ValueError("Expected controlled conditions")
    return {_id(k): _id(v) for k, v in sorted(value.items())}


def _context(req, entrypoint, solver, version, public_tokens=None):
    if not isinstance(req, dict):
        raise ValueError("Expected request object")
    ctx = {
        "entrypoint": _id(entrypoint),
        "domain": _id(req["domain"]),
        "goals": _ids(req.get("goals", [])),
        "mechanisms": _ids(req.get("mechanisms", [])),
        "conditions": _conditions(req.get("conditions", {})),
        "known_inputs": _ids(req.get("known_inputs", [])),
        "solver": _id(solver) if solver is not None else None,
        "version": _id(version) if version is not None else None,
    }
    stages = req.get("stages", [])
    if not isinstance(stages, list):
        raise ValueError("Expected stages")
    normalized = []
    for s in stages:
        if not isinstance(s, dict) or set(s) - {
            "id",
            "goal",
            "mechanisms",
            "conditions",
            "known_inputs",
        }:
            raise ValueError("Invalid stage")
        normalized.append(
            {
                "id": _id(s.get("id", s.get("goal"))),
                "goal": _id(s["goal"]),
                "mechanisms": _ids(s.get("mechanisms", [])),
                "conditions": _conditions(s.get("conditions", {})),
                "known_inputs": _ids(s.get("known_inputs", [])),
            }
        )
    ctx["stages"] = sorted(normalized, key=lambda x: x["id"])
    links = req.get("links", [])
    if not isinstance(links, list):
        raise ValueError("Expected links")
    out = []
    for link in links:
        if not isinstance(link, dict) or set(link) != {
            "source",
            "target",
            "quantity",
            "unit",
            "location",
        }:
            raise ValueError("Invalid link")
        d = {k: _id(link[k]) for k in ("source", "target", "quantity", "location")}
        if not isinstance(link["unit"], str) or not _UNIT.fullmatch(link["unit"]):
            raise ValueError("Invalid link unit")
        d["unit"] = link["unit"]
        out.append(d)
    ctx["links"] = sorted(
        out,
        key=lambda x: tuple(
            x[k] for k in ("source", "target", "quantity", "unit", "location")
        ),
    )
    # No caller-provided free text or unregistered identifiers leave this boundary.
    public = set(public_tokens or ()) | {
        "engineering_targets",
        "phase_change",
        "flow",
        "conduction",
        "convection",
        "thermal_expansion",
        "elasticity",
        "boiling",
        "condensation",
        "radiation",
        "pressure_drop",
        "heat_duty",
        "wall_temperature",
        "thermal_stress",
        "outlet_net_vapor",
        "unresolved_film",
        "condensation_with_ncg",
        "steady",
        "transient",
        "single_phase",
        "multiphase",
        "free_fluid",
        "porous",
        "solid_only",
        "fluid_only",
        "conjugate",
        "linear_elastic",
        "nonlinear",
        "one_way",
        "two_way",
        "static_difference",
        "total_loss",
        "K",
        "Pa",
        "W",
        "m",
        "m/s",
        "W/m2",
        "solid_volume",
        "fluid_volume",
        "interface_surface",
        "condensate_mass_flow",
        "condensation_heat_duty",
        "net_vapor_mass_flow",
        "geometry",
        "select_targets",
        "select_models",
        "select_chains",
        "elmer",
        "calculix",
        "opengeosys",
        "openfoam_opencfd",
        "moose",
        "dolfinx",
        "getdp",
        "gmsh",
        "precice",
        "temperature_field",
        "normal_heat_flux",
        "interface_temperature",
        "velocity_field",
    }

    def safe(token):
        if token is None or token in public:
            return token
        return "opaque_" + hashlib.sha256(token.encode()).hexdigest()[:16]

    # Stage names are supplied by the caller and need not be public.
    stage_map = {s["id"]: "stage_" + str(i + 1) for i, s in enumerate(ctx["stages"])}
    ctx["stage_identity"] = hashlib.sha256(
        json.dumps(sorted(stage_map), separators=(",", ":")).encode()
    ).hexdigest()[:16]
    ctx["stage_map"] = stage_map
    for k in ("entrypoint", "domain", "solver", "version"):
        ctx[k] = safe(ctx[k])
    ctx["goals"] = [safe(v) for v in ctx["goals"]]
    ctx["mechanisms"] = [safe(v) for v in ctx["mechanisms"]]
    ctx["known_inputs"] = [safe(v) for v in ctx["known_inputs"]]
    ctx["conditions"] = {safe(k): safe(v) for k, v in ctx["conditions"].items()}
    for s in ctx["stages"]:
        s["id"] = stage_map[s["id"]]
        s["goal"] = safe(s["goal"])
        s["mechanisms"] = [safe(v) for v in s["mechanisms"]]
        s["known_inputs"] = [safe(v) for v in s["known_inputs"]]
        s["conditions"] = {safe(k): safe(v) for k, v in s["conditions"].items()}
    for link in ctx["links"]:
        link["source"] = stage_map.get(link["source"], safe(link["source"]))
        link["target"] = stage_map.get(link["target"], safe(link["target"]))
        for key in ("quantity", "unit", "location"):
            link[key] = safe(link[key])
    ctx["_public"] = public
    return ctx


def attach(
    result,
    request,
    entrypoint,
    solver,
    version,
    emit_gaps=None,
    with_evidence=None,
    research_cache=None,
    public_tokens=None,
):
    """Mutate/return result; findings are annotations and never alter selection status."""
    context = _context(request, entrypoint, solver, version, public_tokens)
    public = context.pop("_public")
    stage_map = context.pop("stage_map")

    def safe(v):
        _id(v)
        return (
            v
            if v in public
            else "opaque_" + hashlib.sha256(v.encode()).hexdigest()[:16]
        )

    scope = (
        "context:"
        + hashlib.sha256(
            json.dumps(context, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:16]
    )
    gaps = {}

    def add(kind, quantity, action, stage=None, owner=None, edge=None, question=None):
        _id(kind)
        _id(quantity)
        quantity = safe(quantity)
        if stage is not None:
            stage = stage_map.get(stage, safe(stage))
        owner = owner or {}
        bound_solver = owner.get("solver") or solver
        bound_version = owner.get("version") or version
        owner_model = owner.get("model") or owner.get("chain")
        model_token = (
            "model_" + hashlib.sha256(str(owner_model).encode()).hexdigest()[:16]
            if owner_model
            else None
        )
        bound_scope = scope + (
            ":"
            + hashlib.sha256(
                str((bound_solver, bound_version, owner_model)).encode()
            ).hexdigest()[:12]
            if owner
            else ""
        )
        identity = {
            "kind": kind,
            "quantity": quantity,
            "context": context,
            "scope": bound_scope,
            "version": safe(bound_version) if bound_version else "unspecified",
            "stage": stage,
            "solver": safe(bound_solver) if bound_solver else None,
            "model": model_token,
            "edge": edge,
        }
        ident = (
            "gap-"
            + hashlib.sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()[:20]
        )
        gaps[ident] = {
            "id": ident,
            **identity,
            "action": action,
            "question": question or {
                "research": "Identify documented methods and their scope.",
                "ask_user": "Obtain the missing or conflicting input.",
                "verify_local": "Verify local implementation and result for this scope.",
            }[action],
        }

        if action == "research" and owner_model:
            # Model labels and URLs come from the versioned local result, never the intake.
            gaps[ident]["record"] = owner_model
            refs = owner.get("evidence") or owner.get("sources") or []
            gaps[ident]["starting_sources"] = [
                r["url"] for r in refs if isinstance(r, dict) and "url" in r
            ][:3]

    for plan in result.get("engineering_plans", []):
        for key, prompt in plan.get("research_needs", {}).items():
            add("unverified_closure", key, "research", owner=plan, question=prompt)
    candidates = result.get("candidates", [])
    if not isinstance(candidates, list):
        raise ValueError("Expected candidates")
    stages = result.get("stages", [])
    for stage in stages:
        sid, goal = _id(stage["stage"]), _id(stage["goal"])
        group = [c for c in candidates if c.get("stage") == sid]
        if not group:
            add("unknown_quantity", goal, "research", sid)
    if not stages and not candidates:
        for goal in request["goals"]:
            add("unknown_quantity", goal, "research")
    # Restrict research to equally best candidates per stage, not every lower-ranked alternative.
    rank = {"matched": 0, "needs_input": 1, "needs_development": 2, "partial_scope": 3}

    def ordering(c):
        return (
            rank.get(c.get("status"), 9),
            sum(
                len(c.get(k, []))
                for k in (
                    "missing_inputs",
                    "missing_conditions",
                    "missing_mechanisms",
                    "unreviewed_conditions",
                    "missing_goals",
                )
            ),
        )

    best = {}
    for c in candidates:
        key = c.get("stage")
        best[key] = min(best.get(key, ordering(c)), ordering(c))
    selected = {
        s["stage"]: {
            (b["solver"], b["version"], b.get("model")) for b in s.get("best", [])
        }
        for s in stages
    }
    for c in candidates:
        key = c.get("stage")
        if selected.get(key):
            if (c["solver"], c["version"], c.get("model")) not in selected[key]:
                continue
        elif ordering(c) != best[key]:
            continue
        sid = c.get("stage")
        for field, kind, action in (
            ("missing_inputs", "required_input", "ask_user"),
            ("missing_conditions", "condition_unconfirmed", "ask_user"),
            ("missing_mechanisms", "unknown_mechanism", "research"),
            ("unreviewed_conditions", "condition_unreviewed", "research"),
            ("missing_goals", "unknown_quantity", "research"),
        ):
            for item in c.get(field, []):
                add(kind, _id(item), action, sid, c)
        if (
            c.get("implementation") == "custom"
            or c.get("status") == "needs_development"
        ):
            quantity = (
                c.get("goal")
                or c.get("chain")
                or (context["goals"][0] if context["goals"] else "method")
            )
            add("custom_method", _id(quantity), "research", sid, c)
        elif c.get("implementation") == "configured" or c.get("status") == "matched":
            quantity = (
                c.get("goal")
                or c.get("chain")
                or (context["goals"][0] if context["goals"] else "method")
            )
            add("integration_unverified", _id(quantity), "verify_local", sid, c)
    for transfer in result.get("transfers", []):
        if transfer.get("status") in ("research", "verify_local"):
            link = transfer.get("link", transfer)
            quantity = _id(link["quantity"])
            action = transfer["status"]
            edge = {
                "source": stage_map.get(
                    link.get("source"),
                    safe(link["source"]) if link.get("source") else None,
                ),
                "target": stage_map.get(
                    link.get("target"),
                    safe(link["target"]) if link.get("target") else None,
                ),
                "unit": link.get("unit"),
                "location": link.get("location"),
            }
            for k in ("unit", "location"):
                if edge[k] is not None and edge[k] not in public:
                    edge[k] = (
                        "opaque_" + hashlib.sha256(edge[k].encode()).hexdigest()[:16]
                    )
            add(
                "unknown_interface"
                if action == "research"
                else "integration_unverified",
                quantity,
                action,
                edge=edge,
            )
    gap_list = list(gaps.values())
    findings = []
    cache_hits = []
    if research_cache is not None or with_evidence is not None:
        from .capability_kb import (
            bind_findings,
            load_cache,
            store_cache,
            cache_eligible,
        )

        if isinstance(with_evidence, (str, bytes)) or hasattr(
            with_evidence, "__fspath__"
        ):
            from pathlib import Path

            with_evidence = json.loads(
                Path(with_evidence).read_text(encoding="utf-8-sig")
            )
        explicit = (
            bind_findings(gap_list, with_evidence) if with_evidence is not None else []
        )
        if research_cache is not None:
            if with_evidence is not None:
                store_cache(research_cache, with_evidence["findings"])
            cached = load_cache(research_cache, request, {"gaps": gap_list})
            explicit_ids = (
                {f["gap_id"] for f in with_evidence["findings"]}
                if with_evidence
                else set()
            )
            cached = [f for f in cached if f["gap_id"] not in explicit_ids]
            cache_hits = [f["gap_id"] for f in cached]
            findings.extend(
                bind_findings(gap_list, {"schema_version": 1, "findings": cached})
            )
        findings.extend(explicit)
        answered = set(cache_hits)
        if with_evidence:
            answered.update(
                f["gap_id"] for f in with_evidence["findings"] if cache_eligible(f)
            )
    else:
        answered = set()
    packet_gaps = [
        g for g in gap_list if g["action"] == "research" and g["id"] not in answered
    ]
    result["cache_hits"] = cache_hits
    result["gaps"] = [{k: v for k, v in g.items() if k != "context"} for g in gap_list]
    result["gap_context"] = context
    result["research_findings"] = findings
    result["research_packet"] = {
        "schema_version": 1,
        "context": context if packet_gaps else None,
        "gaps": [{k: v for k, v in g.items() if k != "context"} for g in packet_gaps],
    }
    if emit_gaps is not None:
        from pathlib import Path

        Path(emit_gaps).write_text(
            json.dumps(result["research_packet"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return result
