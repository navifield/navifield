"""Headless projections of retained solver results into PNG and portable HTML.

Render views are section/surface. Registered parameters are region, axis and
position_mm; camera currently accepts only the isometric preset. All values come
from the selected run's published cell fields, never a caller-provided dataset.
"""
from __future__ import annotations

import base64
import csv
import hashlib
from html import escape
import json
import logging
import math
import os
from pathlib import Path, PurePosixPath
from itertools import islice
import unicodedata
import tempfile

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import project
from .result_boundary import BoundaryError, envelope, public_result, relative


class _ReportValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReportParagraph(_ReportValue):
    text: str = Field(min_length=1, max_length=8192)


class ReportTable(_ReportValue):
    columns: tuple[str, ...] = Field(min_length=1, max_length=16)
    rows: tuple[tuple[str, ...], ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def validate_table(self):
        if (any(len(row) != len(self.columns) for row in self.rows)
                or any(not c or len(c) > 1024 for c in self.columns)
                or any(len(cell) > 2048 for row in self.rows for cell in row)):
            raise ValueError("Invalid report table")
        return self


class ReportFigure(_ReportValue):
    relative_path: str = Field(min_length=1, max_length=512)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    caption: str = Field(min_length=1, max_length=1024)
    alt_text: str = Field(min_length=1, max_length=512)

    @field_validator("relative_path")
    @classmethod
    def validate_path(cls, path):
        parts = path.split("/")
        if (path != unicodedata.normalize("NFC", path) or "\\" in path or "\x00" in path
                or PurePosixPath(path).is_absolute() or (len(path) >= 2 and path[1] == ":")
                or any(not part or part in (".", "..") for part in parts)
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in path)
                or not path.casefold().endswith((".png", ".jpg", ".jpeg", ".webp"))):
            raise ValueError("Invalid report figure path")
        return path


class ReportSection(_ReportValue):
    key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
    title: str = Field(min_length=1, max_length=256)
    blocks: tuple[ReportParagraph | ReportTable | ReportFigure, ...] = Field(default=(), max_length=128)


class EngineeringReport(_ReportValue):
    summary: str = Field(min_length=1, max_length=4096)
    sections: tuple[ReportSection, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def validate_sections(self):
        if len({section.key for section in self.sections}) != len(self.sections):
            raise ValueError("Duplicate report section")
        return self


def _history_sections(*, key, title, columns, rows):
    iterator = iter(rows)
    sections = []
    while True:
        tables = []
        for _ in range(128):
            batch = tuple(islice(iterator, 128))
            if not batch:
                break
            tables.append(ReportTable(columns=columns, rows=batch))
        if not tables:
            return tuple(sections)
        sections.append(ReportSection(key=f"{key}.{len(sections)}", title=title, blocks=tuple(tables)))


class ResultError(ValueError):
    def __init__(self, message, code="POSTPROCESS_FAILED"):
        self.code, self.message = code, message


def _failure(error, stage):
    result = envelope(status="failed", code=error.code, message=error.message)
    result["failure"]["stage"] = stage
    return result


def _digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024**2), b""):
            value.update(block)
    return value.hexdigest()


def _load(root, request, *, allow_missing_phi=False):
    run_id = request.get("run_id")
    if not isinstance(run_id, str):
        raise ResultError("Select an existing result run.", "INPUT_INVALID")
    relative(root, f".navifield/runs/{run_id}/manifest.json")
    run = project.read_run(root, run_id)
    path = relative(root, f"result/{run_id}/fields.json")
    if not path.is_file():
        raise ResultError("The selected run has no retained field results.", "MEASUREMENT_UNAVAILABLE")
    fields = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(fields, list) or not fields:
        raise ResultError("The field catalog is empty.", "MEASUREMENT_UNAVAILABLE")
    published = {item["path"] for item in run.get("artifacts", [])}
    for item in fields:
        if not isinstance(item, dict) or not {"name", "unit", "association", "region", "artifact_path"} <= item.keys():
            raise ResultError("The field catalog is incomplete.")
        target = relative(root, item["artifact_path"])
        if not item["artifact_path"].startswith(f"result/{run_id}/"):
            raise ResultError("A catalogued result is outside the selected run.")
        if allow_missing_phi and item["name"] == "phi" and item["association"] == "face":
            continue
        if item["artifact_path"] not in published or not target.is_file():
            raise ResultError("A catalogued result is unavailable.", "MEASUREMENT_UNAVAILABLE")
    return run, fields


def _image_artifact(relative_path, run_id, label):
    return {"path": relative_path, "kind": "image", "media_type": "image/png", "run_id": run_id, "label": label}


def _png(path):
    raw = path.read_bytes()
    if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ResultError("The rendered image could not be read back.")
    return raw


