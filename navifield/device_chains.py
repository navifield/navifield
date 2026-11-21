"""Inspect and match evidenced equation chains for coupled equipment requirements."""

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from .model_selection import _conditions, _identifier, _request, _text_fields, _tokens

SUPPORT = (
    "configured",
    "custom",
    "coupling_only",
    "mesh_only",
    "insufficient_evidence",
)
METHODS = ("native", "configured", "custom", "external")
STATUSES = (
    "matched",
    "needs_development",
    "needs_input",
    "partial_scope",
    "insufficient_evidence",
    "supporting_role",
)


def _inputs(items):
    _tokens([r["key"] for r in items], empty=True)
    for r in items:
        _text_fields(r, ("meaning",))


def _strings(values, empty=False):
    if (
        not isinstance(values, list)
        or (not values and not empty)
        or not all(isinstance(s, str) and s.strip() for s in values)
    ):
        raise ValueError("Expected nonempty text entries")


def _check_chain(c):
    _text_fields(c, ("id", "scope"))
    if c["status"] not in SUPPORT:
        raise ValueError(f"Unknown implementation for {c['id']}")
    _inputs(c["required_inputs"])
    benchmarks = c.get("benchmarks", [])
    _tokens([entry["id"] for entry in benchmarks], empty=True)
    for entry in benchmarks:
        _text_fields(entry, ("title", "kind", "source_url", "source_locator", "native_status"))
        url = urlsplit(entry["source_url"])
        if url.scheme != "https" or not url.hostname:
            raise ValueError(f"Invalid benchmark source for {c['id']}")
    adapter = c.get("native_adapter")
    if adapter:
        _identifier(adapter["id"])
        _text_fields(adapter, ("prepare_command", "execution_command"))
    for k in ("custom_work", "checks"):
        _strings(c[k])
    sources = set(_tokens([s["id"] for s in c["sources"]]))
    for s in c["sources"]:
        _text_fields(s, ("kind", "locator", "url", "claim"))
        u = urlsplit(s["url"])
        if (
            u.scheme not in ("http", "https")
            or not u.netloc
            or s["kind"]
            not in (
                "version_source",
                "version_manual",
                "upstream_case",
                "physics_reference",
            )
        ):
            raise ValueError(f"Invalid source for {c['id']}")
    if not any(s["kind"] in ("version_source", "version_manual") for s in c["sources"]):
        raise ValueError("A pinned manual or implementation is required")
    seen = set()
    _tokens([s["id"] for s in c["steps"]])
    for s in c["steps"]:
        _text_fields(s, ("operation", "equation"))
        for key in ("receives", "produces"):
            _strings(s[key])
        if "ports" in s:
            for direction in ("inputs", "outputs"):
                _tokens(s["ports"][direction])
        deps = set(_tokens(s["depends_on"], empty=True))
        if not deps <= seen:
            raise ValueError(f"Forward or unknown dependency in {c['id']}: {s['id']}")
        seen.add(s["id"])
    if not c["closures"]:
        raise ValueError("Missing closure or description of its external owner")
    for item in c["closures"]:
        _text_fields(item, ("name", "relation", "applicability"))
    for item in [*c["steps"], *c["closures"]]:
        if (
            item["implementation"] not in METHODS
            or not set(_tokens(item["evidence"])) <= sources
        ):
            raise ValueError(f"Unknown implementation or source in {c['id']}")
        if c["status"] == "configured" and item["implementation"] == "custom":
            raise ValueError(
                f"Configured chain contains an unimplemented component: {c['id']}"
            )


