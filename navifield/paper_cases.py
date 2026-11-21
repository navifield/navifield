"""Index primary-paper cases independently of locally supported solver capabilities."""

import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from . import device_chains


SOFTWARE = {
    "fluent": "Fluent",
    "ansys fluent": "Fluent",
    "comsol": "COMSOL",
    "ansys": "ANSYS",
    "ansys mechanical": "ANSYS",
    "ansys 11": "ANSYS",
    "comsol multiphysics": "COMSOL",
    "未确认": "未确认",
    "unknown": "未确认",
    "unconfirmed": "未确认",
}
KINDS = {"measured", "derived", "simulated", "comparison"}
SUMMARY = ("id", "doi", "title", "year", "software", "software_version", "industry", "targets")


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _doi_key(value):
    value = value.strip().casefold()
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value)


def explicit_dois(text):
    return {doi.rstrip(".").casefold()
            for doi in re.findall(r"(?<![a-z0-9])10\.\d{4,9}/[a-z0-9._/-]+", text, re.IGNORECASE | re.ASCII)}


def _software_key(value):
    key = " ".join(value.casefold().split())
    return SOFTWARE.get(key, key).casefold()


def _validate(row):
    for key in ("id", "title", "authors", "industry", "source_url", "source_locator", "provenance"):
        if not _text(row.get(key)):
            raise ValueError(f"Missing or empty {key}")
    if "doi" not in row or (row["doi"] is not None and (
            not isinstance(row["doi"], str) or not re.fullmatch(r"10\.\d{4,9}/\S+", row["doi"], re.IGNORECASE))):
        raise ValueError("Expected a bare DOI or null")
    url = urlsplit(row["source_url"])
    if url.scheme != "https" or not url.hostname:
        raise ValueError("Expected a full HTTPS source_url")
    if type(row.get("year")) is not int or not 1000 <= row["year"] <= 9999:
        raise ValueError("Expected a four-digit publication year")
    if not _text(row.get("software")):
        raise ValueError("Expected a software name or 未确认")
    if "software_version" not in row or not (row["software_version"] is None or _text(row["software_version"])):
        raise ValueError("Expected a software_version string or null")
    for key in ("conditions", "model_chain", "required_inputs", "limitations", "targets"):
        values = row.get(key)
        if not isinstance(values, list) or not values or not all(_text(v) for v in values):
            raise ValueError(f"Expected nonempty text list for {key}")
    if any(not re.fullmatch(r"[a-z][a-z0-9_]*", target) for target in row["targets"]):
        raise ValueError("Expected lower-case target identifiers")
    keywords = row.get("keywords", [])
    if not isinstance(keywords, list) or not all(_text(item) for item in keywords):
        raise ValueError("Expected a keyword list")
    observations = row.get("observations")
    if not isinstance(observations, list) or not observations:
        raise ValueError("Expected an observations list")
    for obs in observations:
        if (not isinstance(obs, dict) or obs.get("kind") not in KINDS
                or not all(_text(obs.get(key)) for key in ("quantity", "detail", "locator"))):
            raise ValueError("Each observation needs kind, quantity, detail and locator")
    if "reproduction" in row:
        reproduction = row["reproduction"]
        if (not isinstance(reproduction, dict)
                or not all(_text(reproduction.get(k)) for k in ("scope", "status"))
                or not all(isinstance(reproduction.get(k), list) and reproduction[k]
                           for k in ("brief_results", "gaps"))
                or not isinstance(reproduction.get("routes"), list)
                or not isinstance(reproduction.get("cases"), list)
                or (bool(reproduction["cases"]) == (reproduction["status"] == "not_run"))):
            raise ValueError("Expected reproduction scope, status, routes, cases, brief_results and gaps")
        if not all(isinstance(route, dict) and all(_text(route.get(k)) for k in ("chain", "solver", "version"))
                   and ("gap" not in route or _text(route["gap"]))
                   and type(route.get("requires_explicit_doi", False)) is bool
                   and type(route.get("requires_case_reference", False)) is bool
                   and (row["doi"] is not None or not route.get("requires_explicit_doi"))
                   for route in reproduction["routes"]):
            raise ValueError("Each reproduction route needs chain, solver and version")