def _render_fields(root, run, fields, request):
    # JobManager intentionally drops user-profile variables. Keep font/config
    # caches in its already-owned temporary directory, not a user home folder.
    cache = Path(tempfile.gettempdir()) / "plot-config"
    cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache))
    os.environ["MPLBACKEND"] = "Agg"
    import numpy as np
    import pyvista as pv
    from .core_render import _render_one
    from .core_section import axis_section

    view = request.get("view", "section")
    names = request.get("fields")
    parameters = request.get("parameters") or {}
    camera = request.get("camera")
    if (view not in ("section", "surface") or not isinstance(names, list) or not names
            or len(names) != len(set(names)) or any(name not in ("U", "p", "p_Pa") for name in names)
            or not isinstance(parameters, dict) or set(parameters) - {"region", "axis", "position_mm"}
            or camera not in (None, {}, {"preset": "isometric"})):
        raise ResultError("Unsupported rendering settings.", "INPUT_INVALID")
    regions = list(dict.fromkeys(item["region"] for item in fields if item["association"] == "cell"))
    selected = parameters.get("region")
    if selected is not None:
        if selected not in regions:
            raise ResultError("The selected region is unavailable.", "INPUT_INVALID")
        regions = [selected]
    if not regions:
        raise ResultError("No cell field regions are available.", "MEASUREMENT_UNAVAILABLE")
    artifacts, measurements = [], []
    run_id = run["run_id"]
    for index, region in enumerate(regions):
        entries = []
        for name in names:
            matches = [item for item in fields if item["region"] == region and item["name"] == name and item["association"] == "cell"]
            if len(matches) != 1:
                raise ResultError("A requested regional field is unavailable.", "MEASUREMENT_UNAVAILABLE")
            entry = matches[0]
            expected = {"U": "m/s", "p": "m^2/s^2", "p_Pa": "Pa"}[name]
            if entry["unit"] != expected:
                raise ResultError("The requested field unit is not supported.", "MEASUREMENT_UNAVAILABLE")
            entries.append(entry)
        paths = {entry["artifact_path"] for entry in entries}
        if len(paths) != 1:
            raise ResultError("The regional fields do not share one retained result grid.")
        source = relative(root, entries[0]["artifact_path"])
        if source.suffix.lower() != ".vtu":
            raise ResultError("The regional display grid is unavailable.")
        source_hash = _digest(source)
        mesh = pv.read(source)
        if not isinstance(mesh, pv.UnstructuredGrid) or mesh.n_cells == 0 or not np.isfinite(mesh.points).all():
            raise ResultError("The regional display grid is invalid.")
        raw_unit = mesh.field_data.get("coordinate_length_unit")
        unit = str(raw_unit[0]) if raw_unit is not None and len(raw_unit) == 1 else None
        mm_per_unit = {"m": 1000., "mm": 1., "cm": 10., "in": 25.4}.get(unit)
        if mm_per_unit is None:
            raise ResultError("The result coordinate unit is unavailable.", "MEASUREMENT_UNAVAILABLE")
        spans = [mesh.bounds[2*i + 1] - mesh.bounds[2*i] for i in range(3)]
        axis = parameters.get("axis", "xyz"[int(np.argmin(spans))])
        if axis not in ("x", "y", "z"):
            raise ResultError("Select a supported section axis.", "INPUT_INVALID")
        component = "xyz".index(axis)
        middle = (mesh.bounds[2*component] + mesh.bounds[2*component + 1]) / 2
        position_mm = parameters.get("position_mm", middle * mm_per_unit)
        if type(position_mm) not in (int, float) or not math.isfinite(position_mm):
            raise ResultError("Section position must be finite.", "INPUT_INVALID")
        position = position_mm / mm_per_unit
        if view == "section" and not mesh.bounds[2*component] < position < mesh.bounds[2*component + 1]:
            raise ResultError("The section must intersect the interior of the selected region.", "INPUT_INVALID")
        display = axis_section(mesh, axis, position) if view == "section" else mesh.extract_surface()
        if not display.n_cells:
            raise ResultError("The requested section is empty.", "MEASUREMENT_UNAVAILABLE")
        request_hash = hashlib.sha256(json.dumps({"source": source_hash, "view": view, "axis": axis,
            "position_mm": position_mm, "fields": names}, sort_keys=True).encode()).hexdigest()[:12]
        for entry in entries:
            name = entry["name"]
            if name not in display.cell_data:
                raise ResultError("The requested cell values were not preserved by the section.")
            values = np.asarray(display.cell_data[name], dtype=float)
            if name == "U":
                if values.shape != (display.n_cells, 3):
                    raise ResultError("Velocity is not a three-component cell vector.")
                values = np.linalg.norm(values, axis=1)
            elif values.shape != (display.n_cells,):
                raise ResultError("Pressure is not a scalar cell field.")
            if not np.isfinite(values).all():
                raise ResultError("The selected field contains unavailable values.", "MEASUREMENT_UNAVAILABLE")
            display.cell_data["display_scalar"] = values
            interval = (float(values.min()), float(values.max()))
            label = {"U": "Speed", "p": "Kinematic pressure", "p_Pa": "Numerical gauge pressure"}[name]
            label += f" ({entry['unit']})"
            relative_path = f"report/{run_id}/v-{request_hash}-{index}-{name}.png"
            target = relative(root, relative_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            annotation = f"{region}: {label}\n{axis}={position_mm:.6g} mm; native cell {view}\nRange: {interval[0]:.6g} to {interval[1]:.6g}"
            _render_one(display, "display_scalar", axis.upper(), str(target), bar_title=label,
                        association="cell", isometric=True, scalar_range=interval, annotation=annotation)
            _png(target)
            artifacts.append(_image_artifact(relative_path, run_id, f"{region} · {label}"))
            measurements.append({"region": region, "field": name, "unit": entry["unit"], "minimum": interval[0],
                "maximum": interval[1], "source_artifact_path": entry["artifact_path"], "source_sha256": source_hash,
                "view": view, "axis": axis, "position_mm": position_mm, "interpolation": "none"})
        if _digest(source) != source_hash:
            raise ResultError("The selected result changed while rendering.", "STATE_CONFLICT")
    return artifacts, measurements


def _publish(root, run, artifacts, findings=()):
    current = project.read_run(root, run["run_id"])
    by_path = {item["path"]: item for item in current.get("artifacts", [])}
    by_path.update({item["path"]: item for item in artifacts})
    current["artifacts"] = list(by_path.values())
    for finding in findings:
        if finding not in current["findings"]:
            current["findings"].append(finding)
    project.save_run(root, current)
    return current


def render_view(root, request):
    """Render retained cell fields using the shared headless contour renderer."""
    try:
        run, fields = _load(root, request)
        artifacts, measurements = _render_fields(root, run, fields, request)
        run = _publish(root, run, artifacts)
        result = envelope({"run_id": run["run_id"], "views": measurements})
        result.update(artifacts=artifacts, findings=run["findings"])
        return result
    except ResultError as error:
        return _failure(error, "postprocess")
    except BoundaryError as error:
        return _failure(ResultError("Result access was denied.", error.code), "postprocess")
    except ImportError as error:
        return _failure(ResultError("Plotting dependencies are unavailable; install navifield[plots]. " + str(error), "ENGINE_UNAVAILABLE"), "postprocess")
    except (OSError, ValueError, KeyError, TypeError):
        logging.exception("Retained result rendering failed")
        return _failure(ResultError("The retained result could not be rendered."), "postprocess")


def _csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream)
        columns = next(reader, [])
        rows = [tuple(float(value) for value in row) for row in reader]
    if (len(columns) < 2 or not rows or any(len(row) != len(columns) or not all(math.isfinite(v) for v in row) for row in rows)
            or any(rows[i][0] <= rows[i - 1][0] for i in range(1, len(rows)))):
        raise ResultError("An existing numeric history is invalid.")
    return tuple(columns), rows


