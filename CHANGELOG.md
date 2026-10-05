# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

First public version, planned as 0.1.0.

### Added

- `vibespice run`: iterative agent that gives a task to a model through the Open WebUI API
  and runs ngspice locally until the model gives a final answer. The task can come from
  the command line, a file or standard input.
- `vibespice bench`: challenges whose final answer is verified independently by
  re-simulating it. Without IDs, lists them.
- Other commands: `check` (connection, model and tool calls), `status` (models loaded on
  the server), `selftest` (no AI) and `analyze` (statistics of the saved runs).
- Native tool calls (`--mode native`) and calls written as text (`--mode text`), with
  detection of loops, empty replies, a missing final JSON block and results that do not
  come from the model's own simulations.
- Tools for the model in `vibespice/tools.py`: `simulate`, `analyze_tolerances` (corners and
  Monte Carlo), `standard_values` (E3 to E96) and `calculate`.
- Netlist safety: directive allow-list, `.control`, `shell`, `.include`, `.lib` and
  `.osdi` blocked, 30 s per simulation and a 5000-sample Monte Carlo cap.
- Eight challenges with independent verification (`vibespice/challenges.py`), from a warm-up
  simulation to biasing a 2N3904 that is stable against β.
- Batches: `bench all`, `--repeat` and `--time-limit`, with progress, time estimates
  from previous runs and a desktop notification at the end.
- Reasoning control with `--think` (yes, no or the model's own levels), checked against
  the model card, and per-family sampling profiles.
- Logs in `logs/`: one Markdown and one JSON file per run, and `summary.csv`.
- `analyze`: pass rate, times, tool use and signs of common mistakes, per
  challenge and configuration, optionally split by code version (`--by-version`).
- Traceability: `--version`, and every log and `summary.csv` row record the version and
  the git commit of the code (`+changes` when there were uncommitted changes).
- `selftest` and `tests/run_tests.py` (end-to-end tests against a fake Open WebUI).
- Guided menu when run without arguments in a terminal.
- Apache-2.0 license with a `NOTICE` file and SPDX license headers in every source file.
- Installable package (`pyproject.toml`, no runtime dependencies): `pipx install` or
  `pip install` gives the `vibespice` command; from a clone, `python3 -m vibespice`.

### Fixed

- The hint "the value must be a number, not an expression" for a failed `.meas` now also
  appears with ngspice versions that report that error differently (for example
  ngspice 42).
