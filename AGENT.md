# Using NaviField Open with a coding agent

Read this file explicitly when a user brings a paper or simulation question. Reply in their language. Keep the question, environment, inputs and results together in their workspace so a follow-up can reuse them.

## First answer

Start with what the user asked for.

- **Selection or feasibility only.** Look up NaviField records and relevant upstream manuals, source or published benchmarks. Answer directly with cited candidates, their supported targets and any conditions that still need checking. Read-only lookup needs no confirmation.
- **Unclear purpose or output.** Lead with one or two questions about what changes the route. If the user only names a field or process, ask both whether they want advice or a trial and what quantity they want. Reuse the purpose, target and inputs they have already provided. Ask about process and material later if the answer makes them relevant. Read-only research can continue while you wait.
- **Trial requested.** Confirm the process, target and inputs needed for the proposed model. When you have a specific plan, present one summary and wait for explicit confirmation as described below.

## Read-only lookup

For a release-specific answer, check where the installed package came from as well as its version. Compare a wheel with the release's `SHA256SUMS.txt` (on Windows, `Get-FileHash -Algorithm SHA256 <wheel>` in PowerShell). For an installation from the source archive, verify that archive instead. An older local build can also report 0.2.0. If provenance cannot be confirmed, install the verified public asset in a fresh project environment and repeat the lookup before attributing results to the release.

1. Identify the requested quantity, units and operating conditions. If the user already has compatible results, inspect them before recomputing. Inspect the project Python environment with `python -m pip show navifield` and `navifield capabilities --help`. Reuse a working installation. If the knowledge queries are missing, use the [official v0.2.0 release](https://github.com/navifield/navifield/releases/tag/v0.2.0) in a project virtual environment. The source ZIP includes the examples. The wheel is sufficient for querying. Record the interpreter path and installed version. Knowledge queries work offline without a solver. [English setup](docs/quick-start.en.md) · [中文安装](docs/quick-start.md)
2. Run a real read-only query. For a DOI, start with `navifield capabilities paper-cases --doi DOI`. For a description, write UTF-8 `request.json` with the user's words in `original_request`, then run `navifield route request.json --brief`. If partial or empty, retry by DOI, target quantity (e.g. `navifield capabilities target-inventory --target pressure_drop`) or a candidate chain and operating conditions. Read the source/version, observable, assumptions and validation limits.
3. Give the route, the specific evidence supporting it, and the next action. For a paper hit, include its DOI, exact catalog identifier, target quantity and source link in the reply. Distinguish database hits, your physical inference and open questions. A paper record can identify a reference without providing an executable deck. A search miss alone is insufficient evidence of a development gap.

## Does an existing case fit?

Name the actual script or complete case, then compare the geometry, physical assumptions, boundaries, output sampling and reference check with the user's request. A model-selection hit supplies evidence for a route. Check separately for an executable case and a reader for the requested quantity. For a user-supplied case, identify the inputs already prepared and which outputs can be checked.

For example, the public cylindrical fin accepts a uniform side-film coefficient and assumes an insulated tip. If only that coefficient is missing, ask for it and include its supplied value in the plan for reusing the case. A convective tip changes the deck **and** the heat-balance readback: account for tip heat loss and check base input against side plus tip losses. Validate the new boundary against an independent solution and check the insulated-tip limit. For a cold plate, a conjugate heat-transfer model match still needs the actual geometry, material stack, heat loads, boundary and interface conditions, mesh, native solution and chip-wall maximum readback before a temperature can be reported.

## Before starting a calculation

1. **Prepare the plan.** Use a matching [public example](examples/cli-v01/README.md), a compatible user-provided case or a clearly scoped new model. Ask only for inputs that affect the choice or result. Check whether the required solver and inputs are available. If an input, executable or reader is missing, explain the specific blocker and the next action. [Solver sources](THIRD_PARTY_NOTICES.md)
2. **Show one summary and wait.** State the case or model, the inputs and their sources, key assumptions, the expected outputs and units, and what you will compare them against. Label published inputs and any illustrative or assumed values. Ask the user to confirm this specific plan. Explicit approval of that plan authorizes execution. A general request to try a calculation, a parameter update, silence or a request to hold leaves the plan awaiting approval. Apply this gate before launching a native solver or running any Python script, notebook or other method that produces trial results, including one-dimensional approximations and demonstrations.
3. **Run after confirmation.** Run the confirmed plan in a new run directory. Check completion, read the requested quantity from native output or the agreed calculation output, and report the value, units, location and comparison with the stated reference. Preserve inputs and run IDs and identify any remaining validation. Confirm each plan once. If the process, requested output or a key physical assumption changes, show the revised plan and get explicit confirmation before running it.

## When development is needed

Approval to calculate and approval to post a public GitHub issue are separate.

- **Propose new development.** Check the release's templates, compatible user inputs, native solver capabilities and result readers. Explain what already works, which model, coupling interface or reader is missing, and which observable consequently remains unavailable. Draft a public title and body with the scenario or public paper link, requested quantity, attempted route, precise gap and any available validation reference. Omit private paths, attachments, unpublished paper text and unapproved product parameters. Show the exact draft to the requester and ask: **你愿意把这份需求作为公开 GitHub Issue 提交给 NaviField 维护者吗？** In English: “Would you like to submit this draft to the NaviField maintainers as a public GitHub issue?”
- With explicit consent to **this draft**, a posting-capable client may submit it once and return the actual issue URL; alternatively provide the draft and the [request form](https://github.com/navifield/navifield/issues/new?template=simulation-request.yml). After refusal or silence, keep it in the conversation. A material change to the draft needs fresh consent. For a reproducible failure of an existing feature use the [bug form](https://github.com/navifield/navifield/issues/new?template=bug-report.yml), sharing only public material.

Before showing a public draft, check each number against a public source or the requester's explicit permission. Replace other values with a symbol, range description or placeholder. This also applies when proposing a future test case. Keep the private calculation inputs in the workspace.

## Local run records

Pass the executable and each argument separately after `--`. The working directory is relative to the project. For example:

```text
navifield run --project work/beam --cwd case --cores 1 --wall-seconds 120 -- ccx -i beam
navifield show --project work/beam --rid r001
```

Use the returned run ID. For additional project inputs use `--input` with a project-relative path. For a long job, add `--background` and use `wait`, `status` or `stop`; confirm the terminal state after a stop request. A wait exit code of 3 means the job was still running, 0 means it finished successfully, and 1 covers other or unknown terminal outcomes. Inspect the solver exit code, native log and inputs. `--full` expands the summary; `report` and `self-check` collect recorded facts and declared checks. Validate the physical observable against the example's formula or data. [Run records and monitoring](docs/local-runs.md)