def load_chains(db, source):
    db.executescript("""
        CREATE TABLE chain_specs(domain TEXT, chain TEXT, body TEXT NOT NULL, PRIMARY KEY(domain,chain));
        CREATE TABLE chain_goals(domain TEXT, goal TEXT, chain TEXT,
          PRIMARY KEY(domain,goal,chain), FOREIGN KEY(domain,chain) REFERENCES chain_specs);
        CREATE TABLE device_chains(domain TEXT,solver TEXT,version TEXT,chain TEXT,
          implementation TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(domain,solver,version,chain),
          FOREIGN KEY(domain,chain) REFERENCES chain_specs);
        CREATE INDEX device_chain_lookup ON device_chains(domain,chain,implementation,solver,version);
        CREATE TABLE chain_steps(domain TEXT,solver TEXT,version TEXT,chain TEXT,step TEXT,ordinal INTEGER,
          body TEXT NOT NULL,PRIMARY KEY(domain,solver,version,chain,step),
          FOREIGN KEY(domain,solver,version,chain) REFERENCES device_chains);
        CREATE TABLE chain_dependencies(domain TEXT,solver TEXT,version TEXT,chain TEXT,step TEXT,requires TEXT,
          PRIMARY KEY(domain,solver,version,chain,step,requires),
          FOREIGN KEY(domain,solver,version,chain,step) REFERENCES chain_steps,
          FOREIGN KEY(domain,solver,version,chain,requires) REFERENCES chain_steps);
    """)
    root = Path(source) / "chains"
    if not root.exists():
        return
    specs = json.loads((root / "catalog.json").read_text("utf-8-sig"))
    domains, intakes = {}, {}
    for p in specs:
        cid, domain = _identifier(p["id"]), _identifier(p["domain"])
        if cid in domains:
            raise ValueError(f"Duplicate or ambiguous chain ID: {cid}")
        domains[cid] = domain
        _text_fields(p, ("title",))
        _conditions(p["conditions"])
        _inputs(p["inputs"])
        for goal, extra in p.get("goal_inputs", {}).items():
            if goal not in p["goals"]:
                raise ValueError(f"Unknown goal for input rule in {cid}: {goal}")
            _inputs(extra)
        for rule in p.get("inputs_when", []):
            values = set(_tokens(rule["values"]))
            if not values <= set(p["conditions"].get(rule["condition"], [])):
                raise ValueError(f"Unknown conditional input regime in {cid}")
            _inputs(rule["inputs"])
        _tokens(p["mechanisms"])
        if "intake" in p:
            intake = p["intake"]
            adapter = intake.get("adapter")
            if adapter is not None and adapter != "confirmed_axial_strip":
                raise ValueError(f"Unknown native intake adapter for {cid}: {adapter}")
            groups = intake.get("all_of", [])
            if not isinstance(groups, list) or (not groups and adapter is None):
                raise ValueError(f"Missing physical selection criteria for {cid}")
            for alternatives in groups:
                _strings(alternatives)
            for key, pattern in intake.get("pattern_groups", {}).items():
                if not key.isdigit() or int(key) >= len(groups):
                    raise ValueError(f"Unknown text pattern group in {cid}: {key}")
                re.compile(pattern)
            for condition, options in intake.get("condition_patterns", {}).items():
                if not set(options) <= set(intake.get("condition_evidence", {}).get(condition, ())):
                    raise ValueError(f"Unknown condition pattern in {cid}: {condition}")
                for pattern in options.values():
                    re.compile(pattern)
            _strings(intake.get("exclude", []), empty=True)
            _strings(intake.get("research_scope_terms", []), empty=True)
            intakes[cid] = intake
            reference = intake.get("research_reference")
            if reference:
                if not re.fullmatch(r"10\.\d{4,9}/[a-z0-9._/-]+", reference["doi"]):
                    raise ValueError(f"Invalid research DOI in {cid}")
                _strings(reference["intent_terms"])
            _strings(list(intake.get("terminal_scope", {}).values()), empty=True)
            for goal, step in intake.get("goal_terminal_steps", {}).items():
                if goal not in p["goals"]:
                    raise ValueError(f"Unknown terminal-step goal in {cid}: {goal}")
                _identifier(step)
            for key, alternatives in intake.get("condition_evidence", {}).items():
                if not set(alternatives) <= set(p["conditions"].get(key, [])):
                    raise ValueError(f"Unknown evidenced condition for {cid}: {key}")
                for terms in alternatives.values():
                    _strings(terms)
            for choice in intake.get("application_choices", []):
                _identifier(choice["application"])
                if not all(value in p["conditions"].get(key, ()) for key, value in choice.items()
                           if key != "application"):
                    raise ValueError(f"Unknown application condition in {cid}")
            for case_id, terms in intake.get("paper_case_terms", {}).items():
                _identifier(case_id)
                _strings(terms)
            declared_inputs = [*p["inputs"]]
            for inputs in p.get("goal_inputs", {}).values():
                declared_inputs.extend(inputs)
            for rule in intake.get("research_needs_by_condition", []):
                values = set(_tokens(rule["values"]))
                if not values <= set(p["conditions"].get(rule["condition"], ())):
                    raise ValueError(f"Unknown conditional research regime in {cid}")
                _tokens(list(rule["needs"]), empty=True)
                _strings(list(rule["needs"].values()), empty=True)
            known_input_keys = {item["key"] for item in declared_inputs}
            for field in ("input_evidence", "ambiguous_input_evidence"):
                for key, pattern in intake.get(field, {}).items():
                    if key not in known_input_keys:
                        raise ValueError(f"Unknown evidence input in {cid}: {key}")
                    re.compile(pattern)
            for mode, evidence in intake.get("excitation_evidence", {}).items():
                if mode not in p["conditions"].get("excitation", []):
                    raise ValueError(f"Unknown excitation evidence mode in {cid}: {mode}")
                _identifier(evidence["input"])
                _strings(evidence["mode_terms"])
                re.compile(evidence["value_pattern"])
            for condition, question in intake.get("condition_questions", {}).items():
                if condition not in p["conditions"]:
                    raise ValueError(f"Unknown question condition in {cid}: {condition}")
                _inputs([question])
            for alias, canonical in intake.get("goal_aliases", {}).items():
                _identifier(alias)
                if canonical not in p["goals"]:
                    raise ValueError(f"Unknown canonical goal in {cid}: {canonical}")
            for cap in _tokens(intake["capability_ids"], empty=True):
                if not db.execute("SELECT 1 FROM capabilities WHERE id=?", (cap,)).fetchone():
                    raise ValueError(f"Unknown component in {cid}: {cap}")
        db.execute(
            "INSERT INTO chain_specs VALUES (?,?,?)",
            (domain, cid, json.dumps(p, ensure_ascii=False)),
        )
        db.executemany(
            "INSERT INTO chain_goals VALUES (?,?,?)",
            [(domain, g, cid) for g in _tokens(p["goals"])],
        )
    for path in sorted(root.glob("*.json")):
        if path.name == "catalog.json":
            continue
        data = json.loads(path.read_text("utf-8-sig"))
        owner = (data["solver"], data["version"])
        if not db.execute(
            "SELECT 1 FROM capabilities WHERE solver=? AND version=? LIMIT 1", owner
        ).fetchone():
            raise ValueError(f"Unknown solver/version {owner}")
        for c in data["chains"]:
            _check_chain(c)
            cid = c["id"]
            intake = intakes.get(cid, {})
            adapter = c.get("native_adapter")
            if (adapter["id"] if adapter else None) != intake.get("adapter"):
                raise ValueError(f"Native adapter does not match intake for {cid}")
            if adapter and any("ports" not in step for step in c["steps"]):
                raise ValueError(f"Native adapter steps require field ports in {cid}")
            step_ids = {step["id"] for step in c["steps"]}
            terminal_ids = set(intake.get("goal_terminal_steps", {}).values())
            if not terminal_ids <= step_ids or not set(intake.get("terminal_scope", {})) <= terminal_ids:
                raise ValueError(f"Unknown goal terminal step in {cid}")
            step_capabilities = intake.get("step_capabilities")
            if step_capabilities is not None:
                if set(step_capabilities) != step_ids or {
                    capability for ids in step_capabilities.values() for capability in _tokens(ids)
                } != set(intake["capability_ids"]):
                    raise ValueError(f"Step capability mapping does not match the declared chain in {cid}")
            key = (domains[cid], *owner, cid)
            db.execute(
                "INSERT INTO device_chains VALUES (?,?,?,?,?,?)",
                (*key, c["status"], json.dumps(c, ensure_ascii=False)),
            )
            for i, s in enumerate(c["steps"]):
                db.execute(
                    "INSERT INTO chain_steps VALUES (?,?,?,?,?,?,?)",
                    (*key, s["id"], i, json.dumps(s, ensure_ascii=False)),
                )
                db.executemany(
                    "INSERT INTO chain_dependencies VALUES (?,?,?,?,?,?)",
                    [(*key, s["id"], dep) for dep in s["depends_on"]],
                )


