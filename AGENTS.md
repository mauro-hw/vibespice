# AGENTS.md

Guidance for contributors and coding agents (Claude Code, Codex, Cursor and others) working
on this repository.

## What this is

VibeSPICE is an agent that designs and simulates analog circuits from a prompt
(`vibespice run`). It talks to a language model through its API (Claude, any
OpenAI-compatible API or Open WebUI) and runs ngspice locally, in a loop, until the model
gives a final answer. It also has a benchmark (`vibespice bench`): challenges whose answers
are verified independently by re-simulating them.

## Layout

Everything lives in the `vibespice/` package (`python3 -m vibespice`, or the `vibespice`
command once installed):

- `vibespice/cli.py`: the commands (`run`, `bench`, `check`, `status`, `selftest`,
  `analyze`) and the guided menu. Nothing else parses arguments.
- `vibespice/config.py`: the configuration file (profiles), environment variables and the
  logs folder, always outside the repository.
- `vibespice/wizard.py`: `vibespice init` in a terminal (questions, key check, new profile).
- `vibespice/agent.py`: agent loop, logs and verification of one run.
- `vibespice/providers/`: one module per API (`anthropic.py`, `openai_chat.py` for Open
  WebUI and OpenAI-compatible APIs) behind a common interface in `base.py`: `Provider`
  (requests, retries, reasoning levels, `check` details) and the `Conversation` it creates
  (the messages in the API's own format).
- `vibespice/batch.py`: running jobs one after another, time limits and estimates.
- `vibespice/checks.py`: `check` (connection) and `selftest` (tools and verifiers, no AI).
- `vibespice/console.py`: terminal output, notification and keeping the computer awake.
- `vibespice/mcp.py`: `vibespice mcp`, the same tools for a chat app over MCP (stdio, both
  protocol generations), and the instructions to add it to each app.
- `vibespice/tools.py`: tools offered to the model (`simulate`, `analyze_tolerances`,
  `standard_values`, `calculate`) and their schemas.
- `vibespice/challenges.py`: challenges, verifiers and reference answers (correct and wrong).
- `vibespice/analyze.py`: statistics of `logs/` (read-only; the tests use it too).
- `pyproject.toml`: package metadata. No runtime dependencies.
- `tests/`: a fake LLM server that speaks the three APIs (Open WebUI, OpenAI-compatible
  and Claude), with a scripted "model", a scripted MCP client and end-to-end tests.
- `CHANGELOG.md`: what changed in each version.
- `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md` and `.github/ISSUE_TEMPLATE/`,
  `.github/pull_request_template.md`: for people who contribute. Keep `CONTRIBUTING.md` and
  the pull request checklist in step with the rules below.

## Rules

- **Standard library only**, compatible with Python 3.11 to 3.14.
- **License header on every code file**, right after the shebang if there is one:
  ```python
  # SPDX-License-Identifier: AGPL-3.0-only
  # Copyright 2026 Mauro Rodriguez Blasco
  # Additional term under section 7(b) of the license: see NOTICE.
  ```
  The license is the GNU AGPL v3 only. Keep `LICENSE` unchanged (it is the official text
  from gnu.org) and keep `NOTICE`, with its attribution term.
- **Anything served over a network must link to its source code** (section 13 of the
  AGPL). The planned web app shows that link, and `vibespice --version` prints it.
- **English everywhere**: code, identifiers, comments, messages, prompts and docs.
- **The name is VibeSPICE** wherever people read it: docs, messages, titles, prompts.
  `vibespice`, in lower case, only where it is an identifier: the command, the package,
  paths, `VIBESPICE_*` variables, the MCP server key and URLs.
- **Nothing personal lives in the repository.** The configuration, with the API key, is
  `~/.config/vibespice/config.toml` and the logs go to `~/.local/share/vibespice/logs/`.
  Never commit either; `config.toml` and `logs/` are in `.gitignore` in case someone points
  them at the repository.
- **Tests never read the developer's configuration or logs.** `tests/run_tests.py` gives
  each run its own `XDG_CONFIG_HOME` and `XDG_DATA_HOME` and drops every `VIBESPICE_*`
  variable; keep it that way when adding tests.
- **Model netlists run on the user's machine.** Keep the directive allow-list and the block
  on `.control`, `shell`, `.include`, `.lib` and `.osdi`. Do not loosen the simulation
  timeout or the Monte Carlo cap without a reason recorded in the changelog.
- **Every new challenge needs independent verification** (re-simulate with ngspice) and
  entries in `CORRECT_REFERENCES` and `WRONG_REFERENCES` for the self-test. If its JSON
  contains simulation results, declare them in `measured` (the agent requires them to come
  from the model's own simulations), and in `derived` the ones that are the % change of a
  quantity between two simulations.
- **The agent loop only uses the provider interface.** Anything specific to one API (its
  message format, reasoning parameters, error hints) goes in its provider, and a new API
  is a new provider plus its dialect in `tests/fake_server.py`.
- **The conversation is append-only.** Never edit or drop an earlier turn: the Claude API
  rejects a history whose assistant turns changed (thinking blocks included), and the fake
  server checks it.
- **In `vibespice mcp`, stdout carries protocol messages only.** Anything else, including a
  stray `print`, breaks the chat app's connection: diagnostics go to stderr. A new tool
  reaches the chat apps on its own; give it a title in `mcp.TITLES` for them to show.
- **Tests never use real keys.** `tests/run_tests.py` drops `ANTHROPIC_API_KEY` and
  `OPENAI_API_KEY` along with every `VIBESPICE_*` variable.
- **Do not change the default `--num-ctx auto`.** A request with a different `num_ctx`
  makes Ollama reload the model, which hurts everyone else sharing the server.
- **Changes to the tool outputs or the prompts change model behavior.** Mention them in the
  changelog so results before and after can be told apart.

## Before every commit

Both must pass. Check their exit code (0), not just the text: a `grep` over the output
succeeds even if there are lines with ❌.

```bash
python3 -m vibespice selftest
python3 tests/run_tests.py
```

GitHub Actions runs both on every pull request and every push to `main`, with Python 3.11
to 3.14 and the ngspice that Ubuntu 24.04 ships (`.github/workflows/tests.yml`).

Runs against a real model cannot happen in CI; contributors run them locally and attach
the relevant logs or output when it matters.

## Versions and changelog

- The project follows [Semantic Versioning](https://semver.org/). The version lives in
  `__version__` in `vibespice/__init__.py` and is written to every log next to the git commit.
- `CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com/). Every change that
  a user would notice gets a line under `## [Unreleased]` **in the same commit**.
- To release: set `__version__` (for example `0.1.0`), rename `## [Unreleased]` to
  `## [0.1.0] - YYYY-MM-DD`, say under it what the version has been tried with (systems,
  apps, real models, counting the compatibility reports opened since the last release; and
  what not yet), open a new empty `## [Unreleased]`, commit, then tag
  `v0.1.0` and push the tag. After the release, move `__version__` to the next `.dev0`.
- While the version is `0.x`, minor versions may change the CLI, the log format or the tool
  names; say so in the changelog.

## Commits

- Small, focused commits with a message that explains why, not only what.
- Work in branches; `main` stays stable and passing.
- Coding agents may commit, push their branch and open a pull request without asking.
  They never push to `main` and never merge on their own: the maintainer approves every
  merge, and an agent merges a pull request only when the maintainer says so for that one.
