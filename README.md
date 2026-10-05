# vibespice

An agent that designs and simulates analog circuits from a prompt, with a language model
and a real SPICE simulator (ngspice).

You describe what you need, for example *"design a 24 V to 3.3 V divider with E12 resistors
and give me the worst case with 5 % tolerance"*. The model thinks on a cloud API (Claude,
OpenAI, OpenRouter…) or on your own server, and vibespice runs ngspice on your computer. Every time the model asks for a simulation,
vibespice runs it and sends the result back, in a loop, until the model gives its final
answer. Everything is logged: the reasoning, every netlist and every result.

It also includes a **benchmark**: eight challenges whose answers vibespice **checks on its
own**, by simulating again, to measure how well your model does this job.

```
 LLM (Claude API, OpenAI-compatible API or Open WebUI)  ⇄  vibespice (your computer)  ⇄  ngspice
```

It uses only the Python standard library, so there is nothing else to install. It is tested
with Python 3.11 to 3.14 and ngspice 42 and 47, on Linux; it should work on macOS, and on
Windows through WSL.

| File | What it is for |
|---|---|
| `vibespice/cli.py` | The commands (`run`, `bench`, `check`…) and the guided menu |
| `vibespice/agent.py` | The agent: loop, logs and verification |
| `vibespice/providers/` | How it talks to each API: Claude, OpenAI-compatible and Open WebUI |
| `vibespice/tools.py` | What the model can use: `simulate`, `analyze_tolerances`, `standard_values`, `calculate` |
| `vibespice/challenges.py` | The challenges, how they are verified and their solutions (the model never sees them) |
| `vibespice/analyze.py` | Statistics of the saved runs: pass rate, times, tools, measurements, unmeasured results and signs that it simulates another circuit, per challenge and configuration |
| `vibespice/config.py` | The configuration file, its profiles and where the logs go (section 6) |
| `vibespice/batch.py`, `checks.py`, `console.py` | Batches and time limits; `check` and `selftest`; terminal output |
| `pyproject.toml` | Package metadata, so it can be installed with pip or pipx |
| `tests/` | Tests without a server: a fake LLM server, with a scripted "model", that speaks the three APIs |
| `AGENTS.md` | Project rules for contributors and coding agents |
| `CHANGELOG.md` | What changed in each version |
| `LICENSE`, `NOTICE` | License (AGPL-3.0-only) and attribution notice |

---

## 1. Getting started (about 10 minutes)

**a) Requirements.**
- Python 3.11 or newer.
- ngspice: `sudo dnf install ngspice` (Fedora), `sudo apt install ngspice` (Debian, Ubuntu)
  or `brew install ngspice` (macOS).
- pipx, to install vibespice as a command: `sudo dnf install pipx`, `sudo apt install pipx`
  or `brew install pipx`.
- A model that can call tools, reachable through an API. Any of these:
  - **the Claude API**: create a key in the [Claude Console](https://platform.claude.com).
    It is paid per use, separately from a Claude Pro or Max subscription, and new accounts
    get a little free credit to try it;
  - **OpenAI or any OpenAI-compatible service** (OpenRouter, for example), with its key;
  - **a model on your own computer or network**: [Ollama](https://ollama.com) (through its
    OpenAI-compatible API, no key) or [Open WebUI](https://github.com/open-webui/open-webui).

**b) Install and configure.**

```bash
pipx install git+https://github.com/mauro-hw/vibespice
vibespice init                             # creates ~/.config/vibespice/config.toml
nano ~/.config/vibespice/config.toml       # paste your key
```

The file comes ready for the Claude API: paste your key (or leave `api_key` empty and set
`ANTHROPIC_API_KEY`). It also has commented examples for OpenAI, OpenRouter, Ollama and Open
WebUI; section 6 explains each one. If you don't know the exact model id, leave `model`
empty: step d lists the models available to you. To update vibespice later:
`pipx upgrade vibespice`.

> **Without installing.** From a clone (`git clone https://github.com/mauro-hw/vibespice`
> and `cd vibespice`), type `python3 -m vibespice` wherever this README says
> `vibespice`.

**c) Local test, without AI.** Checks your ngspice and the verifiers:

```bash
vibespice selftest
```

**Can't remember the commands?** Run `vibespice` with nothing else: a guided menu
asks what it needs and shows you the exact command before running it.

**d) Connection test.** Checks ngspice, the key, the model, a chat and a tool call:

```bash
vibespice check
```

If step 5 says that native mode works, you are ready. If not, use `--mode text` (see
section 5).

**e) First task and first challenge:**

```bash
vibespice run "Design a 12 V to 5 V divider with E24 resistors"
vibespice bench 0
```

