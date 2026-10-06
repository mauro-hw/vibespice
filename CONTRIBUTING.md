# Contributing to VibeSPICE

VibeSPICE is developed by its maintainer, with the help of coding agents. For now the code
is not open to outside contributions: pull requests from outside the project will be
closed. You can still help a lot, and none of it needs programming.

By taking part in issues and discussions you agree to follow the
[code of conduct](CODE_OF_CONDUCT.md).

## Ways to help

**Try it where nobody has yet.** The [changelog](CHANGELOG.md) says, for each version, what
it has been tried with. Version 0.1.0 has not been tried on macOS or Windows (WSL), with the
Claude API or OpenAI-compatible APIs against real models, nor from Claude Desktop, Codex or
Gemini CLI. If you use any of them, open a
[compatibility report](https://github.com/mauro-hw/vibespice/issues/new?template=compatibility.yml),
whether it worked or not.

**Report a bug.** Open a
[bug report](https://github.com/mauro-hw/vibespice/issues/new?template=bug_report.yml). The
form asks for what helps most: the output of `vibespice --version`, your system, the
command and what you saw. If you know how to fix it, describe the fix in the issue.

**Suggest an idea or a challenge.** Open a
[feature request](https://github.com/mauro-hw/vibespice/issues/new?template=feature_request.yml).
A good benchmark challenge has an answer that can be checked by simulating it again, and a
reference solution you have checked yourself.

> **Security problems go elsewhere.** If you find a netlist that gets past the safety limits,
> or a way for your API key to leak, do not open a public issue: follow
> [SECURITY.md](SECURITY.md).

**Never paste your API key or your `config.toml`** in an issue. Read a log before you attach
it: it holds your whole task and the model's replies.

## Changing the code

Only the maintainer, the people they invite and their coding agents change the code.
Whoever does it follows [AGENTS.md](AGENTS.md), runs both checks (they need ngspice, and both
must end with exit code 0) and fills in the pull request checklist:

```bash
python3 -m vibespice selftest
python3 tests/run_tests.py
```

Only the maintainer decides what is merged.