def inventory(db, solver=None, version=None, chain=None):
    criteria = {
        k: v
        for k, v in (("solver", solver), ("version", version), ("chain", chain))
        if v is not None
    }
    sql = (
        "SELECT c.domain,c.solver,c.version,c.chain,c.implementation,c.body,p.body FROM device_chains c "
        "JOIN chain_specs p USING (domain,chain)"
    )
    if criteria:
        sql += " WHERE " + " AND ".join("c." + k + "=?" for k in criteria)
    result = []
    for d, s, v, c, role, body, spec in db.execute(
        sql + " ORDER BY c.solver,c.version,c.chain", tuple(criteria.values())
    ):
        item = json.loads(body)
        entry = dict(
            domain=d,
            solver=s,
            version=v,
            chain=c,
            implementation=role,
            scope=item["scope"],
        )
        if item.get("native_adapter"):
            entry["native_adapter"] = item["native_adapter"]
        if chain is not None:
            if item.get("benchmarks"):
                entry["benchmarks"] = item["benchmarks"]
            p = json.loads(spec)
            inputs = {r["key"]: r for r in [*p["inputs"], *item["required_inputs"]]}
            entry.update(
                goals=p["goals"],
                mechanisms=p["mechanisms"],
                conditions=p["conditions"],
                steps=item["steps"],
                closures=item["closures"],
                required_inputs=list(inputs.values()),
                conditional_inputs=p.get("inputs_when", []),
                goal_inputs=p.get("goal_inputs", {}),
                custom_work=item["custom_work"],
                checks=item["checks"],
                sources=item["sources"],
            )
        result.append(entry)
    return result