> **What it costs with a paid API.** Every step is a request that carries the whole
> conversation so far, so a run costs from a few cents to a couple of dollars, depending on
> the model and on how many steps it takes. With the Claude API, vibespice turns on prompt
> caching, which makes the repeated part much cheaper. Each run ends with its token count,
> and `summary.csv` keeps it for every run (section 2).

---

## 2. Running a task

```bash
vibespice run "Design a 24 V to 3.3 V divider with the E12 series and give me the worst case with 5 % resistors"
vibespice run --file my_task.txt
vibespice run --file - < my_task.txt      # from standard input
```

You see each step as it happens: how long the model took, which tool it asked for and the
first lines of the result. At the end, its final answer. Free tasks have **no automatic
verification**: check the numbers in the log, where every netlist is ready to run again.

**Writing good tasks.** Give the specification with numbers: values, tolerances, E series,
limits on current or power, and what you want back (the design, the worst case, a table).
The model only knows what you write. If the task cannot be met, a good model says so instead
of forcing an answer.

**Logs.** Every run, task or challenge, goes to the logs folder,
`~/.local/share/vibespice/logs/` (section 6):
- **`YYYYMMDD-HHMMSS_free.md`** (or `…_challengeN.md`): the full conversation of **one** run
  (with `--repeat`, one per repetition: `…_rep1.md`, `…_rep2.md`…). It includes the
  reasoning, every netlist, every result and, in challenges, the verification. That is where
  you see **what it gets wrong**: mental arithmetic, `1M` (milli) instead of `1MEG`, the sign
  of the current, shortcuts without simulating…
- **`.json`**: the raw data of the same run.
- **`summary.csv`**: one row per run (provider, model, task or challenge, PASS/FAIL, steps,
  time, tokens, version and code commit).

---

## 3. The benchmark: challenges with verification

```bash
vibespice bench                 # lists them
vibespice bench 2               # one
vibespice bench 1 6 7           # several
vibespice bench all             # all in a row
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

**How it is scored.** The model ends each challenge with a JSON block. The script simulates
what it proposes again and compares it with the specification. It also checks that the
figures the model reports match the simulation. In addition, in challenges 0, 1, 6 and 7 the
results it reports (voltage, current, fc, Ic, Vce) must come from **its own** successful
simulations. The same applies to values computed by comparing two simulations, like the Ic
change in challenge 7: it must match the change of Ic between two of its simulations. If they
don't, the agent sends them back so the model measures them (up to 2 times) and, if they are
still unmeasured, the run fails even if the design is good. At the end you see ✅/❌ per
criterion and a **PASS / FAIL** result.

### Measuring what a model can do

LLM answers vary from one run to the next, so **a single success means nothing**. Measure
pass rates:

```bash
# 1. One run, reading calmly (shows its reasoning)
vibespice bench 1 --show-thinking

# 2. Reliability: 5 repetitions of each challenge, with and without reasoning
vibespice bench all --repeat 5 --think yes
vibespice bench all --repeat 5 --think no

