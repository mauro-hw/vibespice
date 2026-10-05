# vibespice

An iterative simulation agent that measures how well a language model designs analog
circuits with a real SPICE simulator (ngspice).

The model thinks on your LLM server and this script runs ngspice on your computer. Every time
the model asks for a simulation, the script runs it and sends the result back, in a loop,
until the model gives its final answer. Then the script **checks that answer on its own**,
by simulating again, and logs everything.

```
 LLM (server)  ⇄  Open WebUI (API)  ⇄  spice_agent.py (your computer)  ⇄  ngspice
```

It uses only the Python standard library, so there is nothing else to install. It is tested
with Python 3.11 to 3.14 and ngspice 42 and 47, on Linux.

| File | What it is for |
|---|---|
| `spice_agent.py` | The agent: connection, loop, logs and verification |
| `spice_tools.py` | What the model can use: `simulate`, `analyze_tolerances`, `standard_values`, `calculate` |
| `challenges.py` | The challenges, how they are verified and their solutions (the model never sees them) |
| `agent.conf.example` | Configuration template (your `agent.conf` is never committed) |
| `analyze_logs.py` | Statistics of `logs/`: pass rate, times, tools, measurements, unmeasured results and signs that it simulates another circuit, per challenge and configuration |
| `tests/` | Tests without a server: a fake Open WebUI with a scripted "model" |
| `AGENTS.md` | Project rules for contributors and coding agents |
| `CHANGELOG.md` | What changed in each version |
| `LICENSE`, `NOTICE` | License (Apache-2.0) and attribution notice |

---

## 1. Getting started (about 10 minutes)

**a) Requirements.**
- Python 3.11 or newer.
- ngspice: `sudo dnf install ngspice` (Fedora), `sudo apt install ngspice` (Debian, Ubuntu)
  or `brew install ngspice` (macOS).
- An [Open WebUI](https://github.com/open-webui/open-webui) server with a model served by
  Ollama that supports tool calling. For now the agent talks to Open WebUI only.

**b) Turn on API keys in Open WebUI.** As an admin, go to *Admin Panel → Settings →
Authentication* (*User Access* section) and turn on **Enable API Keys**. Click *Save* at the
bottom of the page. If *API Key Endpoint Restrictions* shows up, leave it off: if you turn it
on, you will have to allow `/api/models`, `/api/chat/completions`, `/ollama/api/ps`,
`/ollama/api/version` and `/ollama/api/show`.

**c) Create your key.** Go to your user, *Settings → Account → API Keys*, and create one. It
starts with `sk-`.

> An admin key lets the agent see which model is loaded and with which context (`--status`,
> `--check`) and check the reasoning levels of each model. Without one, everything else still
> works. The key acts on your behalf, so do not share it.

**d) Download and configure.**

```bash
git clone https://github.com/mauro-hw/vibespice-app
cd vibespice-app
cp agent.conf.example agent.conf
chmod 600 agent.conf
nano agent.conf           # fill in OWUI_URL and OWUI_API_KEY
```

Environment variables with the same names (`OWUI_URL`, `OWUI_API_KEY`, `OWUI_MODEL`…) take
priority over the file.

**e) Local test, without AI.** Checks your ngspice and the verifiers:

```bash
python3 spice_agent.py --selftest
```

**Can't remember the commands?** Run `python3 spice_agent.py` with nothing else: a guided menu
asks what it needs and shows you the exact command before running it.

**f) Connection test.** Checks ngspice, the key, the model, the loaded context, a chat and a
tool call:

```bash
python3 spice_agent.py --check
```

If step 5 says that native mode works, you are ready. If not, use `--mode text` (see
section 5).

**g) First challenge:**

```bash
python3 spice_agent.py --challenge 0
```

---

## 2. The challenges

```bash
python3 spice_agent.py --list
python3 spice_agent.py --challenge 2             # one
python3 spice_agent.py --challenge all           # all in a row
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

---

## 3. Measuring what a model can do

LLM answers vary from one run to the next, so **a single success means nothing**. Measure
pass rates:

```bash
# 1. One run, reading calmly (shows its reasoning)
python3 spice_agent.py --challenge 1 --show-thinking

# 2. Reliability: 5 repetitions of each challenge, with and without reasoning
python3 spice_agent.py --challenge all --repeat 5 --think yes
python3 spice_agent.py --challenge all --repeat 5 --think no

# 3. If it fails with the tools, compare with text mode
python3 spice_agent.py --challenge 3 --repeat 5 --mode text

