# VibeSPICE

[![tests](https://github.com/mauro-hw/vibespice/actions/workflows/tests.yml/badge.svg)](https://github.com/mauro-hw/vibespice/actions/workflows/tests.yml)
[![Python 3.11 to 3.14](https://img.shields.io/badge/python-3.11%20to%203.14-blue)](#step-2-install-python-ngspice-and-pipx)
[![License: AGPL-3.0-only](https://img.shields.io/badge/license-AGPL--3.0--only-blue)](#7-versions-and-license)

**Describe a circuit in plain words: a language model designs it and checks it with a real
SPICE simulator (ngspice), on your computer.**

You write something like *"Design a 24 V to 3.3 V divider with E12 resistors and give me the
worst case with 5 % tolerance"*. The model (Claude, a model from OpenAI or OpenRouter, or one
running on your own server) proposes a circuit and asks for a simulation. VibeSPICE runs
ngspice on your computer and sends the result back. The model adjusts the design and
simulates again, as many times as it needs, until it gives its final answer. Everything is
saved: its reasoning, every netlist and every result.

VibeSPICE also includes a **benchmark**: eight design challenges whose answers it **checks on
its own**, by simulating them again, to measure how well a model does this job.

**No API key?** If you use Claude, ChatGPT or Gemini with your own account, VibeSPICE can
lend its tools to the app you already use: see
[Without an API key](#without-an-api-key-from-your-chat-app).

```
 the model (Claude API, any OpenAI-compatible API or Open WebUI)
        ⇅  it asks for simulations and gets their results
 VibeSPICE (your computer)  ⇄  ngspice
```

---

## Contents

1. [Getting started](#1-getting-started-about-15-minutes): install it and run your first task,
   or [use it from your chat app](#without-an-api-key-from-your-chat-app) without an API key
2. [Using it](#2-using-it): writing tasks, reading the results, what it costs
3. [Other models and APIs](#3-other-models-and-apis): the configuration file
4. [The benchmark](#4-the-benchmark-measuring-a-model): measuring a model
5. [What the model can do](#5-what-the-model-can-do-and-what-it-cant): tools and safety limits
6. [Reference](#6-reference): commands, options, troubleshooting, files
7. [Versions and license](#7-versions-and-license)
8. [Contributing](#8-contributing): trying it, reporting bugs, suggesting ideas

A few words used below:

| Word | What it means here |
|---|---|
| **Terminal** | The window where you type commands. Every command in this README is typed there, followed by Enter |
| **API** | The door through which a program talks to a model that runs elsewhere (on Anthropic's servers, OpenAI's or your own) |
| **API key** | A password for that API, usually a long text that starts with `sk-`. It is personal: whoever has it spends your credit |
| **Provider** | The kind of API you use: `anthropic` (Claude), `openai` (OpenAI and compatible ones) or `openwebui` |
| **Tokens** | How models count text, about ¾ of a word each. Paid APIs charge per token |
| **Run** | One task or challenge from start to finish: several requests to the model and several simulations |

---

## 1. Getting started (about 15 minutes)

VibeSPICE is made for **Linux** and **macOS**, and for **Windows through WSL** (a Linux
inside Windows, see step 1). So far it has been tried on Fedora and Ubuntu, with Python 3.11
to 3.14 and ngspice 42 and 47; on macOS and WSL, not yet. If you try it there, please
[tell us](https://github.com/mauro-hw/vibespice/issues) how it went.

### Step 1. Open a terminal

- **Linux:** look for *Terminal* in your applications menu.
- **macOS:** *Applications → Utilities → Terminal*.
- **Windows:** open *PowerShell* as administrator and type `wsl --install`. Restart the
  computer when it asks, then open *Ubuntu* from the Start menu and choose a user name and a
  password. From then on, type every command in that Ubuntu window and follow the
  instructions for Ubuntu.

### Step 2. Install Python, ngspice and pipx

pipx is the tool that installs VibeSPICE as a command. Type the line for your system:

| System | Command |
|---|---|
| Fedora | `sudo dnf install python3 ngspice pipx` |
| Ubuntu 24.04 or newer, Debian 12 or newer (and WSL) | `sudo apt update && sudo apt install python3 ngspice pipx` |
| macOS, with [Homebrew](https://brew.sh) | `brew install python ngspice pipx` |

`sudo` asks for your computer's password; nothing appears on screen while you type it. Then
type:

```bash
pipx ensurepath
```

and **close the terminal and open a new one**, so that it finds the commands you install.

> VibeSPICE needs Python 3.11 or newer; `python3 --version` tells you which one you have.
> Ubuntu 22.04 comes with 3.10, which is too old.

### Step 3. Install VibeSPICE

```bash
pipx install git+https://github.com/mauro-hw/vibespice
vibespice selftest
```

The second command tests ngspice and VibeSPICE without any model. It should end with
**Self-test passed.**

### Step 4. Get an API key

> **No API key, but a Claude, ChatGPT or Google account?** Skip steps 4 to 7 and go to
> [Without an API key](#without-an-api-key-from-your-chat-app).

The quickest way is the **Claude API**:
1. Create an account in the [Claude Console](https://platform.claude.com).
2. Add some credit (*Billing*). The API is paid per use, and new accounts get a little free
   credit to try it. **A Claude Pro or Max subscription does not include the API**: it is a
   separate account.
3. Create a key (*API keys*) and copy it. It starts with `sk-ant-`.

Do you have another API (OpenAI, OpenRouter) or a model on your own computer (Ollama) or
server (Open WebUI)? You can use it instead: see [section 3](#3-other-models-and-apis).

### Step 5. Tell VibeSPICE which model to use

```bash
vibespice init
```

It asks three things and checks them as you go:
1. **Which API** you use: Claude, OpenAI, OpenRouter, Ollama, another compatible one or
   Open WebUI. For Claude, just press Enter.
2. **Your key.** Paste it: Ctrl+Shift+V in most Linux terminals, Cmd+V on macOS, right
   click on Windows. **Nothing shows on screen while you paste it**, on purpose; press Enter
   afterwards. VibeSPICE tries it straight away and tells you if it is wrong.
3. **The model**, from the list your API offers. For Claude it suggests `claude-opus-5-5`:
   press Enter to take it.

It saves everything in `~/.config/vibespice/config.toml`, a file only you can read, and
offers to check the connection, which is step 6. Run `vibespice init` again whenever you
want to add another model or API.

> **Prefer to edit the file yourself?** `vibespice init --template` writes a commented file
> without asking anything. Open it with a text editor (`nano ~/.config/vibespice/config.toml`;
> save with Ctrl+O and Enter, leave with Ctrl+X) and paste your key between the quotes of
> `api_key = "sk-ant-paste-your-key-here"`. `.config` is a hidden folder: in the file
> manager, Ctrl+H shows it. You can also leave `api_key` empty and put the key in the
> environment: `export ANTHROPIC_API_KEY=sk-ant-…` (add that line to `~/.bashrc` so that it
> stays).

### Step 6. Check the connection

```bash
vibespice check
```

If you said yes at the end of step 5, you have just seen this. It checks ngspice, your key,
the model, a short chat and a tool call, and it should end with **All good.** If it doesn't,
it says what is wrong; see also [troubleshooting](#troubleshooting).

### Step 7. Your first task

```bash
vibespice run "Design a 12 V to 5 V divider with E24 resistors"
```

You see each step as it happens: how long the model took, which simulation it asked for and
the first lines of the result. At the end, its answer and the file where the whole run is
saved. A simple task takes from a few seconds to a few minutes.

**Can't remember the commands?** Type `vibespice` alone: a menu asks what you want to do
(setting up a model or API is one of the options) and shows you the exact command before
running it.

**Updating:** `pipx upgrade vibespice`. **Uninstalling:** `pipx uninstall vibespice`. Your
configuration and your results stay in `~/.config/vibespice` and `~/.local/share/vibespice`;
delete those folders if you want them gone too.

> **Without installing**, from a copy of the code
> (`git clone https://github.com/mauro-hw/vibespice`, then `cd vibespice`), type
> `python3 -m vibespice` wherever this README says `vibespice`.

### Without an API key: from your chat app

Do you use Claude, ChatGPT or Gemini with your own account? Then you don't need an API:
VibeSPICE can lend its tools to the app you already use, and the app's model uses them. You
talk to it in the app as usual, and ngspice still runs on your computer.

```
 your chat app (Claude Desktop, Claude Code, Codex, Gemini CLI) and its model
        ⇅  MCP: it asks for simulations and gets their results
 vibespice mcp (your computer)  ⇄  ngspice
```

1. Do steps 1 to 3: Python, ngspice and VibeSPICE.
2. Type `vibespice mcp`. In a terminal it starts nothing: it shows what to add to each app,
   with the paths of your computer already filled in.
3. Add it to your app, the way it says:

   | App | Account | How |
   |---|---|---|
   | Claude Desktop | Claude | *Settings → Developer → Edit Config*: paste the block it shows, save, quit Claude completely (from the menu bar or the tray, not only the window) and open it again |
   | Claude Code | Claude Pro or Max | `claude mcp add --scope user vibespice -- …/vibespice mcp` |
   | Codex | ChatGPT | `codex mcp add vibespice -- …/vibespice mcp` |
   | Gemini CLI | Google | `gemini mcp add --scope user vibespice …/vibespice mcp` |

4. Open a new chat and ask for a circuit, for example *"Design a 12 V to 5 V divider with
   E24 resistors and check it with VibeSPICE"*. The app may ask your permission the first
   time it uses each tool.

So far it has been tried with Claude Code on Linux. Claude Desktop, Codex and Gemini CLI
work the same way, but nobody has tried them yet: if you do, please [tell us](https://github.com/mauro-hw/vibespice/issues)
how it went, with what you saw if something failed.

On Windows, install VibeSPICE in WSL (step 1) and type `vibespice mcp` there: the block it
shows makes Claude Desktop, on Windows, start VibeSPICE inside WSL.

The web versions (claude.ai, chatgpt.com) only connect to servers on the internet, so they
can't reach `vibespice mcp` on your computer: use one of the apps above.

**What changes compared with `vibespice run`:**
- The app's model runs the loop, with the app's own instructions. VibeSPICE gives it the
  same tools and safety limits (section 5) and the netlist rules, but it doesn't save the
  conversation: the app keeps it. Ask the model for the final netlist to check it yourself.
- There is no automatic verification and no benchmark: `vibespice bench` needs an API, so
  that every model is measured in the same conditions.
- It counts against the usage limits of your plan, not against API credit.
- Some apps stop a tool that takes too long: Codex, after 60 s by default. A big Monte
  Carlo can take that long. To give it more time in Codex, add `tool_timeout_sec = 300`
  under `[mcp_servers.vibespice]` in `~/.codex/config.toml`.

---

## 2. Using it

### Writing a task

```bash
vibespice run "Design a 24 V to 3.3 V divider with the E12 series and give me the worst case with 5 % resistors"
vibespice run --file my_task.txt          # a longer task, written in a text file
```

The model only knows what you write, so give the specification **with numbers**: values,
tolerances, E series, limits on current or power, and what you want back (the design, the
worst case, a table). Some examples:

- *"Design an RC low-pass filter at 1 kHz with E12 resistors and E6 capacitors; error under
  1 %."*
- *"Bias a 2N3904 for Ic = 1 mA from 12 V, stable when β changes from 100 to 400."*
- *"I need 5 V ±2 % from an input of 12 V ±3 % with a divider. Is it possible?"*

If the task cannot be met, a good model says so and explains why instead of forcing an
answer.

### Reading the results

The answer appears at the end of the run. The full story is in a file whose path is printed
on the last line (`Log: …`). Every run is saved in `~/.local/share/vibespice/logs/`; to open
that folder, type `xdg-open ~/.local/share/vibespice/logs` (Linux) or
`open ~/.local/share/vibespice/logs` (macOS).

- **`…_free.md`** (`…_challengeN.md` for the challenges): the whole conversation of one run,
  readable in any text editor. It includes the model's reasoning, every netlist, every result
  and, in challenges, the verification. That is where you see **what it gets wrong**: mental
  arithmetic, `1M` (milli) instead of `1MEG`, the sign of the current, shortcuts without
  simulating…
- **`….json`**: the same run, as raw data.
- **`summary.csv`**: one row per run (provider, model, task or challenge, result, steps,
  time, tokens, version), ready for a spreadsheet.

**Check the numbers of a free task yourself.** VibeSPICE verifies the challenges on its own
(section 4), but it cannot know what a free task should give. Every netlist is in the log,
ready to run again in ngspice.

### What it costs

With a paid API, every step is a request that carries the whole conversation so far. A run
costs from a few cents to a couple of dollars, depending on the model and on how many steps
it takes. With the Claude API, VibeSPICE turns on prompt caching, which makes the repeated
part much cheaper. Each run ends with its token count (`total input … tok (… cached) · total
output … tok`), and `summary.csv` keeps it. To spend less with Claude, use the model
`claude-sonnet-5-5` instead of `claude-opus-5-5` (half the price, see section 3) or lower the
reasoning with `--think low`.

### How much the model reasons

`--think` sets how much the model thinks before answering. With Claude: `low`, `medium`,
`high`, `xhigh` or `max`; `yes`, the default, means `high`. More reasoning usually gives
better designs, but it takes longer and costs more. To watch the reasoning as it happens, add
`--show-thinking` (it is always saved in the log).

```bash
vibespice run --think medium --show-thinking "Design a 1 kHz RC low-pass filter with E12 and E6 values"
```

---

## 3. Other models and APIs

### The configuration file

`~/.config/vibespice/config.toml` has **one profile per model or API**. The easiest way to
add one is `vibespice init`, which asks the questions of step 5 and appends the new profile
without touching the others. You can also edit the file: it has commented examples for every
provider (remove the `#` at the start of the lines of one and fill it in).
`default_profile` says which profile is used, and `--profile NAME` picks another one for a
single command.

```toml
default_profile = "claude"

[profiles.claude]
provider = "anthropic"
api_key = "sk-ant-…"
model = "claude-opus-5-5"

[profiles.local]
provider = "openai"
url = "http://localhost:11434/v1"
model = "qwen3:8b"
```

With that file, `vibespice run --profile local "…"` uses the second one. If you don't know the
exact name of a model, leave `model = ""`: `vibespice check` lists the ones available to you.

| `provider` | For | `url` | `api_key` |
|---|---|---|---|
| `anthropic` | The Claude API | Not needed | From the Claude Console, or `ANTHROPIC_API_KEY` |
| `openai` | OpenAI and any OpenAI-compatible API: OpenRouter, Ollama, vLLM, LM Studio, llama.cpp… | The API's base, usually ending in `/v1` (default: OpenAI's) | Its key, or `OPENAI_API_KEY`; local servers need none |
| `openwebui` | Open WebUI, with its models served by Ollama | Your server | Your Open WebUI key |

The model must be able to **call tools**; most recent models can, and `vibespice check` tells
you. This file is never shared or uploaded: it stays on your computer, and so do your
results. Each person who uses VibeSPICE creates their own.

### Claude API

VibeSPICE keeps the conversation exactly as the API returns it, turns on prompt caching and
sets the reasoning with the effort level (`--think`). Each reply is capped at `max_tokens`
(16000 by default). If a model declines a request, the fallback model Anthropic recommends
continues, on the models that offer it; `fallbacks = false` in the profile turns that off.
Claude Opus 5.5 always reasons, so `--think no` is not accepted: use `--think low` instead.

### OpenAI and compatible APIs (OpenRouter, Ollama, LM Studio…)

VibeSPICE sends the standard chat format. `--think LEVEL` goes as `reasoning_effort`, if the
server accepts it; there is no standard way to turn reasoning off. For the qwen3 families it
also sends the sampling their vendor recommends (temperature, top_p and presence_penalty).

| Service | `url` | Key |
|---|---|---|
| OpenAI | `https://api.openai.com/v1` (the default) | Yes |
| OpenRouter | `https://openrouter.ai/api/v1` | Yes |
| Ollama on your computer | `http://localhost:11434/v1` | No |

### Open WebUI

Two steps on the server:
1. As an admin, go to *Admin Panel → Settings → Authentication* (*User Access* section) and
   turn on **Enable API Keys**. Click *Save* at the bottom of the page. If *API Key Endpoint
   Restrictions* shows up, leave it off: if you turn it on, you will have to allow
   `/api/models`, `/api/chat/completions`, `/ollama/api/ps`, `/ollama/api/version` and
   `/ollama/api/show`.
2. Go to your user, *Settings → Account → API Keys*, and create one. It starts with `sk-`.

An admin key also lets VibeSPICE see which model is loaded and with which context
(`status`, `check`, `--num-ctx auto`) and read each model's reasoning levels. Without one,
everything else still works. VibeSPICE uses the sampling each model family's vendor
recommends (qwen3: 0.6 / 0.95 / 20 with reasoning and 0.7 / 0.8 / 20 without; qwen3.8:
temperature 1.0 with reasoning and 0.7 with `presence_penalty` 1.5 without); for a family it
does not know, those of its Modelfile.

**⚠ If other people share the server:**
- **Context.** If VibeSPICE asked for a different `num_ctx` from the one other users get,
  Ollama would reload the model every time your requests alternate. That is why
  `--num-ctx auto`, the default, copies the one already loaded. Change it only if you know
  nobody else is using the model.
- **Server load.** Every step is a request to the GPU other people use. A challenge takes
  from one to several minutes, and if the server handles one request at a time, others wait
  behind you. Run long batches (`bench all --repeat 5`) off-peak.
- **Parameters set in Open WebUI.** If the model has parameters set in its Open WebUI
  configuration, those values **override** VibeSPICE's.

### Other settings

Any profile can also set `ca` (your CA certificate, for a server with its own HTTPS
certificate), `timeout` (seconds to wait for each reply, 900 by default; when the reply
streams in, the longest pause allowed while it keeps coming), `max_tokens` and `stream`.
With Open WebUI and OpenAI-compatible APIs, replies stream in by default: a long reply is
never cut while it keeps coming, not even by a proxy in front of the server, and the waiting
line shows how much has arrived. `stream = false` asks for each reply in one piece.
Outside the profiles, `logs_dir` moves the results folder and `ngspice` sets the path of the
executable.

**Environment variables** take priority over the file: `VIBESPICE_PROFILE`,
`VIBESPICE_PROVIDER`, `VIBESPICE_URL`, `VIBESPICE_API_KEY`, `VIBESPICE_MODEL`,
`VIBESPICE_CA`, `VIBESPICE_TIMEOUT`, `VIBESPICE_LOGS` and `VIBESPICE_NGSPICE`.
`VIBESPICE_CONFIG` points to another configuration file. With the provider, the model and a
key in the environment, you don't even need the file.

---

## 4. The benchmark: measuring a model

```bash
vibespice bench                 # lists the challenges
vibespice bench 2               # one
vibespice bench 1 6 7           # several
vibespice bench all             # all, one after another
```

| Challenge | What it measures | Reference solution (for you) |
|---|---|---|
| **0** Warm-up | Can it call the simulator and read the result? | Vout = 5.0323 V; I = 0.3871 mA |
| **1** E24 design | Iterating with standard values and constraints | Best 18k / 13k (+0.645 %). 51k / 36k (−0.690 %) and 47k / 33k (−1.000 %) also pass |
| **2** Maximum tolerance | Worst case: analytic and verified by corners | t max = **1.709 %** → commercial **1 %** (with 2 % the worst case is 2.341 %) |
| **3** ADC load (100 k ±10 %) + E96 | Spotting the loading effect and balancing stiffness against consumption | Only 26 E96 pairs pass. Best: 9.76k / 7.5k (1.548 %). The naive 14k / 10k gives −5.5 % from the load alone |
| **4** Monte Carlo versus worst case | Statistics and interpretation | Worst case 2.341 %; yield ≈ 99.99 % (gaussian) and ≈ 97.9 % (uniform) |
| **5** Trick: input ±3 %, output ±2 % | Does it recognize an impossible spec or make something up? | Not feasible: a divider scales with its input. Alternative: a reference or an LDO |
| **6** RC filter at 1 kHz: Ra + Rb (E12) and C (E6) | Three variables of two kinds, `.ac` analysis and `.meas` | 49 designs pass ±1 %, in 19 levels. Best: 4.7k + 120 Ω with 33 nF (+0.060 %). The obvious path (10 nF → 15k + 820) gives +0.604 %. With `vdb = −3` instead of −3.0103, fc comes out −0.24 % off |
| **7** Biasing an NPN (2N3904) that is stable against β | Four variables, a transistor with its `.model` and a trade-off (stability against Vce and consumption); does it simulate or assume Vbe ≈ 0.7 V? | 11,226 designs pass, in 91 levels. Best ≈ −1.01 %: 4.7k / 7.5k / small RC / 6.8k. Textbook designs fail: Ic of 1.1 to 1.35 mA |

The formula for challenge 2, with a = R2/(R1+R2) = 5/12, is

  worst_error = 2·t·(1−a) / (1 + t·(2a−1)) = 2 % → t = 1.7094 %

**How it is scored.** The model ends each challenge with a JSON block. VibeSPICE simulates
what it proposes again and compares it with the specification. It also checks that the
figures the model reports match the simulation. In addition, in challenges 0, 1, 6 and 7 the
results it reports (voltage, current, fc, Ic, Vce) must come from **its own** successful
simulations. The same applies to values computed by comparing two simulations, like the Ic
change in challenge 7: it must match the change of Ic between two of its simulations. If they
don't, the agent sends them back so the model measures them (up to 2 times) and, if they are
still unmeasured, the run fails even if the design is good. At the end you see ✅/❌ per
criterion and a **PASS / FAIL** result.

### Measuring what a model can do

A model's answers vary from one run to the next, so **a single success means nothing**.
Measure pass rates:

```bash
# 1. One run, reading calmly (shows its reasoning)
vibespice bench 1 --show-thinking

# 2. Reliability: 5 repetitions of each challenge, at two reasoning levels
#    (Claude's here; each model has its own, and `check` lists them)
vibespice bench all --repeat 5 --think high
vibespice bench all --repeat 5 --think low

# 3. If it fails with the tools, compare with text mode
vibespice bench 3 --repeat 5 --mode text
```

With a paid API, long batches add up: `bench all --repeat 5` is 40 runs.

**What to look at:**
- **Pass rate per challenge**, at each reasoning level. In these challenges reasoning usually
  pays off, even if it takes longer.
- **Steps and calls.** Many steps in a simple challenge usually mean it is going in circles.
- **"Used no tools".** If it still passes, it solved it mentally. In challenge 5 that is
  correct; in challenge 3 it would be suspicious.
- **Context warnings.** If "possible context truncation" shows up, the conversation does not
  fit in the server's `num_ctx` (your own server only).

`vibespice analyze` summarizes everything saved (with `--challenge`, `--since YYYYMMDD`,
`--last N` or `--md report.md`). To compare before and after a code change, add
`--by-version`.

**Writing your own challenges.** In `vibespice/challenges.py`, copy a `Challenge(...)`,
change the statement and the JSON format, and write its verification function (or `None` if
you don't want one). The helpers `divider_vout()` and `divider_worst_case()` are good examples.

---

## 5. What the model can do (and what it can't)

| Tool | What it does |
|---|---|
| `simulate` | Runs the netlist in ngspice. Returns the DC operating point (always), the quantities of each transistor (Ic, Vbe, Vce, gm and region), the connections of each component in words and the `.meas` results of `.tran`, `.ac` and `.dc` |
| `analyze_tolerances` | Worst case by corners or Monte Carlo (gaussian or uniform) over R, C, L and sources. The output can be `v(out)`, an expression or **the name of a `.meas`**, so it also works for filters (for example, f-3dB) |
| `standard_values` | Gives the nearest E3 to E96 values with their error, so the model doesn't recite them from memory |
| `calculate` | Exact calculator with SPICE suffixes (`12*10k/(14k+10k)`, `parallel(10k,100k)`) |

**Safety limits.** The model writes the netlist and it runs on your computer, so:
- `.control`, `shell`, `.include`, `.lib`, `.osdi` and any directive outside an allow-list
  are rejected.
- Each simulation runs in a temporary folder with a 30 s limit.
- Monte Carlo is capped at 5000 samples.

The limits are the same when a chat app uses the tools through `vibespice mcp`.

**Help for the model.** The tools warn about the usual mistakes: a title line (SPICE ignores
the first line), `M` = milli, a missing ground `0`, a singular matrix, `.step` (it does not
exist in ngspice) and failed `.meas`. `simulate` also says which E series each R, C and L
belongs to (for example, `R1 = 14k: E48, E96`), because models tend to treat non-standard
values as standard. It also sends the circuit back in words
(`R1: node vcc (+ of VCC) – node b (base of Q1)`) and warns about floating nodes, numbered
nodes and, if a BJT is saturated or cut off, that the connections should be checked before
the values, because models often simulate a different circuit without noticing. With that
the model can correct itself, which is exactly what we want to see.

---

## 6. Reference

### Commands

| Command | What it does |
|---|---|
| `run "task"` / `run --file F` | Gives the model a task and lets it simulate until it answers (section 2). `--file -` reads the task from standard input |
| `bench [ID…]` / `bench all` | Runs challenges with verification; without IDs, lists them (section 4) |
| `check` | Checks ngspice, the key, the model, a chat and a tool call (and, with Open WebUI, the loaded context) |
| `status` | Open WebUI only: which model Ollama has loaded, with which context and how much VRAM it uses (admin key) |
| `selftest` | Local test without any model: ngspice, the tools and the verifiers |
| `analyze` | Statistics of the saved runs (section 4) |
| `init` | Asks which API, key and model to use, checks them and saves them; run it again to add another (section 3). `--template` writes a commented file instead, without questions. It never overwrites anything |
| `mcp` | Lends the tools to a chat app over MCP, without an API key (section 1, *Without an API key*). The app starts it on its own; typed in a terminal, it shows how to add it to each app |
| `--version` | Prints the version, the code commit, the license and where the source code is |

Type `vibespice <command> -h` for the options of each one.

### Options of `run` and `bench`

`--repeat` and `--time-limit` are only for `bench`.

| Option | Effect |
|---|---|
| `--think yes/no/level` | How much the model reasons (`yes` by default). With Claude, the effort: `low`, `medium`, `high`, `xhigh` or `max`, and `yes` means `high`; Claude Opus 5.5 always reasons, so `no` is not accepted. With Open WebUI, the model's own levels (qwen3.8: `low`, `medium`, `xhigh`; `yes` is its default level). With an OpenAI-compatible API, the level goes as `reasoning_effort`. `check` shows what each model accepts |
| `--profile NAME` | Uses another profile of the configuration file (section 3). Also for `check` and `status` |
| `--model M` | Uses another model of the same provider (default: `model` in the profile). Before the first batch with a new model, run `check --model M`: it shows which reasoning levels it accepts and whether it calls the tools correctly |
| `--mode native/text` | *native*: tool calls through the API. *text*: the model writes `<tool_call>{…}</tool_call>` in its reply, for models that can't call tools through the API. The agent understands both, and even a "native" call that slips through as text |
| `--repeat N` | Repeats and summarizes the pass rate |
| `--time-limit T` | Maximum batch time (`45m`, `2h`, `1h30`): does not start a run that cannot finish in time and tells you when it will end |
| `--max-steps N` | Cap on model replies per run (20 by default) |
| `--num-ctx auto/N` | Open WebUI only. *auto* (default) uses the context the server already has the model loaded with (section 3) |
| `--show-thinking` | Shows the reasoning on screen (it is always in the log) |

**Following a long batch.** Before it starts, VibeSPICE tells you how long each run usually
takes according to your history and when it will finish. While the model thinks, a line
updates every 5 s (⏳), and after each run you see what is left (⏱). At the end, a desktop
notification. While it works, the computer does not suspend by itself when idle and, with
the charger plugged in, not when closing the lid either. The screen still locks and you can
suspend it by hand; on battery, closing the lid does suspend it. If it suspends, the
`--time-limit` is counted with wall-clock time, so the batch does not run past its end time.

### Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `vibespice: command not found` | Run `pipx ensurepath` and open a new terminal (step 2) |
| "Incomplete configuration" | It says what is missing and in which file. Without a file: `vibespice init` (step 5) |
| Nothing shows when I paste the key | That is on purpose: paste it and press Enter (step 5) |
| "api_key still has the example value" | Paste your key in the configuration file, or run `vibespice init` (step 5) |
| `HTTP 401` | Key copied wrong, regenerated or for another provider (section 3) |
| `HTTP 402` | No credit left: add it in your provider's console (for Claude, the Claude Console). A Claude Pro or Max subscription does not include the API |
| `HTTP 403` | The key has no permission for that model. With Open WebUI: *Enable API Keys* off, *API Key Endpoint Restrictions* on, or a key without admin rights (section 3) |
| `HTTP 404` or "is not listed" | `url` is wrong, or `model` does not match the exact name (`check` lists the available ones) |
| `HTTP 429`, `529` or `503` and "retrying" | Rate limit or busy server: VibeSPICE waits and retries up to 4 times, as long as the server asks. If it still fails, wait a bit or lower the pace (fewer repetitions) |
| "Can't connect" | Wrong URL or port, you are not on the server's network, or a firewall is in the way. Try the same URL in a browser |
| Certificate error (HTTPS) | Put the path of your CA's certificate in `ca`, in your profile |
| "did not call the tool" in `check` | Use `--mode text` |
| "does not support --think …" | That model does not have that reasoning level; the message lists the ones it has |
| "No reply within 900 s" | Very long reasoning or an overloaded server. Raise `timeout` in your profile or lower `--think`. With `stream = false`, a proxy in front of the server may also cut long replies (`HTTP 502` after a few minutes): leave streaming on |
| "The reply stopped coming for 900 s" | The server paused in the middle of a streamed reply: usually overloaded. Raise `timeout` or try later |
| "the reply was cut at max_tokens" | The model needed a longer reply: raise `max_tokens` in the profile |
| "the model declined to answer" | The API's safety classifiers refused the request; the log says the category. Reword the task |
| "possible context truncation" | With your own server: the conversation does not fit in its `num_ctx`. Raise it in the model's configuration (carefully, section 3) |
| `ngspice executable not found` | Install ngspice (step 2) or set its path in `ngspice`, in the configuration file. In a chat app, use the block that `vibespice mcp` shows: it has the full path of ngspice |
| The chat app does not show the VibeSPICE tools | Quit the app completely and open it again. Check that the paths in its configuration are the ones `vibespice mcp` shows; they change if you reinstall Python or VibeSPICE somewhere else |
| A tool "timed out" in a chat app | A long Monte Carlo: ask for fewer samples, or give the app more time (Codex: `tool_timeout_sec`, section 1, *Without an API key*) |
| The self-test fails on some value | Please open an issue with the output: it may be a format change between ngspice versions |

### Files in this repository

| File | What it is for |
|---|---|
| `vibespice/cli.py` | The commands (`run`, `bench`, `check`…) and the guided menu |
| `vibespice/agent.py` | The agent: loop, logs and verification |
| `vibespice/providers/` | How it talks to each API: Claude, OpenAI-compatible and Open WebUI |
| `vibespice/tools.py` | What the model can use: `simulate`, `analyze_tolerances`, `standard_values`, `calculate` |
| `vibespice/mcp.py` | `vibespice mcp`: the same tools for a chat app, over MCP |
| `vibespice/challenges.py` | The challenges, how they are verified and their solutions (the model never sees them) |
| `vibespice/analyze.py` | Statistics of the saved runs: pass rate, times, tools, measurements, unmeasured results and signs that it simulates another circuit, per challenge and configuration |
| `vibespice/config.py` | The configuration file, its profiles and where the results go |
| `vibespice/batch.py`, `checks.py`, `console.py` | Batches and time limits; `check` and `selftest`; terminal output |
| `pyproject.toml` | Package metadata, so it can be installed with pipx or pip |
| `tests/` | Tests without any model: a fake server, with a scripted "model", that speaks the three APIs, and a scripted MCP client |
| `AGENTS.md` | Project rules for contributors and coding agents |
| `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md` | How to contribute, the code of conduct and how to report a security problem |
| `.github/` | Tests on GitHub Actions, and the forms for issues and pull requests |
| `CHANGELOG.md` | What changed in each version |
| `LICENSE`, `NOTICE` | License (AGPL-3.0-only) and attribution notice |

---

## 7. Versions and license

VibeSPICE follows [Semantic Versioning](https://semver.org/), and every change is described in
[CHANGELOG.md](CHANGELOG.md). Each run records in its log the version (`--version`) and the
git commit of the code, with `+changes` if there were uncommitted changes (a copy installed
with pipx from GitHub records the commit it was installed from), so results can always be
traced back to the exact code that produced them.

The entry of each version in the CHANGELOG says what it has been tried with: systems, apps
and real models. `pipx install git+https://github.com/mauro-hw/vibespice` installs the
latest code; to install a given version, add its tag:
`pipx install git+https://github.com/mauro-hw/vibespice@v0.1.0`.

Copyright 2026 Mauro Rodriguez Blasco. VibeSPICE is free software under the GNU Affero
General Public License, version 3 only (AGPL-3.0-only): see [LICENSE](LICENSE) and
[NOTICE](NOTICE).

In short (the license is what counts):
- You can use it for anything, study it, change it and share it.
- If you share it, or a version you changed, you must share its source code under the same
  license. That includes letting others use a changed version over a network, for example
  as a web service (section 13 of the license).
- Keep the copyright and the attribution in `NOTICE` (an additional term under section 7(b))
  and mark the files you changed.
- It comes with no warranty.

---

## 8. Contributing

The most useful help right now needs no programming: try VibeSPICE where nobody has yet
(macOS, WSL, Claude Desktop, Codex, Gemini CLI, models other than the ones in the
changelog) and [tell us how it went](https://github.com/mauro-hw/vibespice/issues/new/choose),
whether it worked or not. Bugs and ideas are welcome too. For now the code is written by
the maintainer, so pull requests from outside the project are not accepted: see
[CONTRIBUTING.md](CONTRIBUTING.md).

Everyone taking part follows the [code of conduct](CODE_OF_CONDUCT.md). Security problems
are reported privately: see [SECURITY.md](SECURITY.md).
