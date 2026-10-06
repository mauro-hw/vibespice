# Security policy

## Supported versions

Security fixes go into the latest release and `main`. VibeSPICE is at version 0.x, so
there is no long-term support for older versions: update with `pipx upgrade vibespice`.

## What to report

VibeSPICE runs netlists written by a language model on your computer, and it handles your
API keys. Please report privately:

- A netlist that gets past the safety limits: one that runs commands, reads or writes files
  outside its temporary folder, gets through the block on `.control`, `shell`, `.include`,
  `.lib` or `.osdi`, or escapes the 30 s limit per simulation or the 5000-sample cap on
  Monte Carlo.
- A way for an API key to leak: into the logs, `summary.csv`, the terminal, an error
  message, or a configuration file that other users of the computer can read.
- `vibespice mcp` doing anything other than run its four tools.

These are not security problems; open a normal issue for them:

- A model that proposes a wrong or unsafe circuit. VibeSPICE checks the answers to its
  challenges, but not free tasks: check those yourself before you build anything.
- How a model provider handles the data you send it. That depends on the provider and your
  account with it.

## How to report

Use GitHub's private vulnerability reporting: go to the
[Security tab](https://github.com/mauro-hw/vibespice/security) and choose **Report a
vulnerability**. Only the maintainer sees the report. Do not open a public issue.

Include the output of `vibespice --version`, your system, and the netlist or the steps that
reproduce the problem. Never include a working API key.

## What happens next

VibeSPICE is maintained by one person in their spare time. You will get an answer as soon
as possible. A confirmed problem is fixed in a new release, described in the changelog, and
credited to you if you wish.