# 4. Free tasks (no automatic verification)
python3 spice_agent.py --task "Design a 24 V to 3.3 V divider with the E12 series and give me the worst case with 5 % resistors"
python3 spice_agent.py --task-file my_task.txt
```

Everything goes to `logs/`:
- **`summary.csv`**: one row per run (challenge, PASS/FAIL, steps, time, tokens, version and
  code commit).
- **`YYYYMMDD-HHMMSS_challengeN.md`**: the full conversation of **one** run (with `--repeat`,
  one per repetition: `…_challengeN_rep1.md`, `…_rep2.md`…). It includes the reasoning, every
  netlist, every result and the verification. That is where you see **what it gets wrong**:
  mental arithmetic, `1M` (milli) instead of `1MEG`, the sign of the current, shortcuts without
  simulating…
- **`.json`**: the raw data of the same run.

**What to look at:**
- **Pass rate per challenge**, with and without reasoning. In these challenges reasoning
  usually pays off, even if it takes longer.
- **Steps and calls.** Many steps in a simple challenge usually mean it is going in circles.
- **"Used no tools".** If it still passes, it solved it mentally. In challenge 5 that is
  correct; in challenge 3 it would be suspicious.
- **Context warnings.** If "possible context truncation" shows up, the conversation does not
  fit in the server's `num_ctx`.

`python3 analyze_logs.py` summarizes everything saved (with `--challenge`, `--since YYYYMMDD`,
`--last N` or `--md report.md`). To compare before and after a code change, add
`--by-version`.

**Writing your own challenges.** In `challenges.py`, copy a `Challenge(...)`, change the
statement and the JSON format, and write its verification function (or `None` if you don't
want one). The helpers `divider_vout()` and `divider_worst_case()` are good examples.

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

## 5. Options

**Following a long batch.** Before it starts, it tells you how long each iteration usually
takes according to your history and when it will finish. While the model thinks, a line
updates every 5 s (⏳), and after each iteration you see what is left (⏱). At the end, a
desktop notification. While it works, the computer does not suspend by itself when idle and,
with the charger plugged in, not when closing the lid either. The screen still locks and you
can suspend it by hand; on battery, closing the lid does suspend it. If it suspends, the
`--time-limit` is counted with wall-clock time, so the batch does not run past its end time.

| Option | Effect |
|---|---|
| `--think yes/no/level` | Turns reasoning on or off (on by default). Models with levels also accept the level: qwen3.8 accepts `low`, `medium` and `xhigh`, and with `yes` it uses its default level (`medium`). The agent checks on the server what each model supports. It uses the sampling parameters each family's vendor recommends (qwen3: 0.6 / 0.95 / 20 with reasoning and 0.7 / 0.8 / 20 without; qwen3.8: temperature 1.0 with reasoning and 0.7 with `presence_penalty` 1.5 without); for a family it does not know, those of its Modelfile |
| `--model M` | Uses another Open WebUI model or preset (default: `OWUI_MODEL`). Before the first batch with a new model, run `--check --model M`: it shows which reasoning levels it supports, whether it calls the tools correctly and with which context the server loads it |
| `--mode native/text` | *native*: tool calls through the API (`tool_calls`). *text*: the model writes `<tool_call>{…}</tool_call>` in its reply. The agent understands both formats, and even a "native" call that slips through as text |
| `--repeat N` | Repeats and summarizes the pass rate |
| `--time-limit T` | Maximum batch time (`45m`, `2h`, `1h30`): does not start an iteration that cannot finish in time and tells you when it will end |
| `--max-steps N` | Cap on model replies (20 by default) |
| `--num-ctx auto/N` | *auto* (default) uses the context the server already has the model loaded with. See the note below |
| `--show-thinking` | Shows the reasoning on screen (it is always in the log) |
| `--status` | Which model Ollama has loaded, with which context and how much VRAM it uses (admin key) |
| `--version` | Prints the version |

**⚠ Sharing a server with other people:**
- **Context.** If the agent asked for a different `num_ctx` from the one other users get,
  Ollama would reload the model every time your requests alternate. That is why `auto` copies
  the one already loaded. Change `--num-ctx` only if you know nobody else is using the model.
- **Server load.** Every step is a request to the same GPU other people use. A challenge
  takes from one to several minutes. If the server handles requests one at a time, others
  will wait in line behind the agent. Long batches (`--challenge all --repeat 5`) are best run
  off-peak.
- **Parameters set in Open WebUI.** If the model has parameters set in its Open WebUI
  configuration, those values **override** the script's.

---

## 6. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `HTTP 401` | Key copied wrong or regenerated (section 1c) |
| `HTTP 403` | *Enable API Keys* off, *API Key Endpoint Restrictions* on, or a key without admin rights (section 1b) |
| `HTTP 404` or "is not listed" | `OWUI_URL` is wrong, or `OWUI_MODEL` does not match the exact id (`--check` lists the available ones) |
| "Can't connect" | Wrong IP or port, you are not on the server's network or a firewall is in the way. Try the same URL in a browser |
| Certificate error (HTTPS) | Put your CA's certificate in `OWUI_CA` |
| "did not call the tool" in `--check` | Use `--mode text` |
| "No reply within 900 s" | Very long reasoning or an overloaded server. Raise `LLM_TIMEOUT` or use `--think no` |
| "possible context truncation" | The conversation does not fit in the server's `num_ctx`. Raise it in the model's configuration (carefully, see section 5) |
| `ngspice executable not found` | Install ngspice (section 1a) or set its path in `NGSPICE` |
| The self-test fails on some value | Please open an issue with the output: it may be a format change between ngspice versions |

---

## 7. Versions

vibespice follows [Semantic Versioning](https://semver.org/). Every change is described in
[CHANGELOG.md](CHANGELOG.md). Each run records in its log the version (`--version`) and the
git commit of the code, with `+changes` if there were uncommitted changes, so results can
always be traced back to the exact code that produced them.

---

## License and authorship

Copyright 2026 Mauro Rodriguez Blasco. Licensed under the Apache License 2.0: see
[LICENSE](LICENSE) and [NOTICE](NOTICE).

If you redistribute this code or a work based on it, the license asks you to include the
license, keep the copyright and attribution notices, carry the contents of `NOTICE` and mark
the files you changed (section 4). The license does not grant permission to use the
project's name, except to describe where the code comes from (section 6).
