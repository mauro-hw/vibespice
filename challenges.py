"""
Progressive challenges to measure a model with ngspice.

Each challenge has:
  - statement: what the model receives (it never sees the solution)
  - format:    the JSON block it must return at the end
  - verify:    independent check (re-simulates with ngspice whatever it proposes)
  - solution:  reference for you (the model never sees it)

To add your own challenges, copy one and change it. If you don't want automatic
verification, set verify=None.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import spice_tools as hs

Result = list[tuple[bool, str]]   # [(correct, explanation), ...]


@dataclass
class Challenge:
    id: str
    title: str
    statement: str
    format: str
    verify: Callable[[dict], Result] | None
    solution: str = ""
    tags: list[str] = field(default_factory=list)
    # JSON fields that are simulation results (and their scale to SI units): they must come
    # from simulations the model itself ran, not be made up
    measured: dict[str, float] = field(default_factory=dict)
    # Fields that are the % change of a quantity between two of its simulations, and which
    # quantity: 'ic' (the Ic of a transistor), 'v(out)', the name of a .meas...
    derived: dict[str, str] = field(default_factory=dict)

    def message(self, extra: str = "") -> str:
        return (f"{self.statement.strip()}\n\n"
                + (f"{extra.strip()}\n\n" if extra else "")
                + "When you finish, write a short explanation and, at the end, a ```json "
                "block with exactly these fields (numeric values without units, unless text "
                f"is requested):\n```json\n{self.format.strip()}\n```")


# ---------------------------------------------------------------------------
# Verification helpers
# ---------------------------------------------------------------------------
def num(d: dict, key: str) -> float:
    if key not in d:
        raise KeyError(f"missing field '{key}'")
    v = d[key]
    if isinstance(v, str):
        v = v.replace("%", "").replace("Ω", "").replace("ohm", "").strip()
    return hs.spice_value(v)


def boolean(d: dict, key: str) -> bool:
    if key not in d:
        raise KeyError(f"missing field '{key}'")
    v = d[key]
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "yes", "1"):
        return True
    if s in ("false", "no", "0"):
        return False
    raise ValueError(f"'{key}' should be true or false, not {v!r}")


def divider_vout(r1: float, r2: float, vin: float = 12.0, rl: float | None = None) -> float:
    """Simulates the divider with ngspice (independent verification)."""
    net = f"V1 in 0 DC {vin:.12g}\nR1 in out {r1:.12g}\nR2 out 0 {r2:.12g}\n"
    if rl:
        net += f"RL out 0 {rl:.12g}\n"
    d = hs.simulate_data(net + ".op")
    return d["op"]["v(out)"]


def divider_worst_case(r1, r2, tol_r, rl=None, tol_rl=0.0, vin=12.0, target=5.0):
    """Worst error in % by corners (with ngspice)."""
    net = f"V1 in 0 DC {vin:.12g}\nR1 in out {r1:.12g}\nR2 out 0 {r2:.12g}\n"
    tols = {"R1": tol_r * 100, "R2": tol_r * 100}
    if rl:
        net += f"RL out 0 {rl:.12g}\n"
        tols["RL"] = tol_rl * 100
    r = hs.tolerance_data(net + ".op", tols, "v(out)", "corners", target_value=target)
    return r["worst_error_pct"], r


def rc_cutoff(ra: float, rb: float, c: float) -> float | None:
    """Cutoff frequency (|H| = 1/√2) of Ra + Rb in series and C to ground, with ngspice."""
    net = (f"V1 in 0 DC 0 AC 1\nRa in m {ra:.12g}\nRb m out {rb:.12g}\nC1 out 0 {c:.12g}\n"
           ".ac dec 100 0.1 10meg\n.meas ac fc WHEN vdb(out)=-3.0103")
    return hs.simulate_data(net)["meas"].get("fc")


def in_range(v: float, series: str, lo: float, hi: float) -> bool:
    return hs.in_series(v, series) and lo * (1 - 1e-9) <= v <= hi * (1 + 1e-9)


def _wrap(f: Callable[[dict], Result]) -> Callable[[dict], Result]:
    """Turns format errors into a readable failure."""
    def g(d: dict) -> Result:
        try:
            return f(d)
        except (KeyError, ValueError, TypeError) as e:
            return [(False, f"Incomplete or malformed JSON answer: {e}")]
    return g


# ---------------------------------------------------------------------------
# Level 0 · Warm-up: can it use the tool?
# ---------------------------------------------------------------------------
@_wrap
def _v0(d: dict) -> Result:
    v, i = num(d, "vout_V"), abs(num(d, "current_mA"))
    return [
        (abs(v - 5.032258) <= 0.005, f"Output voltage: {v:.5g} V (correct: 5.0323 V)"),
        (abs(i - 0.387097) <= 0.002, f"Source current: {i:.5g} mA (correct: 0.3871 mA)"),
    ]


# ---------------------------------------------------------------------------
# Level 1 · Design with E24 values
# ---------------------------------------------------------------------------
@_wrap
def _v1(d: dict) -> Result:
    r1, r2 = num(d, "R1_ohm"), num(d, "R2_ohm")
    out: Result = []
    out.append((hs.in_series(r1, "E24") and hs.in_series(r2, "E24"),
                f"R1 = {hs.fmt_spice(r1)}, R2 = {hs.fmt_spice(r2)} belong to the E24 series"))
    i_ma = 12 / (r1 + r2) * 1000
    out.append((0.1 - 1e-9 <= i_ma <= 1 + 1e-9,
                f"Divider current {i_ma:.4g} mA (must be between 0.1 and 1 mA)"))
    v = divider_vout(r1, r2)
    e = (v - 5) / 5 * 100
    extra = " — the best possible!" if abs(e) <= 0.6452 + 1e-3 else \
        " (0.645 % was possible)"
    out.append((abs(e) <= 1.0 + 1e-6, f"Simulated nominal error {e:+.3f} % (limit ±1 %){extra}"))
    if "vout_V" in d:
        vr = num(d, "vout_V")
        out.append((abs(vr - v) <= 0.005,
                    f"The voltage it reports ({vr:.5g} V) matches the simulated one ({v:.5g} V)"))
    return out


# ---------------------------------------------------------------------------
# Level 2 · Maximum tolerance
# ---------------------------------------------------------------------------
_T_MAX = 1.709402   # exact %: 2t(1-a)/(1+t(2a-1)) = 0.02 with a = 10/24

@_wrap
def _v2(d: dict) -> Result:
    t, tc = num(d, "max_tolerance_pct"), num(d, "commercial_tolerance_pct")
    return [
        (abs(t - _T_MAX) <= 0.02, f"Maximum tolerance {t:.4g} % (correct: {_T_MAX:.3f} %)"),
        (abs(tc - 1.0) < 1e-9, f"Chosen commercial tolerance {tc:g} % (correct: 1 %; "
                               "with 2 % the worst case reaches 2.34 %)"),
    ]


# ---------------------------------------------------------------------------
# Level 3 · Load (ADC) + tolerances, E96 values
# ---------------------------------------------------------------------------
@_wrap
def _v3(d: dict) -> Result:
    r1, r2 = num(d, "R1_ohm"), num(d, "R2_ohm")
    out: Result = []
    out.append((hs.in_series(r1, "E96") and hs.in_series(r2, "E96"),
                f"R1 = {hs.fmt_spice(r1)}, R2 = {hs.fmt_spice(r2)} belong to the E96 series"))
    rpar = r2 * 100e3 / (r2 + 100e3)
    i_ma = 12 / (r1 + rpar) * 1000
    out.append((0.1 - 1e-9 <= i_ma <= 1 + 1e-9,
                f"Source current {i_ma:.4g} mA (between 0.1 and 1 mA)"))
    wc, _ = divider_worst_case(r1, r2, 0.01, rl=100e3, tol_rl=0.10)
    out.append((wc <= 2.0 + 1e-6, f"Simulated worst case {wc:.3f} % (limit 2 %; the best "
                                  "possible with E96 is 1.548 %)"))
    if "worst_error_pct" in d:
        wr = abs(num(d, "worst_error_pct"))
        out.append((abs(wr - wc) <= 0.05,
                    f"The worst case it reports ({wr:.3f} %) matches the simulated one "
                    f"({wc:.3f} %)"))
    return out


# ---------------------------------------------------------------------------
# Level 4 · Monte Carlo versus worst case
# ---------------------------------------------------------------------------
@_wrap
def _v4(d: dict) -> Result:
    wc = abs(num(d, "worst_error_pct"))
    g, u = num(d, "yield_gaussian_pct"), num(d, "yield_uniform_pct")
    return [
        (2.30 <= wc <= 2.38, f"Worst case {wc:.3f} % (correct: 2.341 %)"),
        (99.5 <= g <= 100.0, f"Gaussian yield {g:.4g} % (expected ≈ 99.99 %)"),
        (96.5 <= u <= 99.3, f"Uniform yield {u:.4g} % (expected ≈ 97.9 %, "
                            "±1.4 points of randomness with 1000 samples)"),
    ]


# ---------------------------------------------------------------------------
# Level 5 · Trick challenge: does it recognize an impossible spec?
# ---------------------------------------------------------------------------
@_wrap
def _v5(d: dict) -> Result:
    f = boolean(d, "feasible")
    return [(f is False, "Recognizes it is impossible: a passive divider scales with its input, "
                         "so ±3 % on 12 V means at least ±3 % at the output"
             if f is False else "Says it is feasible, but it is not: the input variation "
                                "(±3 %) already exceeds the allowed ±2 %")]


# ---------------------------------------------------------------------------
# Level 6 · RC low-pass filter: three variables (Ra + Rb in E12, C in E6)
# ---------------------------------------------------------------------------
@_wrap
def _v6(d: dict) -> Result:
    ra, rb, c = num(d, "Ra_ohm"), num(d, "Rb_ohm"), num(d, "C_F")
    out: Result = [
        (in_range(ra, "E12", 100, 100e3) and in_range(rb, "E12", 100, 100e3),
         f"Ra = {hs.fmt_spice(ra)}, Rb = {hs.fmt_spice(rb)}: E12 series and between 100 Ω "
         "and 100 kΩ"),
        (in_range(c, "E6", 1e-9, 1e-6),
         f"C = {hs.fmt_spice(c)}F: E6 series and between 1 nF and 1 µF"
         + (" (given in nF instead of farads?)" if c >= 1e-3 else "")),
    ]
    fc = rc_cutoff(ra, rb, c)
    if fc is None:
        return out + [(False, "Could not measure the cutoff frequency (outside 0.1 Hz–10 MHz)")]
    e = (fc - 1000) / 10
    extra = " — the best possible!" if abs(e) <= 0.0605 else " (+0.060 % was possible)"
    out.append((abs(e) <= 1.0 + 1e-6,
                f"Simulated cutoff frequency {fc:.2f} Hz, error {e:+.3f} % (limit ±1 %){extra}"))
    if "fc_Hz" in d:
        fr = num(d, "fc_Hz")
        out.append((abs(fr - fc) <= 0.005 * fc,
                    f"The fc it reports ({fr:.5g} Hz) matches the simulated one ({fc:.5g} Hz)"))
    return out


# ---------------------------------------------------------------------------
# Level 7 · Biasing an NPN that is stable against β (2N3904)
# ---------------------------------------------------------------------------
MODEL_2N3904 = (".model Q2N3904 NPN(IS=6.734f XTI=3 EG=1.11 VAF=74.03 BF=416.4 NE=1.259 "
                "ISE=6.734f IKF=66.78m XTB=1.5 BR=.7371 NC=2 ISC=0 IKR=0 RC=1 CJC=3.638p "
                "MJC=.3085 VJC=.75 FC=.5 CJE=4.493p MJE=.2593 VJE=.75 TR=239.5n TF=301.2p "
                "ITF=.4 VTF=4 XTF=2 RB=10)")
_R7 = ("R1", "R2", "RC", "RE")


def bias_point(r1, r2, rc, re_, bf: float | None = None) -> dict:
    """Operating point of the base-divider bias with ngspice: Ic, Vce and total supply current."""
    model = MODEL_2N3904 if bf is None else MODEL_2N3904.replace("BF=416.4", f"BF={bf:g}")
    d = hs.simulate_data(f"VCC vcc 0 DC 12\nR1 vcc b {r1:.12g}\nR2 b 0 {r2:.12g}\n"
                         f"RC vcc c {rc:.12g}\nRE e 0 {re_:.12g}\nQ1 c b e Q2N3904\n{model}\n.op")
    q = d["devices"].get("q1")
    if not q or "vce" not in q or "i(vcc)" not in d["op"]:
        raise ValueError("the simulation did not give the transistor's operating point")
    return {"ic": q["ic"], "vce": q["vce"], "itot": -d["op"]["i(vcc)"]}


def _measure_bias(d: dict) -> tuple[dict, dict, float, list[str]]:
    rs = {k: num(d, f"{k}_ohm") for k in _R7}
    n = bias_point(*rs.values())
    change = (bias_point(*rs.values(), bf=100)["ic"] - n["ic"]) / n["ic"] * 100
    failures = [f"{k} is not E24 or not between 100 Ω and 1 MΩ" for k, v in rs.items()
                if not in_range(v, "E24", 100, 1e6)]
    if not 0.95e-3 <= n["ic"] <= 1.05e-3:
        failures.append("Ic outside 1 mA ± 5 %")
    if not 5 <= n["vce"] <= 7:
        failures.append("Vce outside 5–7 V")
    if n["itot"] > 2e-3 + 1e-9:
        failures.append("supply current > 2 mA")
    if abs(change) > 10 + 1e-6:
        failures.append("Ic change > ±10 %")
    return rs, n, change, failures


@_wrap
def _v7(d: dict) -> Result:
    rs, n, change, _ = _measure_bias(d)
    extra = " — the best possible!" if abs(change) <= 1.015 else " (−1.01 % was possible)"
    out: Result = [
        (all(in_range(v, "E24", 100, 1e6) for v in rs.values()),
         ", ".join(f"{k} = {hs.fmt_spice(v)}" for k, v in rs.items())
         + ": E24 series and between 100 Ω and 1 MΩ"),
        (0.95e-3 <= n["ic"] <= 1.05e-3, f"Simulated Ic {n['ic'] * 1e3:.4f} mA (1 mA ± 5 %)"),
        (5 <= n["vce"] <= 7, f"Simulated Vce {n['vce']:.3f} V (between 5 and 7 V)"),
        (n["itot"] <= 2e-3 + 1e-9, f"Total supply current {n['itot'] * 1e3:.3f} mA (≤ 2 mA)"),
        (abs(change) <= 10 + 1e-6, f"Ic change if β drops from 416 to 100: {change:+.2f} % "
                                   f"(limit ±10 %){extra}"),
    ]
    if "ic_mA" in d:
        ic = num(d, "ic_mA") * 1e-3
        out.append((abs(ic - n["ic"]) <= 0.01 * n["ic"],
                    f"The Ic it reports ({ic * 1e3:.4g} mA) matches the simulated one "
                    f"({n['ic'] * 1e3:.4g} mA)"))
    if "vce_V" in d:
        vce = num(d, "vce_V")
        out.append((abs(vce - n["vce"]) <= 0.05,
                    f"The Vce it reports ({vce:.4g} V) matches the simulated one "
                    f"({n['vce']:.4g} V)"))
    if "ic_change_pct" in d:
        v = num(d, "ic_change_pct")
        out.append((abs(abs(v) - abs(change)) <= 0.5,
                    f"The change it reports ({v:+.2f} %) matches the simulated one "
                    f"({change:+.2f} %)"))
    return out


CHALLENGES: dict[str, Challenge] = {r.id: r for r in [
    Challenge("0", "Warm-up: simulate a given divider",
              """
