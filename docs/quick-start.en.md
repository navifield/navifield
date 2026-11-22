# Quick start

**English** · [简体中文](quick-start.md)

Open a new local workspace in Codex or WorkBuddy, or start DeepSeek Harness in a project directory. Paste the [homepage prompt](../README.md) with your question or DOI. Ask the client to read [AGENT.md](../AGENT.md) explicitly. It needs access to the repository and permission to run local commands. If it cannot run them, use the commands below and bring the output back to the conversation.

## Find a route before choosing a solver

Check the project environment with `python -m pip show navifield` and `navifield capabilities --help`. Reuse it if the queries are available. Otherwise download and extract `navifield-0.2.0-source.zip` from the [official release](https://github.com/navifield/navifield/releases/tag/v0.2.0). It contains the CLI, offline knowledge base and example scripts. With 64-bit Python 3.10+ on Windows, open Command Prompt in the directory containing `pyproject.toml`:

```bat
py -3 -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install .
python -m pip show navifield
navifield capabilities paper-cases --doi 10.17482/uumfd.446304
```

For a question in plain language, have the agent write UTF-8 `request.json` with a single `original_request` field containing your words. Run `navifield route request.json --brief`. On a partial or empty match, try the DOI, target or specific operating conditions. For pressure drop, try `navifield capabilities target-inventory --target pressure_drop`. Read back which quantity was studied, the model version, its applicability and the source. Identify which conclusions are supported by records, which are inferences and which inputs remain open. Reuse the interpreter and project for your next question.

## Prepare the query tools

In an existing project environment, run `python -m pip show navifield` and `navifield capabilities --help`. Use an environment with the query commands shown below. For a fresh setup, use Python 3.10 or newer and the official GitHub wheel. These Windows Command Prompt commands install v0.2.0 into the workspace.

```bat
py -3 -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install "https://github.com/navifield/navifield/releases/download/v0.2.0/navifield-0.2.0-py3-none-any.whl"
python -m pip show navifield
```

On macOS or Linux, use `python3 -m venv .venv` and `. .venv/bin/activate`, then the same pip commands. If the direct download fails, download the wheel from the [GitHub release](https://github.com/navifield/navifield/releases/tag/v0.2.0) and pass its local path to pip. The published SHA-256 is in `SHA256SUMS.txt` on the same page. Check the installed version and command availability when using another distribution channel.

## Check whether the example fits

Ask for the executable case and compare its geometry, assumptions, boundaries, readback and reference with your problem. For instance, you can change the side-film coefficient of the cylindrical fin. A convective tip also needs a new boundary and an updated heat-balance calculation. A cold-plate model match still needs case-specific conditions, a mesh, a solution and a temperature readback on the chip region.

## Take the next step

- **Run the model.** Once the model fits, required inputs are known and a local solver works, run a matching [public example](../examples/cli-v01/README.en.md) or the user's compatible deck. Read the target from native output. Return its value, units, run ID, result path and comparison with a named reference. Identify outstanding validation. Save changed inputs and results separately. [Running and monitoring jobs](local-runs.md)
- **Complete an input.** Ask only for a value that changes the model or result and explain why. A cantilever with known geometry, material and loading pattern still needs the total end load to predict deflection. Continue on the same route when the user supplies it. If the solver is missing, configure that executable and its libraries using the [solver sources](../THIRD_PARTY_NOTICES.md), then continue.
- **Request development.** Explain the step that works, the missing model or interface and the quantity it prevents you from reporting. Show a public title and body covering the scenario or paper, desired quantity, attempted route, specific gap and available reference. Ask “Would you like to submit this draft to the NaviField maintainers as a public GitHub issue?” After explicit consent to that draft, a capable client can post it once and return the real URL, or the user can paste it into the [request form](https://github.com/navifield/navifield/issues/new?template=simulation-request.yml). On refusal or no answer, retain the draft in the conversation. For a reproducible fault in an existing feature use the [bug form](https://github.com/navifield/navifield/issues/new?template=bug-report.yml).