def _history_image(path, columns, rows, destination, *, residual):
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    figure = Figure(figsize=(10, 5), dpi=128)
    FigureCanvasAgg(figure)
    axes = figure.subplots()
    for index, name in enumerate(columns[1:], start=1):
        axes.plot([row[0] for row in rows], [row[index] for row in rows], label=name)
    if residual and all(row[i] > 0 for row in rows for i in range(1, len(columns))):
        axes.set_yscale("log")
    axes.set_xlabel(columns[0])
    axes.set_ylabel("Initial equation residual" if residual else "Static pressure drop (Pa)")
    axes.grid(True, alpha=.3)
    axes.legend()
    axes.set_title(path.stem)
    figure.tight_layout()
    figure.savefig(destination)
    _png(destination)


def _customer(value):
    # Finding codes and private provenance are not report prose.
    words = {
        "FAIL": "未达标（未满足该项判据）", "PASS": "仅该项判据已满足，不代表工程验收通过",
        "MET": "已满足记录中的数值判据", "NOT_MET": "未达标（未满足记录中的数值判据）",
        "NOT_AVAILABLE": "未取得完整测量", "NOT_EVALUATED": "未作工程验收判断",
        "NOT_RUN": "未执行", "UNKNOWN": "尚无法确定",
        "INSUFFICIENT_PRESSURE_WINDOW": "压力历史长度不足以评价指定窗口",
        "LOG_PRESSURE_ITERATION_MISMATCH": "压力历史与求解日志的末次迭代不一致",
        "NONCONSECUTIVE_PRESSURE_WINDOW": "压力评价窗口存在不连续迭代",
        "NEAR_ZERO_PRESSURE_MEAN": "平均压差接近零，无法定义相对波动",
        "PRESSURE_SPAN_NONFINITE": "压力波动计算未取得有限数值",
        "MISSING_RESIDUAL_COMPONENT": "缺少评价所需的残差分量",
        "LOG_READBACK_UNAVAILABLE": "未取得可用的求解日志读回",
        "LOG_NOT_DECLARED": "未记录可用的求解日志", "LOG_READ_LIMIT": "求解日志未完整读取",
        "LOG_NONINTEGER_ITERATION": "日志迭代标签不符合稳态历史格式",
        "ARITHMETIC_ONLY_NOT_ACCEPTANCE": "仅为数值读回，不代表工程验收",
        "residual_history": "残差历史", "residuals": "残差判据", "pressure_span": "压差波动判据",
        "boundary_volume_flux": "边界体积流量算术", "continuity_history": "连续性历史",
        "MEASURED": "已取得算术测量，未作守恒验收", "NOT_CLAIMED": "未作守恒或工程验收",
        "completed": "记录为完成", "failed": "运行失败", "running": "运行中",
        "cancelled": "已取消", "needs_decision": "等待选择",
    }
    labels = {"status": "状态", "reason": "说明", "criterion": "判据", "value": "数值", "unit": "单位",
              "missing_iterations": "缺失迭代", "invalid_lines": "异常日志行数", "region": "区域",
              "coordinates_mm": "位置（mm）", "uid": "标识"}

    def translate(item, key=None):
        if item is None:
            return "未取得"
        if isinstance(item, dict):
            return {labels.get(name, name): translate(child, name) for name, child in item.items()}
        if isinstance(item, list):
            return [translate(child, key) for child in item]
        if isinstance(item, str):
            if item in words:
                return words[item]
            if key in ("status", "reason", "criterion") and item.replace("_", "").isalnum() and item.isupper():
                return "未取得可解释的诊断说明；本项不作通过判断"
        return item

    clean = translate(public_result(value))
    return json.dumps(clean, ensure_ascii=False, sort_keys=True) if isinstance(clean, (dict, list)) else str(clean)