# 3. If it fails with the tools, compare with text mode
vibespice bench 3 --repeat 5 --mode text
```

**What to look at:**
- **Pass rate per challenge**, with and without reasoning. In these challenges reasoning
  usually pays off, even if it takes longer.
- **Steps and calls.** Many steps in a simple challenge usually mean it is going in circles.
- **"Used no tools".** If it still passes, it solved it mentally. In challenge 5 that is
  correct; in challenge 3 it would be suspicious.
- **Context warnings.** If "possible context truncation" shows up, the conversation does not
  fit in the server's `num_ctx`.

`vibespice analyze` summarizes everything saved (with `--challenge`,
`--since YYYYMMDD`, `--last N` or `--md report.md`). To compare before and after a code
change, add `--by-version`.

**Writing your own challenges.** In `vibespice/challenges.py`, copy a `Challenge(...)`,
change the statement and the JSON format, and write its verification function (or `None` if
you don't want one). The helpers `divider_vout()` and `divider_worst_case()` are good examples.

---

## 4. What the model can do here (and what it can't)

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

## 5. Commands and options

| Command | What it does |
|---|---|
| `run "task"` / `run --file F` | Gives the model a task and lets it simulate until it answers (section 2) |
| `bench [ID…] / bench all` | Runs challenges with verification; without IDs, lists them (section 3) |
| `check` | Checks ngspice, the key, the model, a chat and a tool call (and, with Open WebUI, the loaded context) |
| `status` | Open WebUI only: which model Ollama has loaded, with which context and how much VRAM it uses (admin key) |
| `selftest` | Local test without AI: ngspice, the tools and the verifiers |
| `analyze` | Statistics of the saved runs (section 3) |
| `init` | Creates the configuration file; it never overwrites it (section 6) |
| `--version` | Prints the version |

Run `vibespice <command> -h` for the options of each one.

**Following a long batch.** Before it starts, it tells you how long each iteration usually
takes according to your history and when it will finish. While the model thinks, a line
updates every 5 s (⏳), and after each iteration you see what is left (⏱). At the end, a
desktop notification. While it works, the computer does not suspend by itself when idle and,
with the charger plugged in, not when closing the lid either. The screen still locks and you
can suspend it by hand; on battery, closing the lid does suspend it. If it suspends, the
`--time-limit` is counted with wall-clock time, so the batch does not run past its end time.

Options of `run` and `bench` (`--repeat` and `--time-limit` are only for `bench`):

| Option | Effect |
|---|---|
| `--think yes/no/level` | How much the model reasons (`yes` by default). With Claude, the level is the effort: `low`, `medium`, `high`, `xhigh` or `max`, and `yes` means `high`; Claude Opus 5.5 always reasons, so `no` is not accepted. With Open WebUI, the model's own levels (qwen3.8: `low`, `medium`, `xhigh`; `yes` is its default level). With an OpenAI-compatible API, the level goes as `reasoning_effort`. `check` shows what each model accepts (section 6) |
| `--profile NAME` | Uses another profile of the configuration file (section 6). Also for `check` and `status` |
| `--model M` | Uses another model of the same provider (default: `model` in the profile). Before the first batch with a new model, run `check --model M`: it shows which reasoning levels it supports and whether it calls the tools correctly |
| `--mode native/text` | *native*: tool calls through the API (`tool_calls`). *text*: the model writes `<tool_call>{…}</tool_call>` in its reply. The agent understands both formats, and even a "native" call that slips through as text |
| `--repeat N` | Repeats and summarizes the pass rate |
| `--time-limit T` | Maximum batch time (`45m`, `2h`, `1h30`): does not start an iteration that cannot finish in time and tells you when it will end |
| `--max-steps N` | Cap on model replies (20 by default) |
| `--num-ctx auto/N` | Open WebUI only. *auto* (default) uses the context the server already has the model loaded with. See the note below |
| `--show-thinking` | Shows the reasoning on screen (it is always in the log) |

**⚠ Sharing your own server with other people** (Open WebUI or Ollama):
- **Context.** If the agent asked for a different `num_ctx` from the one other users get,
  Ollama would reload the model every time your requests alternate. That is why `auto` copies
  the one already loaded. Change `--num-ctx` only if you know nobody else is using the model.
- **Server load.** Every step is a request to the same GPU other people use. A challenge
  takes from one to several minutes. If the server handles requests one at a time, others
  will wait in line behind the agent. Long batches (`bench all --repeat 5`) are best run
  off-peak.
- **Parameters set in Open WebUI.** If the model has parameters set in its Open WebUI
  configuration, those values **override** the script's.

---

## 6. Configuration, providers and where things are saved

Nothing is saved in the code folder, so updating vibespice never touches your settings or
your logs, and a key can never end up in a commit.

| What | Where |
|---|---|
| Configuration, with your keys | `~/.config/vibespice/config.toml` (`vibespice init` creates it, readable only by you) |
| Logs: one `.md` and one `.json` per run, and `summary.csv` | `~/.local/share/vibespice/logs/` |

The file has **one profile per server or API**, and `default_profile` says which one is used
when you don't pass `--profile`:

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

`vibespice run --profile local "…"` uses the second one. The providers:

| `provider` | For | `url` | `api_key` |
|---|---|---|---|
| `anthropic` | The Claude API | Not needed | From the Claude Console, or `ANTHROPIC_API_KEY` |
| `openai` | OpenAI and any OpenAI-compatible API: OpenRouter, Ollama, vLLM, LM Studio, llama.cpp… | The API's base, usually ending in `/v1` (default: OpenAI's) | Its key, or `OPENAI_API_KEY`; local servers need none |
| `openwebui` | Open WebUI, with its models served by Ollama | Your server | Your Open WebUI key |

**Claude API.** vibespice keeps the conversation exactly as the API returns it, turns on
prompt caching and sets the reasoning with the effort level (`--think`). Each reply is capped
at `max_tokens` (16000 by default). If a model declines a request, the fallback model
Anthropic recommends continues, on the models that offer it; `fallbacks = false` in the
profile turns that off.

**OpenAI-compatible APIs.** vibespice sends the standard chat format. `--think LEVEL` goes as
`reasoning_effort`, if the server accepts it. For the qwen3 families it also sends the
sampling their vendor recommends (temperature, top_p and presence_penalty).

**Open WebUI.** Two steps on the server:
1. As an admin, go to *Admin Panel → Settings → Authentication* (*User Access* section) and
   turn on **Enable API Keys**. Click *Save* at the bottom of the page. If *API Key Endpoint
   Restrictions* shows up, leave it off: if you turn it on, you will have to allow
   `/api/models`, `/api/chat/completions`, `/ollama/api/ps`, `/ollama/api/version` and
   `/ollama/api/show`.
2. Go to your user, *Settings → Account → API Keys*, and create one. It starts with `sk-`.

An admin key also lets vibespice see which model is loaded and with which context
(`status`, `check`, `--num-ctx auto`) and read each model's reasoning levels. Without one,
everything else still works. With Open WebUI, vibespice uses the sampling each model family's
vendor recommends (qwen3: 0.6 / 0.95 / 20 with reasoning and 0.7 / 0.8 / 20 without;
qwen3.8: temperature 1.0 with reasoning and 0.7 with `presence_penalty` 1.5 without); for a
family it does not know, those of its Modelfile.

**Other settings.** Any profile can set `ca` (your CA certificate, for a server with its own
HTTPS certificate), `timeout` (seconds to wait for each reply, 900 by default) and
`max_tokens`. Outside the profiles, `logs_dir` moves the logs and `ngspice` sets the path of
the executable.

**Environment variables** take priority over the file: `VIBESPICE_PROFILE`,
`VIBESPICE_PROVIDER`, `VIBESPICE_URL`, `VIBESPICE_API_KEY`, `VIBESPICE_MODEL`,
`VIBESPICE_CA`, `VIBESPICE_TIMEOUT`, `VIBESPICE_LOGS` and `VIBESPICE_NGSPICE`.
`VIBESPICE_CONFIG` points to another configuration file. With the provider, the model and a
key in the environment you don't even need the file.

---

## 7. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `HTTP 401` | Key copied wrong, regenerated or for another provider (section 6) |
| `HTTP 402` | No credit left: add it in your provider's console (for Claude, the Claude Console). A Claude Pro or Max subscription does not include the API |
| `HTTP 403` | The key has no permission for that model. With Open WebUI: *Enable API Keys* off, *API Key Endpoint Restrictions* on, or a key without admin rights (section 6) |
| "Incomplete configuration" | It says what is missing and in which file. Without a file: `vibespice init` (section 1b) |
| `HTTP 404` or "is not listed" | `url` is wrong, or `model` does not match the exact id (`check` lists the available ones) |
| `HTTP 429`, `529` or `503` and "retrying" | Rate limit or busy server: vibespice waits and retries up to 4 times, as long as the server asks. If it still fails, wait a bit or lower the pace (fewer repetitions) |
| "Can't connect" | Wrong IP or port, you are not on the server's network or a firewall is in the way. Try the same URL in a browser |
| Certificate error (HTTPS) | Put the path of your CA's certificate in `ca`, in your profile |
| "did not call the tool" in `check` | Use `--mode text` |
| "No reply within 900 s" | Very long reasoning or an overloaded server. Raise `timeout` in your profile or use `--think no` |
| "the reply was cut at max_tokens" | The model needed a longer reply: raise `max_tokens` in the profile |
| "the model declined to answer" | The API's safety classifiers refused the request; the log says the category. Reword the task |
| "possible context truncation" | With your own server: the conversation does not fit in its `num_ctx`. Raise it in the model's configuration (carefully, see section 5) |
| `ngspice executable not found` | Install ngspice (section 1a) or set its path in `ngspice`, in the configuration file |
| "does not support --think …" | That model does not have that reasoning level; the message lists the ones it has |
| The self-test fails on some value | Please open an issue with the output: it may be a format change between ngspice versions |

---

## 8. Versions

vibespice follows [Semantic Versioning](https://semver.org/). Every change is described in
[CHANGELOG.md](CHANGELOG.md). Each run records in its log the version (`--version`) and the
git commit of the code, with `+changes` if there were uncommitted changes (a copy installed
with pipx from GitHub records the commit it was installed from), so results can always be
traced back to the exact code that produced them.

---

## License and authorship

Copyright 2026 Mauro Rodriguez Blasco. vibespice is free software under the GNU Affero
General Public License, version 3 only (AGPL-3.0-only): see [LICENSE](LICENSE) and
[NOTICE](NOTICE).

In short (the license is what counts):
- You can use it for anything, study it, change it and share it.
- If you share it, or a version you changed, you must share its source code under the same
  license. That includes letting others use a changed version over a network, for example
  as a web service (section 13).
- Keep the copyright and the attribution in `NOTICE` (an additional term under section 7(b))
  and mark the files you changed.
- It comes with no warranty.