def load_papers(db, source):
    db.executescript("""
        CREATE TABLE paper_cases (
            id TEXT PRIMARY KEY, doi TEXT COLLATE NOCASE, software TEXT NOT NULL,
            year INTEGER NOT NULL, summary TEXT NOT NULL, body TEXT NOT NULL);
        CREATE INDEX paper_doi ON paper_cases(doi);
        CREATE INDEX paper_software_year ON paper_cases(software, year DESC);
        CREATE TABLE paper_targets (
            target TEXT NOT NULL, case_id TEXT NOT NULL REFERENCES paper_cases(id),
            PRIMARY KEY (target, case_id));
        CREATE TABLE paper_reproduction_routes (
            chain TEXT NOT NULL, solver TEXT NOT NULL, version TEXT NOT NULL,
            case_id TEXT NOT NULL REFERENCES paper_cases(id),
            PRIMARY KEY (chain, solver, version, case_id));
    """)
    for path in sorted((Path(source) / "papers").glob("*.json")):
        records = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(records, list):
            raise ValueError(f"Expected case array in {path.name}")
        for offset, row in enumerate(records):
            try:
                if not isinstance(row, dict):
                    raise ValueError("Expected a case object")
                _validate(row)
            except ValueError as exc:
                raise ValueError(f"{path.name}[{offset}]: {exc}") from exc
            summary = {key: row[key] for key in SUMMARY}
            db.execute("INSERT INTO paper_cases VALUES (?, ?, ?, ?, ?, ?)", (
                row["id"], _doi_key(row["doi"]) if row["doi"] is not None else None, _software_key(row["software"]), row["year"],
                json.dumps(summary, ensure_ascii=False), json.dumps(row, ensure_ascii=False)))
            db.executemany("INSERT INTO paper_targets VALUES (?, ?)",
                           ((target, row["id"]) for target in row["targets"]))
            db.executemany("INSERT INTO paper_reproduction_routes VALUES (?, ?, ?, ?)",
                           ((route["chain"], route["solver"], route["version"], row["id"])
                            for route in row.get("reproduction", {}).get("routes", ())))


def query(db, *, software=None, target=None, identifier=None, doi=None, industry=None, term=None):
    filters = []
    values = []
    if software is not None:
        filters.append("c.software = ?")
        values.append(_software_key(software))
    if target is not None:
        filters.append("c.id IN (SELECT case_id FROM paper_targets WHERE target = ?)")
        values.append(target.strip().casefold())
    if industry is not None:
        industry_path = "json_extract(c.summary, '$.industry')"
        filters.append(f"({industry_path} = ? OR instr({industry_path}, ?) = 1)")
        values.extend((industry, industry + " / "))
    if term is not None:
        fields = ("c.id", "json_extract(c.summary, '$.title')", "json_extract(c.body, '$.authors')",
                  "json_extract(c.body, '$.keywords')")
        filters.append("(" + " OR ".join(f"instr(lower({field}), lower(?)) > 0" for field in fields) + ")")
        values.extend([term.strip()] * len(fields))
    if identifier is not None:
        filters.append("c.id = ?")
        values.append(identifier)
    if doi is not None:
        filters.append("c.doi = ?")
        values.append(_doi_key(doi))
    body = "body" if identifier is not None or doi is not None else "summary"
    sql = f"SELECT c.{body} FROM paper_cases AS c"
    if filters:
        sql += " WHERE " + " AND ".join(filters)
    sql += " ORDER BY c.year DESC, c.id"
    rows = [json.loads(record[0]) for record in db.execute(sql, values)]
    if identifier is not None:
        if not rows:
            raise ValueError(f"No matching paper case: {identifier}")
        return rows[0]
    return rows