def _configured_descriptor(root, run):
    """Read this run's retained solver descriptor, never the source case as it."""
    inputs = (run.get("continuation") or {}).get("inputs", {})
    path = inputs.get("case_path")
    if path is None:
        return {}
    expected = f"simulation_setup/{run['run_id']}/case.json"
    mesh_path = f"model/{run['run_id']}/case"
    if path != expected or inputs.get("mesh_path") != mesh_path:
        raise ResultError("The result configuration belongs to a different run.", "STATE_CONFLICT")
    descriptor = json.loads(relative(root, path).read_text(encoding="utf-8"))
    if (descriptor.get("source_case_path") != run["case_path"]
            or descriptor.get("source_mesh_run_id") != run.get("resume_from_run_id")
            or descriptor.get("mesh_path") != mesh_path):
        raise ResultError("The result configuration is not bound to this run.", "STATE_CONFLICT")
    return descriptor


def _boundary_flux(root, run, fields, descriptor):
    """Final retained phi only; no residual-based inference or new acceptance limit."""
    unavailable = {"status": "NOT_AVAILABLE", "unit": "m^3/s", "inlet_m3_s": None,
        "outlet_m3_s": None, "other_m3_s": None, "net_m3_s": None, "relative_imbalance": None,
        "reason": "未取得同一运行、同一主域和迭代的完整体积通量与边界读回。",
        "engineering_acceptance": "NOT_CLAIMED"}
    if run.get("status") not in ("completed", "failed", "cancelled"):
        return unavailable
    try:
        from .core_flux import _flux_arithmetic
        main = descriptor["main_region"]
        iteration = descriptor["physics"]["iterations"]
        phi_field, = (item for item in fields if item["name"] == "phi" and item["region"] == main
                      and item["association"] == "face" and item["unit"] == "m^3/s")
        published = {item["path"] for item in run.get("artifacts", [])}
        if phi_field["artifact_path"] not in published or not phi_field["artifact_path"].startswith(f"result/{run['run_id']}/"):
            return unavailable
        source = project.read_run(root, descriptor["source_mesh_run_id"])
        adoption = source["adoption"]
        if adoption["candidate_id"] != source["run_id"]:
            return unavailable
        adopted_path, = adoption["artifact_paths"]
        mesh_record = json.loads(relative(root, adopted_path + "/mesh.json").read_text(encoding="utf-8"))
        inlet, = (row["patch"] for row in mesh_record["params"]["ports"] if row["role"] == "inlet")
        outlet, = (row["patch"] for row in mesh_record["params"]["ports"] if row["role"] == "outlet")
        case_path = descriptor["mesh_path"] + "/" + descriptor["region_cases"][main]
        boundary_path = case_path + "/constant/polyMesh/boundary"
        phi = relative(root, phi_field["artifact_path"])
        boundary = relative(root, boundary_path)
        # Use the existing arithmetic reader's limits, dimensions and full patch coverage.
        if phi.stat().st_size > 64 * 1024**2 or boundary.stat().st_size > 16 * 1024**2:
            return unavailable
        measured = _flux_arithmetic(phi.read_bytes(), boundary.read_bytes(), iteration_index=iteration,
                                   inlet_patch=inlet, outlet_patch=outlet, region=None)
        return {"status": "MEASURED", **measured, "region": main,
                "phi_artifact_path": phi_field["artifact_path"], "boundary_path": boundary_path}
    except (BoundaryError, OSError, ValueError, KeyError, TypeError):
        logging.exception("Retained boundary flux arithmetic unavailable")
        return unavailable


