# User guide

**English** · [简体中文](quick-start.md)

## Start with your question

Open a project workspace in Codex, WorkBuddy or DeepSeek Harness. Paste the [homepage prompt](../README.md) and replace the placeholder with a paper link or the quantity you want to calculate. The three clients use the same explicit [agent instructions](../AGENT.md). Use a client mode that can read the public repository and run commands in your workspace. If it only supports chat, the commands below provide a manual path.

A useful first reply identifies the target, points to a matching record and its source, explains the limits of the verification and asks for any input that changes the choice of workflow. You can ask a second question in the same workspace and reuse the installation. A physical solver is needed when you choose to run a model.

## Prepare the query tools

In an existing project environment, run `python -m pip show navifield` and `navifield capabilities --help`. Use an environment with the query commands shown below. For a fresh setup, use Python 3.10 or newer and the official GitHub wheel. These Windows Command Prompt commands install v0.2.0 into the workspace.

```bat
py -3 -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install "https://github.com/navifield/navifield/releases/download/v0.2.0/navifield-0.2.0-py3-none-any.whl"
python -m pip show navifield
```

On macOS or Linux, use `python3 -m venv .venv` and `. .venv/bin/activate`, then the same pip commands. If the direct download fails, download the wheel from the [GitHub release](https://github.com/navifield/navifield/releases/tag/v0.2.0) and pass its local path to pip. The published SHA-256 is in `SHA256SUMS.txt` on the same page. Check the installed version and command availability when using another distribution channel.

## Try a real query

A DOI search returns a specific paper. Read its identifier, goal, source, reported observations and the scope of any reproduction.

```text
navifield capabilities paper-cases --doi 10.3390/mi12121580
```

For a new problem the agent can save the user's original wording as UTF-8 in `request.json`, e.g. `{"original_request":"I know the inlet and outlet sections and flow rate. What is the pressure drop?"}`, and run the first command below. On a partial match or `needs_review / no_match`, look up a known target with the next two commands. A candidate model still needs compatible conditions and evidence.

```text
navifield route request.json --brief
navifield capabilities target-inventory --target wall_temperature
navifield capabilities paper-cases --target pressure_drop
```

Use `navifield capabilities target-schema` to find target names. `target-inventory` describes model applicability, inputs, quantities exchanged and checks. Keep the paper authors' numerical results, experimental measurements and the open-source reproduction distinct. [Public evidence notes](../navifield/capability_data/PUBLIC_EVIDENCE_BOUNDARY.txt)

## Run a model locally

Choose a [worked example](../examples/cli-v01/README.en.md). Download and extract `navifield-0.2.0-source.zip` from the same [release](https://github.com/navifield/navifield/releases/tag/v0.2.0) for the scripts and inputs. Run the example from the directory containing `pyproject.toml`, install the required local solver and follow the case-specific instructions. Those pages explain how to change inputs, read the output quantity and compare it with a reference. [Solver downloads](../THIRD_PARTY_NOTICES.md) · [Run records and monitoring](local-runs.md)
