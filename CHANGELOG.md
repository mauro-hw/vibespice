# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-10-06

First public version. Nothing was released before it, so this list describes everything
vibespice does.

**What it has been tried with.** The tests without a model (`selftest` and
`tests/run_tests.py`) pass on Fedora 44 (Python 3.14, ngspice 47) and on Ubuntu 24.04 in
GitHub Actions (Python 3.11 to 3.14, ngspice 42). With real models: the agent with Open WebUI
and qwen3.8:27b-q8_0, and `vibespice mcp` with Claude Code on Fedora 44. Not tried yet with
real models: the Claude API and OpenAI-compatible APIs (only against the test server), and
Claude Desktop, Codex and Gemini CLI; nor on macOS or Windows (WSL).

### Added

**Designing from a prompt**
- `vibespice run "task"`: gives a task to a model and runs ngspice on your computer, in a
  loop, until the model gives its final answer. The task can also come from a file
  (`--file`) or standard input (`--file -`).
- Guided menu when `vibespice` runs alone in a terminal: it asks what you want to do
  (setting up a model or API included) and shows the exact command before running it.
- Tools for the model: `simulate` (operating point, transistor quantities, connections in
  words and `.meas` results), `analyze_tolerances` (corners and Monte Carlo, also over a
  `.meas`), `standard_values` (E3 to E96) and `calculate` (exact, with SPICE suffixes).
- Help for the model: warnings about the usual mistakes (title line, `M` = milli, missing
  ground, `.step`, failed `.meas`), the E series of every value, the circuit described in
  words, floating and numbered nodes, saturated or cut-off transistors, sources in parallel
  and repeated `.model` names.
- Safety for the netlists the model writes: directive allow-list; `.control`, `shell`,
  `.include`, `.lib` and `.osdi` blocked; 30 s per simulation; Monte Carlo capped at 5000
  samples.
- Native tool calls (`--mode native`) and calls written as text (`--mode text`), with
  detection of loops, empty replies and a missing final JSON block.

**Models and APIs**
- Three providers, one per profile: `anthropic` (the Claude API), `openai` (OpenAI and any
  OpenAI-compatible API: OpenRouter, Ollama, vLLM, LM Studio…) and `openwebui` (Open WebUI
  with Ollama). All over plain HTTP, with no dependencies.
- With Claude: the conversation goes back exactly as the API returned it, automatic prompt
  caching, the reasoning set with the effort level, declined requests reported with their
  reason, and the fallback model Anthropic recommends on the models that offer it
  (`fallbacks = false` turns it off).
- `--think` sets the reasoning: yes, no or a level the model accepts (Claude: `low` to
  `max`, with `yes` = `high`; Open WebUI: the model's own levels; OpenAI-compatible APIs:
  `reasoning_effort`). vibespice checks the level against the model before starting.
- Per-family sampling for the qwen3 models (their vendor's recommendation), on Open WebUI
  and OpenAI-compatible APIs.
- Retries, up to 4, for rate limits (429), busy servers (529, 503…) and dropped
  connections, honoring the wait the server asks for.
- `vibespice check`: ngspice, the key, the model (with its reasoning levels), a chat and a
  tool call. `vibespice status`: the models loaded on an Open WebUI server.

**From a chat app, without an API key**
- `vibespice mcp`: an MCP server over standard input and output that lends the four tools,
  with the same safety limits, to a chat app (Claude Desktop, Claude Code, Codex, Gemini
  CLI). The app's model, from the user's own account, runs the loop; vibespice runs ngspice
  on the user's computer. It speaks both generations of the protocol, 2026-07-28 and the
  ones with the `initialize` handshake (2024-11-05 to 2025-11-25), and gives the app
  instructions with the netlist rules and the way of working of `vibespice run`.
- Run in a terminal, `vibespice mcp` shows what to add to each app, with the full paths of
  vibespice and ngspice: the apps do not start it from a terminal, so they may not find
  them in the PATH.

**Measuring a model**
- `vibespice bench`: eight challenges, from a warm-up simulation to biasing a 2N3904 that is
  stable against β, whose final answer is verified independently by re-simulating it.
  Without IDs, it lists them.
- The results a model reports must come from its own successful simulations (and the %
  changes, from two of them); otherwise it is asked to measure them, and the run fails if
  it doesn't.
- Batches: `bench all`, `--repeat` and `--time-limit`, with progress, time estimates from
  your history and a desktop notification at the end. The computer does not suspend by
  itself while a batch runs.
- `vibespice analyze`: pass rate, times, tool use and signs of common mistakes, per
  challenge and configuration, optionally split by code version (`--by-version`).

**Configuration, results and installation**
- Installable with `pipx install git+https://github.com/mauro-hw/vibespice` (no runtime
  dependencies); from a clone, `python3 -m vibespice`.
- `vibespice init` asks which API to use (Claude, OpenAI, OpenRouter, Ollama, another
  OpenAI-compatible API or Open WebUI), the key (hidden while you paste it) and the model,
  from the list the API offers. It checks the key at once, lets you try again, writes the
  file readable only by you and offers to run `check`. Run again, it adds another profile
  without touching the others; `--template` writes a commented file instead.
- Configuration outside the code folder, in `~/.config/vibespice/config.toml`, with one
  profile per model or API (`default_profile`, `--profile`). Keys can also come from
  `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`, and `VIBESPICE_*` environment variables take
  priority over the file.
- Results in `~/.local/share/vibespice/logs/` (or `logs_dir`, or `VIBESPICE_LOGS`): one
  Markdown and one JSON file per run, and `summary.csv` with one row per run (provider,
  model, result, steps, time, input tokens, cached tokens and output tokens).
- Traceability: every log and `summary.csv` row record the version and the git commit of
  the code (`+changes` with uncommitted changes; a copy installed with pipx from GitHub
  records the commit it was installed from). `--version` shows it, with the license.
- `vibespice selftest` (no model needed) and `tests/run_tests.py`: end-to-end tests against
  a fake server that speaks the three APIs, and a scripted MCP client.
- AGPL-3.0-only license, with a `NOTICE` that must be kept (an additional term on
  attribution, under section 7(b)) and SPDX headers in every source file.

### Fixed

- The hint "the value must be a number, not an expression" for a failed `.meas` now also
  appears with ngspice versions that report that error differently (for example
  ngspice 42).