def select(db, req, solver=None, version=None, brief=False):
    _request(req)
    goals = req["goals"]
    sql = (
        "SELECT DISTINCT p.body,c.solver,c.version,c.body FROM chain_goals g "
        "JOIN chain_specs p USING(domain,chain) JOIN device_chains c USING(domain,chain) "
        "WHERE g.domain=? AND g.goal IN (" + ",".join("?" for _ in goals) + ")"
    )
    args = [req["domain"], *goals]
    for key, value in (("solver", solver), ("version", version)):
        if value is not None:
            sql += " AND c." + key + "=?"
            args.append(value)
    given = req.get("conditions", {})
    known = set(req.get("known_inputs", []))
    candidates, roles = [], []
    for spec_json, s, v, record_json in db.execute(sql, args):
        p, c = json.loads(spec_json), json.loads(record_json)
        if not set(req["mechanisms"]).intersection(p["mechanisms"]):
            continue
        criteria = p["conditions"]
        if any(k in given and given[k] not in vals for k, vals in criteria.items()):
            continue
        role = c["status"]
        summary = dict(solver=s, version=v, chain=c["id"], implementation=role)
        if role in ("mesh_only", "coupling_only", "insufficient_evidence"):
            roles.append(dict(summary, scope=c["scope"]))
            continue
        required = {
            r["key"]: r["meaning"] for r in [*p["inputs"], *c["required_inputs"]]
        }
        for rule in p.get("inputs_when", []):
            if given.get(rule["condition"]) in rule["values"]:
                required.update(
                    (item["key"], item["meaning"]) for item in rule["inputs"]
                )
        for goal in goals:
            required.update((i["key"], i["meaning"]) for i in p.get("goal_inputs", {}).get(goal, []))
        missing_inputs = sorted(required.keys() - known)
        missing_conditions = sorted(criteria.keys() - given.keys())
        unreviewed = sorted(given.keys() - criteria.keys())
        missing_goals = sorted(set(goals) - set(p["goals"]))
        missing_mechanisms = sorted(set(req["mechanisms"]) - set(p["mechanisms"]))
        if unreviewed or missing_goals or missing_mechanisms:
            state = "partial_scope"
        elif missing_inputs or missing_conditions:
            state = "needs_input"
        else:
            state = "matched" if role == "configured" else "needs_development"
        candidates.append(
            dict(
                summary,
                status=state,
                title=p["title"],
                scope=c["scope"],
                missing_inputs=missing_inputs,
                missing_conditions=missing_conditions,
                unreviewed_conditions=unreviewed,
                missing_goals=missing_goals,
                missing_mechanisms=missing_mechanisms,
                steps=c["steps"],
                closures=c["closures"],
                custom_work=c["custom_work"],
                checks=c["checks"],
                sources=c["sources"],
            )
        )

    def rank(c):
        gaps = sum(
            len(c[k])
            for k in (
                "missing_inputs",
                "missing_conditions",
                "unreviewed_conditions",
                "missing_goals",
                "missing_mechanisms",
            )
        )
        return (
            STATUSES.index(c["status"]),
            gaps,
            c["implementation"] != "configured",
            c["solver"],
            c["chain"],
        )

    candidates.sort(key=rank)
    roles.sort(key=lambda r: (r["solver"], r["chain"]))
    if candidates:
        state = candidates[0]["status"]
    elif any(r["implementation"] == "insufficient_evidence" for r in roles):
        state = "insufficient_evidence"
    else:
        state = "supporting_role" if roles else "no_match"
    result = dict(
        domain=req["domain"], status=state, candidates=candidates, other_roles=roles
    )
    if brief:
        result["candidate_count"] = len(candidates)
        best = rank(candidates[0])[:3] if candidates else None
        compact = []
        for c in candidates:
            if rank(c)[:3] != best:
                break
            row = {
                k: v
                for k, v in c.items()
                if k not in ("steps", "closures", "checks", "sources")
            }
            row["steps"] = [i["operation"] for i in c["steps"]]
            row["sources"] = list(dict.fromkeys(s["url"] for s in c["sources"]))
            compact.append(row)
        result["candidates"] = compact
        result["other_roles"] = [
            {k: v for k, v in r.items() if k != "scope"} for r in roles
        ]
    return result
