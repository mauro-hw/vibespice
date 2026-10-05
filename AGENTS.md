# AGENTS.md

Guidance for contributors and coding agents (Claude Code, Codex, Cursor and others) working
on this repository.

## What this is

vibespice measures how well a language model designs analog circuits. `spice_agent.py` talks
to the model through the Open WebUI API and runs ngspice locally, in a loop, until the model
gives a final answer; that answer is then verified independently by re-simulating it.

## Layout

- `spice_agent.py`: CLI, Open WebUI client, agent loop, logs, verification and self-test.
- `spice_tools.py`: tools offered to the model (`simulate`, `analyze_tolerances`,
  `standard_values`, `calculate`) and their schemas.
- `challenges.py`: challenges, verifiers and reference answers (correct and wrong).
- `analyze_logs.py`: statistics of `logs/` (read-only; the tests use it too).
- `tests/`: fake Open WebUI with a scripted "model" and end-to-end tests.
- `CHANGELOG.md`: what changed in each version.

## Rules

- **Standard library only**, compatible with Python 3.11 to 3.14.
- **License header on every code file**, right after the shebang if there is one:
  ```python
  # SPDX-License-Identifier: Apache-2.0
  # Copyright 2026 Mauro Rodriguez Blasco
  ```
  Keep `LICENSE` unchanged and keep the attribution in `NOTICE`.
- **English everywhere**: code, identifiers, comments, messages, prompts and docs.
- **Never commit `agent.conf`, keys or `logs/`.** They are in `.gitignore`.
- **Model netlists run on the user's machine.** Keep the directive allow-list and the block
  on `.control`, `shell`, `.include`, `.lib` and `.osdi`. Do not loosen the simulation
  timeout or the Monte Carlo cap without a reason recorded in the changelog.
- **Every new challenge needs independent verification** (re-simulate with ngspice) and
  entries in `CORRECT_REFERENCES` and `WRONG_REFERENCES` for the self-test. If its JSON
  contains simulation results, declare them in `measured` (the agent requires them to come
  from the model's own simulations), and in `derived` the ones that are the % change of a
  quantity between two simulations.
- **Do not change the default `--num-ctx auto`.** A request with a different `num_ctx`
  makes Ollama reload the model, which hurts everyone else sharing the server.
- **Changes to the tool outputs or the prompts change model behavior.** Mention them in the
  changelog so results before and after can be told apart.

## Before every commit

Both must pass. Check their exit code (0), not just the text: a `grep` over the output
succeeds even if there are lines with ❌.

```bash
python3 spice_agent.py --selftest
python3 tests/run_tests.py
```

GitHub Actions runs both on every pull request and every push to `main`, with Python 3.11
to 3.14 and the ngspice that Ubuntu 24.04 ships (`.github/workflows/tests.yml`).

Runs against a real model cannot happen in CI; contributors run them locally and attach
the relevant logs or output when it matters.

## Versions and changelog

- The project follows [Semantic Versioning](https://semver.org/). The version lives in
  `__version__` in `spice_agent.py` and is written to every log next to the git commit.
- `CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com/). Every change that
  a user would notice gets a line under `## [Unreleased]` **in the same commit**.
- To release: set `__version__` (for example `0.1.0`), rename `## [Unreleased]` to
  `## [0.1.0] - YYYY-MM-DD` and open a new empty `## [Unreleased]`, commit, then tag
  `v0.1.0` and push the tag. After the release, move `__version__` to the next `.dev0`.
- While the version is `0.x`, minor versions may change the CLI, the log format or the tool
  names; say so in the changelog.

## Commits

- Small, focused commits with a message that explains why, not only what.
- Work in branches; `main` stays stable and passing.
