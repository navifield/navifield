# Public examples

**English** · [简体中文](README.md)

Choose the quantity you want to calculate. Each worked example includes input generation, a local solver command, result readback and a reference check. The linked walkthroughs are in Chinese. [User guide](../../docs/quick-start.en.md)

| Quantity | Local solver | What you can check |
| --- | --- | --- |
| [Cantilever tip deflection](beam/README.md) | CalculiX | Compare the mean tip deflection with the Euler–Bernoulli beam solution. |
| [Lid-driven cavity velocity](cavity/README.md) | OpenFOAM | Compare velocities along both centrelines with the numerical benchmark by Ghia and colleagues. |
| [Pipe pressure drop](pipe/README.md) | OpenFOAM and Microsoft MPI | Compare the pressure drop between two stations with the Blasius correlation. |
| [Brass-fin temperature](brass-fin/README.md) | CalculiX | Compare eight surface temperatures with the paper's simulation and thermocouple measurements, and check the heat balance. |

To query a workflow for a different problem, start with a sample request for [model selection](model-selection/), [turbulence models](turbulence-selection/) or [equipment workflows](device-chains/). The JSON files in those directories are inputs for the knowledge-base query.