def _flux_report(root, run, fields, descriptor):
    measured = _boundary_flux(root, run, fields, descriptor)
    continuity = {"status": "NOT_AVAILABLE", "reason": "本次公开结果未记录连续性历史；未验证其随迭代的守恒表现。"}
    run_id = run["run_id"]
    relative_path = f"report/{run_id}/flux.json"
    path = relative(root, relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run_id": run_id, "boundary_flux": measured, "continuity_history": continuity},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    rows = [("终态通量读回", _customer(measured["status"])),
            ("入口外向体积流量 / m³/s", _customer(measured["inlet_m3_s"])),
            ("出口外向体积流量 / m³/s", _customer(measured["outlet_m3_s"])),
            ("其余边界外向体积流量 / m³/s", _customer(measured["other_m3_s"])),
            ("全部边界净外向流量 / m³/s", _customer(measured["net_m3_s"])),
            ("相对不平衡 |净流量| / max(|入口|,|出口|)", _customer(measured["relative_imbalance"])),
            ("连续性历史", continuity["reason"])]
    if measured["status"] == "MEASURED":
        rows.extend((("采样迭代", str(measured["iteration_index"])),
                     ("入口 / 出口边界", measured["inlet_patch"] + " / " + measured["outlet_patch"])))
    section = ReportSection(key="flux-arithmetic", title="边界体积流量算术与守恒验证范围", blocks=(
        ReportTable(columns=("项目", "读回"), rows=tuple(rows)),
        ReportParagraph(text="通量按原生边界面的外法向带符号求和，正常入口为负、出口为正。这里只提供终态算术测量，未设合格阈值、未作守恒验收。残差或压差波动的单项判据满足，不足以证明整体收敛。")))
    values = {key: measured.get(key) for key in ("status", "unit", "inlet_m3_s", "outlet_m3_s", "other_m3_s", "net_m3_s", "relative_imbalance")}
    values["criterion"] = "boundary_volume_flux"
    findings = [{"code": "SOLVER_DIAGNOSTIC", "severity": "info" if measured["status"] == "MEASURED" else "warn",
        "measured": values, "limit": None, "message": "终态边界通量为算术诊断，未作守恒验收。",
        "location": {"uid": None, "coordinates_mm": None, "region": descriptor.get("main_region")}},
        {"code": "SOLVER_DIAGNOSTIC", "severity": "warn", "measured": {"criterion": "continuity_history", **continuity},
         "limit": None, "message": continuity["reason"], "location": None}]
    artifact = {"path": relative_path, "kind": "data", "media_type": "application/json", "run_id": run_id,
                "label": "终态边界通量算术与连续性历史缺测说明"}
    return section, findings, artifact


def _index(root):
    metadata = json.loads(relative(root, ".navifield/project.json").read_text(encoding="utf-8"))
    rows = []
    for path in metadata["runs"]:
        run = json.loads(relative(root, path).read_text(encoding="utf-8"))
        reports = [item for item in run.get("artifacts", []) if item["kind"] == "report" and item["path"].startswith(f"report/{run['run_id']}/")]
        for artifact in reports:
            link = os.path.relpath(relative(root, artifact["path"]), relative(root, "report")).replace("\\", "/")
            rows.append(f'<tr><td>{escape(run["run_id"])}</td><td>{escape(_customer(run.get("status")))}</td><td><a href="{escape(link, quote=True)}">{escape(artifact["label"])}</a></td></tr>')
    output = relative(root, "report/index.html")
    output.write_text('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>计算结果报告</title></head><body><h1>计算结果报告</h1><table><tr><th>运行</th><th>记录状态</th><th>报告</th></tr>' + "".join(rows) + '</table></body></html>', encoding="utf-8")
    return {"path": "report/index.html", "kind": "report", "media_type": "text/html", "run_id": None, "label": "计算结果报告索引"}


