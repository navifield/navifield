"""Project-local, evidence-bound numeric index for confirmed strip control runs."""

from __future__ import annotations
import hashlib
import json
import math
import re
import sqlite3
from pathlib import Path
from . import project, strip_control as strip


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def _json(p):
    return json.loads(p.read_text(encoding="utf-8"))


def _close(a, b):
    return (
        type(a) in (int, float)
        and math.isfinite(a)
        and math.isclose(a, b, rel_tol=1e-7, abs_tol=1e-12)
    )


def index_run(root, run):
    """Index only the exact strip worker and its freshly produced, independently read fields."""
    root = Path(root).resolve()
    rid = run["run_id"]
    spec = run.get("execution_request") or {}
    argv = spec.get("argv") or []
    if not (
        len(argv) >= 2
        and argv[0] == "-m"
        and argv[1] in ("navifield.strip_control", "backend.agent_engine.strip_control")
    ):
        return None
    state = run.get("execution_state") or {}
    resources = run.get("resources") or {}
    if (
        run.get("status") != "completed"
        or state.get("state") != "completed"
        or state.get("exit_code") != 0
        or spec.get("location", "local") != "local"
        or not state.get("started_at")
        or not state.get("finished_at")
        or not isinstance(resources.get("cores"), int)
        or resources["cores"] < 1
        or type(resources.get("wall_seconds")) not in (int, float)
        or not math.isfinite(resources["wall_seconds"])
        or resources["wall_seconds"] <= 0
        or type(resources.get("memory_bytes")) is not int
        or resources["memory_bytes"] <= 0
    ):
        raise ValueError("strip run is not a successful bounded local job")
    case = project.resolve_path(root, run["execution_case_path"])
    snap = run.get("input_snapshot") or {}
    copied = snap.get("copied") or []
    snaproot = project.resolve_path(root, snap["path"])
    required = ("strip.json", "electric.pro", "electric.msh")
    if any(n not in copied for n in required):
        raise ValueError("missing prepared input snapshot")
    for name in required:
        if (case / name).is_symlink() or (snaproot / name).read_bytes() != (
            case / name
        ).read_bytes():
            raise ValueError("prepared input changed since snapshot")
    receipt = _json(snaproot / "strip.json")
    summary = receipt["summary"]
    confirm = receipt["confirmation_sha256"]
    if (
        confirm != _sha(strip._as_json(summary).encode())
        or summary.get("model") != strip.MODEL
        or summary.get("scope") != strip.SCOPE
    ):
        raise ValueError("confirmation mismatch")
    if (snaproot / "electric.pro").read_text(encoding="ascii") != strip._formulation(
        summary
    ) or (snaproot / "electric.msh").read_text(encoding="ascii") != strip._mesh(
        summary
    ):
        raise ValueError("compiled input mismatch")
    log = project.resolve_path(root, state["log_path"]).read_text(
        encoding="utf-8", errors="replace"
    )
    claims = []
    for line in log.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(obj, dict)
            and obj.get("status") == "native_readback"
            and isinstance(obj.get("result"), str)
        ):
            claims.append(obj["result"])
    if len(claims) != 1:
        raise ValueError("missing unique worker result claim")
    match = re.search(
        r"(?:^|[\\/])(strip-native-[A-Za-z0-9_-]+)[\\/]result\.json$", claims[0]
    )
    if not match:
        raise ValueError("invalid worker result path")
    native_name = match.group(1)
    before = [
        x if isinstance(x, str) else x.get("path", "")
        for x in copied + (snap.get("skipped") or [])
        if isinstance(x, (str, dict))
    ]
    if any(x.startswith(native_name + "/") for x in before):
        raise ValueError("output existed before run")
    folder = case / native_name
    if folder.is_symlink() or not folder.resolve().is_relative_to(case):
        raise ValueError("invalid result folder")

    def safe(path):
        p = folder / path
        if (
            p.is_symlink()
            or not p.resolve().is_relative_to(folder.resolve())
            or not p.is_file()
        ):
            raise ValueError("missing or linked native evidence")
        return p

    result_path = safe("result.json")
    raw = result_path.read_bytes()
    result = json.loads(raw)
    if (
        result.get("status") != "native_readback"
        or result.get("confirmation_sha256") != confirm
        or result.get("scope") != strip.SCOPE
    ):
        raise ValueError("result not bound to confirmed input")
    for stage, name in [
        ("electric", "getdp.log"),
        ("thermal", "getdp.log"),
        ("structural", "ccx.log"),
    ]:
        text = safe(stage + "/" + name).read_text(encoding="utf-8", errors="replace")
        if (
            "Stopped (" not in text
            if stage != "structural"
            else "Job finished" not in text
        ):
            raise ValueError("native stage did not finish")
    electric = folder / "electric"
    thermal = folder / "thermal"
    structural = folder / "structural"
    for f in (
        "electric/gradient.txt",
        "electric/values.txt",
        "thermal/gradient.txt",
        "thermal/values.txt",
        "structural/axial.dat",
        "thermal/thermal.pro",
        "thermal/thermal.msh",
        "structural/axial.inp",
    ):
        safe(f)
    gv = strip._read_field(electric / "gradient.txt", summary, vector=True)
    volts = strip._read_field(electric / "values.txt", summary)
    temps = strip._read_field(thermal / "values.txt", summary)
    gt = strip._read_field(thermal / "gradient.txt", summary, vector=True)
    strip._check_ends(volts, summary, "voltage", "v")
    strip._check_ends(temps, summary, "temperature", "k")
    q = [v * v / summary["resistivity_ohm_m"] for v in gv]
    if (
        (thermal / "thermal.pro").read_text(encoding="ascii")
        != strip._formulation(summary, sources=q)
        or (thermal / "thermal.msh").read_text(encoding="ascii")
        != strip._mesh(summary, per_element=True)
        or (structural / "axial.inp").read_text(encoding="ascii")
        != strip._mechanical(summary, temps)
    ):
        raise ValueError("field transfer mismatch")
    x, _ = strip._connectivity(summary)
    widths = [b - a for a, b in zip(x, x[1:])]
    for i, dx in enumerate(widths):
        if not _close(gv[i], (volts[i + 1] - volts[i]) / dx):
            raise ValueError("electric gradient disagrees with voltage field")
        if not _close(gt[i], (temps[i + 1] - temps[i]) / dx):
            raise ValueError("thermal gradient disagrees with temperature field")
    area = summary["width_m"] * summary["thickness_m"]
    rho = summary["resistivity_ohm_m"]
    power = math.fsum(v * area * dx for v, dx in zip(q, widths))
    terminal = area / rho * (volts[-1] * gv[-1] - volts[0] * gv[0])
    ka = summary["conductivity_w_m_k"] * area
    heat = ka * (gt[0] - gt[-1]) + area * (q[0] * widths[0] + q[-1] * widths[-1]) / 2
    disp = strip._read_displacement(structural / "axial.dat", len(x))
    integral = summary["expansion_per_k"] * math.fsum(
        ((a + b) / 2 - summary["reference_k"]) * dx
        for a, b, dx in zip(temps, temps[1:], widths)
    )
    peak = max(range(len(temps)), key=temps.__getitem__)
    el = result["electrical"]
    th = result["thermal"]
    st = result["structural"]
    for a, b in [
        (el["integrated_joule_power_W"], power),
        (el["terminal_power_W"], terminal),
        (th["maximum_K"], temps[peak]),
        (th["maximum_x_m"], x[peak]),
        (st["free_end_axial_displacement_m"], disp),
        (st["free_end_x_m"], x[-1]),
        (st["integral_of_mapped_thermal_strain_m"], integral),
        (th["outward_heat_minus_joule_W"], heat - power),
    ]:
        if not _close(a, b):
            raise ValueError("native summary disagrees with raw field")
    tools = result["tools"]
    for tool, version in [("getdp", "3.5.0"), ("calculix", "2.22")]:
        if tools[tool].get("version") != version or not re.fullmatch(
            r"[0-9a-f]{64}", tools[tool].get("sha256", "")
        ):
            raise ValueError("invalid solver identity")
    if len(argv) != 6 or argv[2] != "--getdp" or argv[4] != "--calculix":
        raise ValueError("strip solver arguments are not uniquely bound")
    for tool, path in (("getdp", argv[3]), ("calculix", argv[5])):
        binary = Path(path)
        if not binary.is_file() or _sha(binary.read_bytes()) != tools[tool]["sha256"]:
            raise ValueError(f"{tool} binary hash differs from the run")
    rel = result_path.relative_to(root).as_posix()
    archive = project.resolve_path(root, f"result/{rid}/strip-result.json")
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.exists() and archive.read_bytes() != raw:
        raise ValueError("run already indexed with different result")
    if not archive.exists():
        archive.write_bytes(raw)
    quantities = [
        ("device_power", power, "W", None),
        ("maximum_strip_temperature", temps[peak], "K", x[peak]),
        ("free_end_axial_displacement", disp, "m", x[-1]),
    ]
    data = {
        "run_id": rid,
        "confirmation_sha256": confirm,
        "input_snapshot": snap["path"],
        "input_summary": summary,
        "result_sha256": _sha(raw),
        "result_path": archive.relative_to(root).as_posix(),
        "source_path": rel,
        "tools": tools,
        "diagnostics": {
            "terminal_minus_joule_W": terminal - power,
            "outward_heat_minus_joule_W": heat - power,
            "displacement_minus_integral_m": disp - integral,
            "status": "reported_not_engineering_acceptance",
        },
        "scope": strip.SCOPE,
        "quantities": [
            {"name": n, "value": v, "unit": u, "x_m": xx} for n, v, u, xx in quantities
        ],
    }
    db = project.resolve_path(root, ".navifield/results.sqlite3")
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS strip_runs (run_id TEXT PRIMARY KEY, result_sha256 TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS strip_quantities (run_id TEXT NOT NULL, name TEXT NOT NULL, value REAL NOT NULL, unit TEXT NOT NULL, x_m REAL, PRIMARY KEY(run_id,name), FOREIGN KEY(run_id) REFERENCES strip_runs(run_id))"
        )
        existing = c.execute(
            "SELECT result_sha256,payload FROM strip_runs WHERE run_id=?", (rid,)
        ).fetchone()
        payload = json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False)
        if existing and existing != (data["result_sha256"], payload):
            raise ValueError("conflicting existing index")
        c.execute(
            "INSERT OR IGNORE INTO strip_runs VALUES (?,?,?)",
            (rid, data["result_sha256"], payload),
        )
        c.executemany(
            "INSERT OR IGNORE INTO strip_quantities VALUES (?,?,?,?,?)",
            [(rid, *row) for row in quantities],
        )
    return data


def query(root, run_id=None, quantity=None):
    db = project.resolve_path(Path(root).resolve(), ".navifield/results.sqlite3")
    if not db.is_file():
        return []
    with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as c:
        sql = "SELECT r.payload FROM strip_runs r"
        params = []
        where = []
        if quantity:
            sql += " JOIN strip_quantities q ON q.run_id=r.run_id"
            where.append("q.name=?")
            params.append(quantity)
        if run_id:
            where.append("r.run_id=?")
            params.append(run_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY r.run_id"
        rows = [json.loads(r[0]) for r in c.execute(sql, params)]
    if quantity:
        for row in rows:
            row["quantities"] = [x for x in row["quantities"] if x["name"] == quantity]
    return rows