Simulate this circuit with the 'simulate' tool and tell me the voltage at node out and the
current delivered by the source (in mA, as an absolute value).

V1 in 0 DC 12
R1 in out 18k
R2 out 0 13k
.op
""",
              '{"vout_V": <number>, "current_mA": <number>}',
              _v0, "Vout = 5.0323 V; I = 0.3871 mA.", ["tools"],
              measured={"vout_V": 1, "current_mA": 1e-3}),

    Challenge("1", "Design a 12 V → 5 V divider with the E24 series",
              """
Design a resistive divider that gets 5 V from 12 V (no load).
- R1 goes from the 12 V to the output and R2 from the output to ground.
- R1 and R2 must be values of the E24 series (any decade).
- The current through the divider must be between 0.1 mA and 1 mA.
- Minimize the output error relative to 5 V; at most ±1 %.
Verify the final design with the simulator.
""",
              '{"R1_ohm": <number>, "R2_ohm": <number>, "vout_V": <number>, '
              '"error_pct": <number>}',
              _v1, "Best: R1 = 18k, R2 = 13k (+0.645 %). 51k/36k (−0.690 %) and 47k/33k "
                   "(−1.000 %) also pass.", ["design", "iterative"],
              measured={"vout_V": 1}),

    Challenge("2", "Maximum resistor tolerance for an error ≤ 2 %",
              """