def _html(report, root, output, *, self_contained, title="CFD计算结果报告"):
    sections = []
    for section in report.sections:
        blocks = []
        for block in section.blocks:
            if isinstance(block, ReportParagraph):
                blocks.append("<p>" + escape(block.text) + "</p>")
            elif isinstance(block, ReportTable):
                blocks.append("<table><thead><tr>" + "".join("<th>" + escape(c) + "</th>" for c in block.columns)
                    + "</tr></thead><tbody>" + "".join("<tr>" + "".join("<td>" + escape(c) + "</td>" for c in row) + "</tr>" for row in block.rows) + "</tbody></table>")
            elif isinstance(block, ReportFigure):
                source = relative(root, block.relative_path)
                raw = _png(source)
                if hashlib.sha256(raw).hexdigest() != block.sha256:
                    raise ResultError("A report image changed during export.", "STATE_CONFLICT")
                url = "data:image/png;base64," + base64.b64encode(raw).decode("ascii") if self_contained else os.path.relpath(source, output.parent).replace("\\", "/")
                blocks.append(f'<figure><img src="{escape(url, quote=True)}" alt="{escape(block.alt_text, quote=True)}"><figcaption>{escape(block.caption)}</figcaption></figure>')
            else:
                raise ResultError("Unsupported report content.")
        sections.append("<section><h2>" + escape(section.title) + "</h2>" + "".join(blocks) + "</section>")
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        '<title>' + escape(title) + '</title><style>body{font-family:"Microsoft YaHei",sans-serif;max-width:1120px;margin:24px auto;padding:0 20px;color:#17324d;line-height:1.6}'
        'section,header{border:1px solid #d4dfe7;padding:20px;margin:16px 0;border-radius:8px}table{border-collapse:collapse;width:100%;font-size:14px}'
        'td,th{border:1px solid #d4dfe7;padding:7px;text-align:left;overflow-wrap:anywhere}th{background:#eef4f9}img{width:100%;height:auto}figure{margin:20px 0}figcaption{color:#506375}</style>'
        '</head><body><header><h1>' + escape(title) + '</h1><p>' + escape(report.summary) + '</p></header>' + "".join(sections) + '</body></html>')


def _script_report(root, run, request):
    """Render the script's registered products with the existing report renderer."""
    run_id = run['run_id']
    rows, blocks, table_sections = [], [], []
    products = run.get('tool_products', [])
    product_index = relative(root, '.navifield/tool-products/' + run_id + '.json')
    if product_index.is_file():
        products = json.loads(product_index.read_text(encoding='utf-8')).get('products', [])
    for item in products:
        if item.get('kind') == 'scalar':
            rows.append((str(item.get('name')), _customer(item.get('value')), _customer(item.get('unit')),
                         _customer(item.get('status'))))
        elif item.get('kind') == 'artifact':
            path = item.get('path')
            source = relative(root, path)
            if not source.is_file():
                continue
            label = '用户脚本：' + str(item.get('title') or path)
            if str(item.get('format', '')).lower() == 'png':
                blocks.append(ReportFigure(relative_path=path, sha256=_digest(source), caption=label, alt_text=label))
            elif str(item.get('format', '')).lower() == 'csv':
                with source.open('r', encoding='utf-8-sig', newline='') as stream:
                    values = list(csv.reader(stream))
                if values:
                    table_sections.extend(_history_sections(key='user-table-' + str(len(table_sections)), title=label,
                        columns=tuple(values[0]), rows=(tuple(row) for row in values[1:])))
            else:
                blocks.append(ReportParagraph(text=label + '：' + path))
    if not blocks and not rows and not table_sections:
        blocks.append(ReportParagraph(text='没有已登记的用户脚本产物。'))
    findings = list(run.get('findings', []))
    source_rows = []
    source_id = run.get('source_run_id')
    if source_id is not None:
        source_rows.append(('来源运行', str(source_id)))
        try:
            source = project.read_run(root, source_id)
            source_rows.append(('来源运行状态', _customer(source.get('status'))))
            for finding in source.get('findings', []):
                if finding not in findings: findings.append(finding)
            record_path = relative(root, '.navifield/records/' + source_id + '.json')
            if record_path.is_file():
                source_record = json.loads(record_path.read_text(encoding='utf-8'))
                quality = source_record.get('quality')
                source_rows.append(('来源质量记录', _customer(quality) if quality is not None else '未取得'))
                if isinstance(quality, dict):
                    for finding in quality.get('findings') or []:
                        if finding not in findings: findings.append(finding)
            else:
                source_rows.append(('来源质量记录', '未取得'))
        except (OSError, ValueError, KeyError, TypeError):
            source_rows.append(('来源运行或质量记录', '未取得'))
    else:
        source_rows.append(('来源运行', '未提供'))
    for item in products:
        for finding in item.get('findings', []):
            if finding not in findings: findings.append(finding)
    finding_rows = [(str(f.get('severity', '')), _customer(f.get('message')), _customer(f.get('measured')),
                     _customer(f.get('limit')), _customer(f.get('location'))) for f in findings if isinstance(f, dict)]
    sections = [ReportSection(key='source', title='来源运行与质量', blocks=(ReportTable(
        columns=('项目', '记录'), rows=tuple(source_rows)),))]
    if blocks:
        sections.append(ReportSection(key='user-script', title='用户脚本', blocks=tuple(blocks)))
    sections.extend(_history_sections(key='user-scalars', title='用户脚本数值',
        columns=('用户脚本数值', '值', '单位', '状态'), rows=rows))
    sections.extend(table_sections)
    sections.extend(_history_sections(key='findings', title='全部检查结果与未满足项',
        columns=('级别', '说明', '实测', '目标或限值', '位置'), rows=finding_rows))
    sections.append(ReportSection(key='limits', title='适用范围', blocks=(ReportParagraph(
        text='用户脚本产物按实际运行记录展示；失败运行的部分产物不代表执行成功或工程验收。'),)))
    report = EngineeringReport(summary=f"运行 {run_id} 状态：{_customer(run.get('status'))}；来源为用户脚本。", sections=tuple(sections))
    output_path = f"report/{run_id}/{'standalone' if request['self_contained'] else 'index'}.html"
    output = relative(root, output_path)
    output.write_text(_html(report, root, output, self_contained=request['self_contained'], title='用户脚本结果报告'), encoding='utf-8')
    artifact = {'path': output_path, 'kind': 'report', 'media_type': 'text/html', 'run_id': run_id, 'label': '用户脚本报告'}
    run = _publish(root, run, [artifact], findings)
    index = _index(root)
    _publish(root, run, [index])
    result = envelope({'run_id': run_id, 'report_path': output_path, 'self_contained': request['self_contained'],
                       'engineering_acceptance': 'NOT_EVALUATED'})
    result.update(artifacts=[artifact, index], findings=run['findings'])
    return result


