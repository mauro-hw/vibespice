# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

First public version, planned as 0.1.0.

### Added

- `vibespice run`: iterative agent that gives a task to a model and runs ngspice locally
  until the model gives a final answer. The task can come from the command line, a file
  or standard input.
- Providers, one per profile: `anthropic` (the Claude API, over plain HTTP), `openai` (any
  OpenAI-compatible API: OpenAI, OpenRouter, Ollama, vLLM, LM Studio…) and `openwebui`.
  With Claude: the conversation goes back exactly as it came, automatic prompt caching,
  `--think` as the effort level (`yes` = `high`), handling of declined requests and the
  server-side fallback model on the models that offer it. Keys can also come from
  `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`. `vibespice init` now starts with a Claude profile
  and commented examples for the others.
- Retries, up to 4, for rate limits (429), busy servers (529, 503…) and dropped connections,
  honoring `retry-after`.
- Every log and `summary.csv` row record the provider, the total input tokens and the cached
  ones. A copy installed with pipx from GitHub records the commit it was installed from, and
  `--version` shows it.
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
- Configuration outside the code folder, in `~/.config/vibespice/config.toml`, with one
  profile per server (`--profile`, `default_profile`). `vibespice init` creates it,
  readable only by you. `VIBESPICE_*` environment variables take priority over it.
- Logs in `~/.local/share/vibespice/logs/` (or `logs_dir`, or `VIBESPICE_LOGS`): one
  Markdown and one JSON file per run, and `summary.csv`.
- `analyze`: pass rate, times, tool use and signs of common mistakes, per
  challenge and configuration, optionally split by code version (`--by-version`).
- Traceability: `--version`, and every log and `summary.csv` row record the version and
  the git commit of the code (`+changes` when there were uncommitted changes).
- `selftest` and `tests/run_tests.py` (end-to-end tests against a fake LLM server that
  speaks the three APIs).
- Guided menu when run without arguments in a terminal.
- AGPL-3.0-only license, with a `NOTICE` that must be kept (an additional term on
  attribution, under section 7(b)) and SPDX headers in every source file. `--version`
  shows the copyright, the license, that there is no warranty and where the source is.
- Installable package (`pyproject.toml`, no runtime dependencies): `pipx install` or
  `pip install` gives the `vibespice` command; from a clone, `python3 -m vibespice`.

### Fixed

- The hint "the value must be a number, not an expression" for a failed `.meas` now also
  appears with ngspice versions that report that error differently (for example
  ngspice 42).