We have a divider that gets 5 V from 12 V with R1 = 14 kΩ (top) and R2 = 10 kΩ (bottom),
no load. Both resistors have the same tolerance t.
1. What is the maximum tolerance t (in %) that guarantees that, in the worst case, the output
   does not deviate more than ±2 % from 5 V? Give the value with two decimals.
2. Check with a corner analysis that with that t the limit is just met.
3. Which commercial tolerance would you choose among 0.1 %, 0.25 %, 0.5 %, 1 %, 2 % and 5 %?
""",
              '{"max_tolerance_pct": <number>, "commercial_tolerance_pct": <number>}',
              _v2, "t max = 1.709 % (analytic: 2t(1−a)/(1+t(2a−1)) = 0.02 with a = 5/12). "
                   "Commercial: 1 % (worst case 1.169 %); with 2 % the worst case is 2.341 %.",
              ["tolerances", "iterative"]),

    Challenge("3", "Loaded divider (ADC) with tolerances, E96 series",
              """
The output of a 12 V → 5 V divider feeds the input of an ADC that behaves like a
100 kΩ ± 10 % resistor to ground. Design R1 and R2 with E96 values and 1 % tolerance so
that:
- The current delivered by the 12 V source is between 0.1 mA and 1 mA (nominal values).
- In the worst case (R1 and R2 ±1 %, load ±10 %) the output does not deviate more than ±2 %
  from 5 V.