def report_generate(root, request):
    """Export this run's fields, histories and every finding without GUI/model calls."""
    try:
        if type(request.get("self_contained")) is not bool:
            raise ResultError("Select self_contained true or false.", "INPUT_INVALID")
        selected = project.read_run(root, request.get("run_id"))
        if selected.get('producer', {}).get('kind') == 'user_script':
            return _script_report(root, selected, request)
        run, fields = _load(root, request, allow_missing_phi=True)
        run_id = run["run_id"]
        artifacts, views = _render_fields(root, run, fields, {"view": "section", "fields": ["U", "p_Pa"]})
        figures = []
        for artifact, view in zip(artifacts, views):
            caption = (f"区域 {view['region']}；{view['field']} / {view['unit']}；{view['axis']}={view['position_mm']:.6g} mm；"
                f"范围 {view['minimum']:.6g} 至 {view['maximum']:.6g}。原生单元截面值，无跨区域插值。"
                + ("压力为该区域数值参考下的相对压力；不同封闭区域的压力零点不可直接比较。" if view['field'] == 'p_Pa' else "速度模值。"))
            figures.append(ReportFigure(relative_path=artifact["path"], sha256=_digest(relative(root, artifact["path"])), caption=caption, alt_text=artifact["label"]))
        sections = [ReportSection(key="fields", title="本次结果截面", blocks=tuple(figures))]
        findings = list(run.get("findings", []))
        descriptor = _configured_descriptor(root, run)
        flux_section, flux_findings, flux_artifact = _flux_report(root, run, fields, descriptor)
        sections.append(flux_section)
        findings.extend(flux_findings)
        artifacts.append(flux_artifact)
        density = descriptor.get("physics", {}).get("density_kg_m3")
        physical_rows = [("压力显示", "已发布的 p_Pa / Pa；不再次换算"),
                         ("密度来源", _customer(density) + " kg/m³" if type(density) in (int, float) and math.isfinite(density) and density > 0 else "未取得；不根据压力图反推密度"),
                         ("主流动区域", _customer(descriptor.get("main_region")))]
        sections.insert(0, ReportSection(key="physical", title="结果物理量说明", blocks=(ReportTable(columns=("项目", "记录"), rows=tuple(physical_rows)),)))
        diagnostic_path = f"result/{run_id}/diagnostics.json"
        published = {item["path"] for item in run.get("artifacts", [])}
        if diagnostic_path in published:
            diagnostic = json.loads(relative(root, diagnostic_path).read_text(encoding="utf-8"))
            statuses = {"MET": "已满足记录中的数值判据", "NOT_MET": "未满足记录中的数值判据", "NOT_AVAILABLE": "未取得完整测量"}
            values = [(name, statuses.get(diagnostic.get(name), "未取得完整测量")) for name in ("residuals", "pressure_span")]
            for name in ("iteration", "residual_limit", "max_initial_residual", "pressure_window", "relative_span_limit", "mean_pressure_drop_pa", "pressure_span_pa"):
                if name in diagnostic:
                    values.append((name, _customer(diagnostic[name])))
            sections.append(ReportSection(key="diagnostics", title="数值诊断原始读回", blocks=(ReportTable(columns=("项目", "读回"), rows=tuple(values)),)))
        regions = list(dict.fromkeys(item["region"] for item in fields if item["name"] == "U" and item["association"] == "cell"))
        histories = [(f"result/{run_id}/{region}-residual.csv", True, region) for region in regions]
        histories.append((f"result/{run_id}/pressure.csv", False, None))
        for index, (path, residual, region) in enumerate(histories):
            source = relative(root, path)
            if path not in published or not source.is_file():
                findings.append({"code": "MEASUREMENT_UNAVAILABLE", "severity": "warn", "measured": None, "limit": None,
                    "message": "A regional residual history is unavailable." if residual else "Pressure-drop history is unavailable.",
                    "location": {"uid": None, "coordinates_mm": None, "region": region}})
                continue
            columns, rows = _csv(source)
            image_path = f"report/{run_id}/history-{index}-{_digest(source)[:12]}.png"
            target = relative(root, image_path)
            _history_image(source, columns, rows, target, residual=residual)
            label = (f"{region} · 残差历史" if residual else "面积平均静压差历史 / Pa")
            artifact = _image_artifact(image_path, run_id, label)
            artifacts.append(artifact)
            sections.append(ReportSection(key=f"history-chart-{index}", title=label, blocks=(ReportFigure(
                relative_path=image_path, sha256=_digest(target), caption="按原始记录逐点绘制，未平滑或补造缺失样本。", alt_text=label),)))
            sections.extend(_history_sections(key=f"history-{index}", title=label + "：完整记录", columns=columns,
                rows=((format(value, ".17g") for value in row) for row in rows)))
        finding_rows = []
        for finding in findings:
            finding_rows.append(({"info": "信息", "warn": "警告 / 未满足或需关注", "block": "阻断 / 不可用"}.get(finding.get("severity"), "未分类"),
                _customer(finding.get("message")), _customer(finding.get("measured")), _customer(finding.get("limit")), _customer(finding.get("location"))))
        sections.extend(_history_sections(key="findings", title="全部检查结果与未满足项", columns=("级别", "说明", "实测", "目标或限值", "位置"), rows=finding_rows))
        if not finding_rows:
            sections.append(ReportSection(key="findings-empty", title="检查记录", blocks=(ReportParagraph(text="本次运行未记录检查提示；这不代表已完成工程验证或客户验收。"),)))
        sections.append(ReportSection(key="limits", title="适用范围", blocks=(ReportParagraph(text="报告仅呈现所选运行保留的场结果和检查记录；数值收敛、网格与工程验证及客户验收分别判断。压力图使用已发布的 Pa 字段，不重复乘密度。"),)))
        report = EngineeringReport(summary=f"运行 {run_id} 的场结果、原始监控历史和全部检查提示。运行记录状态：{_customer(run.get('status'))}；图片来自该运行的保留结果。", sections=tuple(sections))
        output_path = f"report/{run_id}/{'standalone' if request['self_contained'] else 'index'}.html"
        output = relative(root, output_path)
        output.write_text(_html(report, root, output, self_contained=request["self_contained"]), encoding="utf-8")
        artifact = {"path": output_path, "kind": "report", "media_type": "text/html", "run_id": run_id, "label": "CFD计算结果报告"}
        artifacts.append(artifact)
        run = _publish(root, run, artifacts, findings)
        index = _index(root)
        artifacts.append(index)
        run = _publish(root, run, [index])
        result = envelope({"run_id": run_id, "report_path": output_path, "self_contained": request["self_contained"],
                           "engineering_acceptance": "NOT_EVALUATED"})
        result.update(artifacts=artifacts, findings=run["findings"])
        return result
    except ResultError as error:
        return _failure(error, "report")
    except BoundaryError as error:
        return _failure(ResultError("Result access was denied.", error.code), "report")
    except ImportError as error:
        return _failure(ResultError("Plotting dependencies are unavailable; install navifield[plots]. " + str(error), "ENGINE_UNAVAILABLE"), "report")
    except (OSError, ValueError, KeyError, TypeError):
        logging.exception("Retained result report failed")
        return _failure(ResultError("The retained result report could not be exported."), "report")
