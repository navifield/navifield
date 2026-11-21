"""Run the brass-fin example with the installed NaviField CLI and a user-supplied CalculiX."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys


def invoke(cli, *args):
    process = subprocess.run([cli, *map(str, args)], capture_output=True, encoding="utf-8",
                             errors="replace", check=False)
    if process.returncode:
        raise RuntimeError(f"NaviField {args[0]} exited {process.returncode}\n{process.stdout}\n{process.stderr}")
    return process.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ccx", required=True, help="Path to the CalculiX executable")
    parser.add_argument("--project", type=Path, required=True, help="New project directory")
    args = parser.parse_args()
    ccx = Path(args.ccx).resolve(strict=True)
    cli_name = "navifield.exe" if sys.platform == "win32" else "navifield"
    cli = Path(sys.executable).with_name(cli_name)
    if not cli.is_file():
        parser.error("Install NaviField in the Python environment running this example")
    root = args.project.resolve()
    root.mkdir(parents=True, exist_ok=False)
    case = root / "case"
    case.mkdir()
    source = Path(__file__).resolve().parent
    model = case / "model.json"
    shutil.copyfile(source / "model.json", model)
    deck = case / "model.inp"
    build = json.loads(invoke(cli, "fin", "build", model, "--deck", deck))
    run_id = invoke(cli, "run", "--project", root, "--cwd", "case", "--cores", 1,
                    "--memory-gib", 2, "--wall-seconds", 180, "--input", "case/model.inp",
                    "--", ccx, "-i", "model")
    run = json.loads(invoke(cli, "show", "--project", root, "--rid", run_id))
    if run.get("state") != "completed" or run.get("exit_code") != 0:
        raise RuntimeError(f"Solver run did not complete: {run}")
    # Reference temperatures are used only after the native solver has completed.
    reference = json.loads((source / "reference.json").read_text(encoding="utf-8"))
    positions = reference["positions_m"]
    field = json.loads(invoke(cli, "fin", "read", "--deck", deck, "--dat", case / "model.dat",
                              "--positions-m", *positions))
    if field["input_sha256"] != build["input_sha256"]:
        raise ValueError("Readback deck differs from the generated solver input")
    (root / "readback.json").write_text(json.dumps(field, indent=2), encoding="utf-8")
    samples = field["samples"]
    if [row["z_m"] for row in samples] != positions:
        raise ValueError("Returned observation positions differ from the requested stations")
    rows = []
    for i, point in enumerate(samples):
        temp = point["lateral_mean_c"]
        rows.append({"sensor": reference["sensor_ids"][i], "z_m": point["z_m"], "native_c": temp,
                     "prior_c": reference["previous_calculix_2_22_c"][i],
                     "delta_prior_k": temp - reference["previous_calculix_2_22_c"][i],
                     "delta_comsol_k": temp - reference["author_comsol_5_2_c"][i],
                     "delta_experiment_k": temp - reference["thermocouple_c"][i]})
    maximum = max(abs(row["delta_prior_k"]) for row in rows)
    result = {"run_id": run_id, "mesh": build, "points": rows,
              "max_abs_delta_prior_k": maximum, "tolerance_k": reference["repeatability_tolerance_k"],
              "comsol_mae_k": sum(abs(r["delta_comsol_k"]) for r in rows) / len(rows),
              "experiment_mae_k": sum(abs(r["delta_experiment_k"]) for r in rows) / len(rows),
              "heat_w": field["heat_w"], "energy_closure": field["energy_closure"],
              "passed": maximum <= reference["repeatability_tolerance_k"] and field["energy_closure"]["passed"]}
    (root / "comparison.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