Try to make the worst case as small as possible and verify it with 'analyze_tolerances'
(corners method).
""",
              '{"R1_ohm": <number>, "R2_ohm": <number>, "worst_error_pct": <number>}',
              _v3, "Only 26 E96 pairs pass. Best: R1 = 9.76k, R2 = 7.5k (worst case 1.548 %). "
                   "Others: 10.2k/7.87k (1.600 %), 9.53k/7.32k (1.613 %), 10k/7.68k (1.664 %). "
                   "The naive 14k/10k design gives −5.5 % from the load alone.",
              ["design", "tolerances", "load"]),

    Challenge("4", "Monte Carlo versus worst case",
              """
12 V → 5 V divider with R1 = 14 kΩ and R2 = 10 kΩ, no load, both resistors 2 %.
1. Compute the worst-case (corners) error in %.
2. Estimate with Monte Carlo (at least 1000 samples) the percentage of units whose output
   stays within ±2 % of 5 V, under two assumptions: gaussian distribution (3σ = tolerance)
   and uniform distribution.
3. Briefly explain why the results differ and what building 10,000 units with 2 %
   resistors would imply.
""",
              '{"worst_error_pct": <number>, "yield_gaussian_pct": <number>, '
              '"yield_uniform_pct": <number>}',
              _v4, "Worst case 2.341 %. Yield ≈ 99.99 % (gaussian) and ≈ 97.9 % (uniform): "
                   "about 210 units out of 10,000 fail under the uniform assumption.",
              ["tolerances", "statistics"]),

    Challenge("5", "Trick challenge: impossible specification",
              """