def route_reproductions(db, plans):
    """Attach research evidence only to a matched chain and indexed solver version."""
    plan_goals = {(p["chain"], p["solver"], p["version"]): p.get("scoped_goals")
                  for p in plans}
    explicit = {(p["chain"], p["solver"], p["version"]): p.get("explicit_paper_doi") for p in plans}
    explicit_cases = {(p["chain"], p["solver"], p["version"]): p.get("explicit_paper_cases", ())
                      for p in plans}
    keys = plan_goals.keys()
    if not keys:
        return []
    values = ", ".join("(?, ?, ?)" for _ in keys)
    try:
        records = db.execute(f"""
            WITH requested(chain, solver, version) AS (VALUES {values})
            SELECT DISTINCT p.body FROM requested AS q
            JOIN paper_reproduction_routes AS r USING (chain, solver, version)
            JOIN paper_cases AS p ON p.id = r.case_id
            ORDER BY p.doi, p.id
        """, [value for key in sorted(keys) for value in key]).fetchall()
    except sqlite3.OperationalError as exc:
        if str(exc) != "no such table: paper_reproduction_routes":
            raise
        return []  # Older exported indexes predate native reproduction records.
    matches = []
    for (body,) in records:
        paper = json.loads(body)
        record = paper["reproduction"]
        routes = []
        for route in record["routes"]:
            key = (route["chain"], route["solver"], route["version"])
            if key not in keys:
                continue
            if route.get("requires_explicit_doi") and explicit[key] != _doi_key(paper["doi"]):
                continue
            if route.get("requires_case_reference") and paper["id"] not in explicit_cases[key]:
                continue
            routes.append(route)
        if routes:
            forward_results = any("gap" not in route for route in routes)
            goal_sets = [plan_goals[(r["chain"], r["solver"], r["version"])]
                         for r in routes if "gap" not in r]
            results = record["brief_results"] if forward_results else []
            if goal_sets and all(goal_sets):
                goals = {goal for values in goal_sets for goal in values}
                results = [{**result, "measurements": [m for m in result["measurements"] if m["quantity"] in goals]}
                           for result in results]
            matches.append({"doi": paper["doi"], "case_id": paper["id"],
                            "status": record["status"], "scope": record["scope"],
                            "results": results,
                            "gaps": record["gaps"] if forward_results else [route["gap"] for route in routes]})
    return matches


def explicit_reproduction_records(db, dois, routed):
    """Answer an indexed DOI lookup even when its engineering route is undetermined."""
    matched = {case["case_id"]: case for case in routed}
    matches = []
    try:
        for doi in sorted(dois):
            for paper in query(db, doi=doi):
                record = paper.get("reproduction")
                if record is None:
                    continue
                candidates = []
                for route in record["routes"]:
                    if route.get("gap"):
                        continue
                    for plan in device_chains.inventory(db, route["solver"], route["version"], route["chain"]):
                        candidates.append({
                            "status": "publication_scoped", "chain": plan["chain"],
                            "solver": plan["solver"], "version": plan["version"], "scope": plan["scope"],
                            "details_command": (
                                f"navifield capabilities chain-inventory --chain {plan['chain']} "
                                f"--solver {plan['solver']} --version {plan['version']}"
                            ),
                        })
                context = {
                    "publication_candidates": candidates,
                    "missing_paper_inputs": paper["required_inputs"],
                }
                if paper["id"] in matched:
                    matched[paper["id"]].update(context)
                    continue
                matches.append({
                    "doi": paper["doi"], "case_id": paper["id"], "title": paper["title"],
                    "status": record["status"], "scope": record["scope"],
                    "reference_scope": "publication_record_only", "source_url": paper["source_url"],
                    "publication_software": paper["software"],
                    "publication_software_version": paper["software_version"],
                    "results": record["brief_results"], "gaps": record["gaps"],
                    **context,
                })
    except sqlite3.OperationalError as exc:
        if str(exc) != "no such table: paper_cases":
            raise
    return matches