The 12 V input comes from a supply with ±3 % tolerance (between 11.64 V and 12.36 V).
Design a resistive divider (any E series and any commercial tolerance) whose output is
always within 5 V ± 2 % over the whole input range. If it is not possible, say so and
propose an alternative.
""",
              '{"feasible": <true or false>, "reason": "<short text>", '
              '"alternative": "<short text>"}',
              _v5, "Impossible: Vout is proportional to Vin, so it inherits at least the ±3 %. "
                   "Alternative: a voltage reference or a regulator (e.g. a 5 V LDO).",
              ["judgment", "honesty"]),

    Challenge("6", "RC low-pass filter at 1 kHz: Ra + Rb (E12) and C (E6)",
              """
Design a first-order RC low-pass filter, no load, with a 1 kHz cutoff frequency (the
frequency at which |Vout/Vin| = 1/√2).
- The signal enters through Ra, which is in series with Rb up to the output; C goes from the
  output to ground.
- Ra and Rb must be values of the E12 series (any decade), each between 100 Ω and 100 kΩ.
- C must be a value of the E6 series (any decade), between 1 nF and 1 µF.
- Minimize the cutoff frequency error relative to 1 kHz; at most ±1 %.
Measure the cutoff frequency of the final design with the simulator (.ac analysis and .meas).
""",
              '{"Ra_ohm": <number>, "Rb_ohm": <number>, "C_F": <number in farads, e.g. '
              '2.2e-9>, "fc_Hz": <number>, "error_pct": <number>}',
              _v6, "49 designs pass, in 19 error levels. Best: 4.7k + 120 Ω with 33 nF, or "
                   "47k + 1.2k with 3.3 nF (+0.060 %). Next come 12k + 3.9k with 10 nF and "
                   "1.2k + 390 with 100 nF (+0.097 %). The obvious path (10 nF → 15.9k) gives "
                   "15k + 1k (−0.528 %) or 15k + 820 (+0.604 %). With a single E12 resistor "
                   "the best is +2.6 %. Careful: measuring with vdb = −3 instead of −3.0103 "
                   "shifts fc by −0.24 %.",
              ["design", "ac", "several variables"], measured={"fc_Hz": 1}),

    Challenge("7", "Biasing an NPN (2N3904) that is stable against β",
              f"""
Bias an NPN 2N3904 transistor in common emitter with a voltage divider on the base and an
emitter resistor, supplied with VCC = 12 V:
- R1 goes from VCC to the base, R2 from the base to ground, RC from VCC to the collector and
  RE from the emitter to ground.
- The four resistors must be values of the E24 series (any decade), between 100 Ω and
  1 MΩ.
- With the model as given: Ic = 1 mA ± 5 %, Vce between 5 and 7 V, and the total current
  delivered by the supply must not exceed 2 mA.
- Current gain varies a lot from one transistor to another, so the operating point must be
  stable: minimize how much Ic changes if BF drops from 416.4 to 100 (with the rest of the
  model unchanged); at most ±10 %.
Use this model (copy it as is into your netlists):
{MODEL_2N3904}
Verify the final design with the simulator.
""",
              '{"R1_ohm": <number>, "R2_ohm": <number>, "RC_ohm": <number>, "RE_ohm": <number>, '
              '"ic_mA": <number>, "vce_V": <number>, "ic_change_pct": <number>}',
              _v7, "11,226 designs pass; |ΔIc| between 1.0 % and 10 % in 91 levels of 0.1 % "
                   "(median 2.9 %). Best ≈ −1.01 %: R1 = 4.7k, R2 = 7.5k, RE = 6.8k and a small "
                   "RC (120–270 Ω; RC barely affects stability). It sits at the limit: "
                   "Vce ≈ 5.1 V and supply current ≈ 1.97 mA. Textbook designs (Vbe ≈ 0.7 V, "
                   "VE ≈ 1 V, divider at 10·Ib) fail: with this model Ic comes out at 1.1 to "
                   "1.35 mA.",
              ["design", "transistors", "several variables", "trade-off"],
              measured={"ic_mA": 1e-3, "vce_V": 1}, derived={"ic_change_pct": "ic"}),
]}


# Reference answers for the self-test (they check that the verifiers work)
CORRECT_REFERENCES = {
    "0": {"vout_V": 5.0323, "current_mA": 0.3871},
    "1": {"R1_ohm": 18000, "R2_ohm": 13000, "vout_V": 5.0323, "error_pct": 0.645},
    "2": {"max_tolerance_pct": 1.71, "commercial_tolerance_pct": 1},
    "3": {"R1_ohm": 9760, "R2_ohm": 7500, "worst_error_pct": 1.548},
    "4": {"worst_error_pct": 2.341, "yield_gaussian_pct": 99.99, "yield_uniform_pct": 97.9},
    "5": {"feasible": False, "reason": "-", "alternative": "-"},
    "6": {"Ra_ohm": 4700, "Rb_ohm": 120, "C_F": 33e-9, "fc_Hz": 1000.6, "error_pct": 0.06},
    "7": {"R1_ohm": 4700, "R2_ohm": 7500, "RC_ohm": 240, "RE_ohm": 6800, "ic_mA": 0.9773,
          "vce_V": 5.072, "ic_change_pct": -1.01},
}
WRONG_REFERENCES = {
    "0": {"vout_V": 5.5, "current_mA": 0.3871},
    "1": {"R1_ohm": 14000, "R2_ohm": 10000, "vout_V": 5.0, "error_pct": 0},
    "2": {"max_tolerance_pct": 2.0, "commercial_tolerance_pct": 2},
    "3": {"R1_ohm": 14000, "R2_ohm": 10000, "worst_error_pct": 1.2},
    "4": {"worst_error_pct": 2.0, "yield_gaussian_pct": 95, "yield_uniform_pct": 90},
    "5": {"feasible": True, "reason": "-", "alternative": "-"},
    # 910 is E24, not E12: the typical mistake of tuning with the wrong series
    "6": {"Ra_ohm": 15000, "Rb_ohm": 910, "C_F": 10e-9, "fc_Hz": 1000.3, "error_pct": 0.03},
    # Textbook design with Vbe ≈ 0.7 V and VE ≈ 1.5 V: with the real model, Ic and Vce fail
    "7": {"R1_ohm": 33000, "R2_ohm": 8200, "RC_ohm": 5100, "RE_ohm": 1500, "ic_mA": 1.0,
          "vce_V": 5.5, "ic_change_pct": -3.6},
}
