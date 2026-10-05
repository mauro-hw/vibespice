# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026 Mauro Rodriguez Blasco
# Additional term under section 7(b) of the license: see NOTICE.
"""Diagnostics: the connection check (vibespice check) and the local self-test without AI
(vibespice selftest)."""
from __future__ import annotations

import time

from . import challenges as C
from . import config
from . import tools as hs
from .agent import (PROFILES, APIError, OWUIClient, calls_from_text, model_options,
                    split_thinking, without_origin)
from .batch import duration, fits_another
from .console import BOLD, GREEN, RED, YELLOW, c, fmt_dur, shorten, tilde


def check(client: OWUIClient | None, settings: config.Settings) -> int:
    failures = 0
    print(c("1. ngspice", BOLD))
    v = hs.ngspice_version()
    if v:
        r = hs.simulate_data("V1 in 0 DC 12\nR1 in out 14k\nR2 out 0 10k\n.op")
        ok = abs(r["op"].get("v(out)", 0) - 5.0) < 1e-6
        print(("  ✅ " if ok else "  ❌ ") + f"{v}; test divider: v(out) = "
              f"{r['op'].get('v(out)')} V")
        failures += not ok
    else:
        print(f"  ❌ '{hs.NGSPICE}' not found. Install ngspice (e.g. sudo apt install ngspice, "
              "sudo dnf install ngspice or brew install ngspice) or set its path in ngspice, in "
              f"{tilde(settings.file)}.")
        failures += 1
    if client is None:
        return failures + 1
    print(c("2. Open WebUI", BOLD))
    print(f"  {tilde(settings.file)}" + (f" · profile '{settings.profile}'" if settings.profile
                                         else " · environment variables only"))
    for w in settings.warnings:
        print(c(f"  ⚠ {w}", YELLOW))
    print(f"  URL: {client.url} · key: {client.key[:5]}…{client.key[-3:]}")
    try:
        mods = client.models()
        print(f"  ✅ connected; {len(mods)} models visible to this key")
        if not client.model:
            print(f"  ❌ no model chosen: copy one into model, in your profile. Models: "
                  f"{', '.join(mods[:15])}")
            return failures + 1
        if client.model in mods:
            print(f"  ✅ model '{client.model}' is available")
        else:
            print(f"  ❌ '{client.model}' is not listed. Models: {', '.join(mods[:15])}")
            failures += 1
    except APIError as e:
        print(f"  ❌ {e}")
        return failures + 1
    print(c("3. Ollama (through Open WebUI, needs an admin key)", BOLD))
    try:
        print(f"  Ollama {client.ollama_version()}")
        loaded = client.ollama_ps()
        if not loaded:
            print("  No model loaded on the GPU right now.")
        for m in loaded:
            vram = (m.get("size_vram") or 0) / 1e9
            print(f"  loaded: {m.get('name')} · context {m.get('context_length', '?')} tokens · "
                  f"{vram:.1f} GB in VRAM · expires {str(m.get('expires_at', '?'))[:19]}")
    except APIError as e:
        print(f"  (not available: {shorten(str(e), 120)})")
    try:
        f = client.model_card()
        det, info = f.get("details") or {}, f.get("model_info") or {}
        ctx = next((v for k, v in info.items() if k.endswith(".context_length")), "?")
        cap = f.get("capabilities") or []
        th = f.get("thinking") or {}
        levels = ", ".join("no" if v is False else "yes" if v is True else str(v)
                           for v in th.get("values") or []) or "does not reason"
        default = th.get("default")
        print(f"  {client.model}: {det.get('parameter_size', '?')} "
              f"{det.get('quantization_level', '')} · native context {ctx}"
              f" · tools: {'yes' if 'tools' in cap else 'no'}"
              f" · images: {'yes' if 'vision' in cap else 'no'}")
        print(f"  reasoning (--think): {levels}"
              + (f" · default {'yes' if default is True else default}" if default is not None
                 else ""))
        profile = next((pr for pr in PROFILES if client.model.startswith(pr)), None)
        print("  sampling: " + (f"agent profile '{profile}'" if profile else
                                "the Modelfile's (the agent does not know this family)"))
    except APIError as e:
        print(f"  (model card not available: {shorten(str(e), 120)})")
    print(c("4. Chat test", BOLD))
    try:
        t0 = time.perf_counter()
        r = client.chat([{"role": "user", "content": "Reply with just the word: OK"}],
                        options=model_options(client.model, False, None))
        text, _ = split_thinking(r["choices"][0]["message"].get("content") or "")
        if text:
            print(f"  ✅ reply '{shorten(text, 60)}' in {fmt_dur(time.perf_counter() - t0)}")
        else:
            print("  ⚠ empty reply (the model answers but with no visible text)")
    except (APIError, KeyError, IndexError) as e:
        print(f"  ❌ {e}")
        return failures + 1
    print(c("5. Tool call test (native mode)", BOLD))
    try:
        t0 = time.perf_counter()
        r = client.chat([{"role": "user", "content": "What is 1234 * 5678? Use the calculate "
                          "tool; do not compute it yourself."}],
                        tools=[e for e in hs.SCHEMAS if e["function"]["name"] == "calculate"],
                        options=model_options(client.model, False, None))
        m = r["choices"][0]["message"]
        tc = m.get("tool_calls") or []
        text, _ = split_thinking(m.get("content") or "")
        if tc:
            print(f"  ✅ requested {tc[0]['function']['name']}"
                  f"({tc[0]['function'].get('arguments')}) in "
                  f"{fmt_dur(time.perf_counter() - t0)} → native mode works")
        elif calls_from_text(text):
            print("  ⚠ wrote the call as text instead of using tool_calls → the agent "
                  "understands it anyway, but also try --mode text")
        else:
            print(f"  ❌ did not call the tool; replied: '{shorten(text, 100)}'. "
                  "Try --mode text.")
            failures += 1
    except (APIError, KeyError, IndexError) as e:
        print(f"  ❌ {e}")
        failures += 1
    try:
        for m in client.ollama_ps():
            if client.model in (m.get("name"), m.get("model")):
                print(f"  The server has it loaded with a context of "
                      f"{m.get('context_length', '?')} tokens and "
                      f"{(m.get('size_vram') or 0) / 1e9:.1f} GB in VRAM.")
    except APIError:
        pass
    print(c("\nAll good." if not failures else f"\n{failures} check(s) with problems.",
            GREEN if not failures else RED))
    return failures


def selftest() -> int:
    """Tests tools and verifiers without AI (validates your ngspice installation)."""
    failures = 0

    def ok_if(cond: bool, text: str):
        nonlocal failures
        print(("  ✅ " if cond else "  ❌ ") + text)
        failures += not cond

    print(c("Tools", BOLD))
    ok_if(hs.ngspice_version() is not None, f"ngspice found: {hs.ngspice_version()}")
    d = hs.simulate_data("V1 in 0 DC 12\nR1 in out 18k\nR2 out 0 13k\n.op")
    ok_if(abs(d["op"].get("v(out)", 0) - 5.032258) < 1e-5,
          f"simulate: v(out) = {d['op'].get('v(out)')}")
    d = hs.simulate_data("V1 in 0 DC 0 PULSE(0 1 0 1n 1n 1 2)\nR1 in out 1k\nC1 out 0 1u\n"
                         ".tran 10u 5m\n.meas tran tau TRIG v(out) VAL=0.001 RISE=1 "
                         "TARG v(out) VAL=0.63212 RISE=1")
    ok_if(abs(d["meas"].get("tau", 0) - 1e-3) < 2e-5,
          f".meas in a transient: τ = {d['meas'].get('tau')} s (≈ 1 ms)")
    r = hs.tolerance_data("V1 in 0 DC 12\nR1 in out 14k\nR2 out 0 10k\n.op",
                          {"R1": 1, "R2": 1}, "v(out)", "corners", target_value=5)
    ok_if(abs(r["worst_error_pct"] - 1.1686) < 0.001,
          f"corners: worst case {r['worst_error_pct']:.4f} % (≈ 1.1686 %)")
    r = hs.tolerance_data("V1 in 0 DC 12\nR1 in out 14k\nR2 out 0 10k\n.op",
                          {"R1": 2, "R2": 2}, "v(out)", "montecarlo", samples=400,
                          distribution="uniform", target_value=5, error_limit_pct=2)
    ok_if(95 <= r["yield_pct"] <= 100, f"Monte Carlo: yield {r['yield_pct']:.1f} % (≈ 98 %)")
    r = hs.tolerance_data("V1 in 0 DC 0 PULSE(0 1 0 1n 1n 1 2)\nR1 in out 1k\nC1 out 0 1u\n"
                          ".tran 10u 5m\n.meas tran tau TRIG v(out) VAL=0.001 RISE=1 "
                          "TARG v(out) VAL=0.63212 RISE=1", {"R1": 5, "C1": 10}, "tau",
                          "corners")
    ok_if(abs(r["max"] / 1e-3 - 1.155) < 0.01,
          f"tolerances on a .meas: τ max {r['max'] * 1e3:.4f} ms (≈ 1.155 ms)")
    ok_if("11k" in hs.standard_values("11.1k", "E96"), "standard_values E96")
    ok_if(hs.calculate("12*10k/(14k+10k)").endswith("= 5"), "calculate with SPICE suffixes")
    s = hs.simulate("V1 a 0 DC 9\nR1 a b 7.2k\nR2 b 0 4.7k\nR3 b 0 14k\n.op")
    ok_if("R1 = 7.2k: not in any" in s and "R2 = 4.7k: E3, E6, E12, E24\n" in s
          and "R3 = 14k: E48, E96" in s, "simulate shows the E series of each value")
    d = hs.simulate_data("V1 a 0 AC 1\nR1 a b 1k\nC1 b 0 1u\n.ac dec 10 100 100\n"
                         ".meas ac f1 WHEN mag(v(b)/v(a))=0.7")
    ok_if("f1" not in d["meas"] and "f1" in d["meas_failed"]
          and any("vm(out)" in a for a in d["warnings"]),
          "a .meas that ngspice rejects gives no false value and comes with a syntax hint")
    d = hs.simulate_data("V1 a 0 AC 1\nR1 a b 1k\nC1 b 0 1u\n.ac dec 10 1 1k\n"
                         ".meas ac f1 WHEN vm(b)=0.7071068\n.meas ac f2 WHEN vm(b)=1/sqrt(2)\n.op")
    ok_if(abs(d["meas"].get("f1", 0) - 159.15) < 0.5 and "f2" in d["meas_failed"]
          and any(".op" in a for a in d["warnings"])
          and any("must be a number" in a for a in d["warnings"]),
          "an .op next to .ac does not break the .meas, and .meas errors come with advice")
    ok_if(hs.simulate("V1 a 0 1\n.control\nop\n.endc").startswith("ERROR"), "blocks .control")
    d = hs.simulate_data("VCC vcc 0 DC 12\nRB vcc b 4.7MEG\nRC vcc c 4.7k\nQ1 c b 0 QN\n"
                         "RB2 vcc b2 10k\nRC2 vcc c2 1k\nQ2 c2 b2 0 QN\n"
                         ".model QN NPN(IS=6.734f BF=416.4 VAF=74.03)\n.op")
    q1, q2 = d["devices"].get("q1", {}), d["devices"].get("q2", {})
    ok_if(q1.get("region") == "active" and q2.get("region") == "saturation" and not d["errors"]
          and abs(q1.get("vce", 0) - (12 - 4.7e3 * q1.get("ic", 0))) < 1e-3
          and 1e-3 * 0.99 <= q1.get("ic", 0) * 1 < 5e-3,
          "simulate gives Ic, Vce and region of each transistor (active / saturation)")
    # A real mistake seen in challenge 7: base and collector swapped, with numbered nodes
    s = hs.simulate("Vcc 1 0 DC 12\nR1 1 2 91k\nR2 2 0 18k\nRC 1 3 4.7k\nRE 4 0 1.2k\n"
                    f"Q1 2 3 4 Q2N3904\n{C.MODEL_2N3904}\n.op")
    good = hs.simulate("VCC vcc 0 DC 12\nR1 vcc b 91k\nR2 b 0 18k\nRC vcc c 4.7k\nRE e 0 1.2k\n"
                       f"Q1 c b e Q2N3904\n{C.MODEL_2N3904}\n.op\nRX e x 1k")
    ok_if("Q1 (NPN): collector → node 2 · base → node 3 · emitter → node 4" in s
          and "R1: node 1 (+ of Vcc) – node 2 (collector of Q1)" in s
          and "numbered nodes (1, 2, 3, 4)" in s and "Q1 is in saturation" in s
          and "swapped terminals" in s
          and "R1: node vcc (+ of VCC) – node b (base of Q1)" in good
          and "node x (floating" in good and "numbered" not in good
          and "saturation" not in good,
          "simulate describes the connections and warns about numbered nodes and saturation")
    n7 = ("VCC vcc 0 DC 12\nR1 vcc b 4.7k\nR2 b 0 7.5k\nRC vcc c 240\nRE e 0 6.8k\n"
          f"Q1 c b e Q2N3904\n{C.MODEL_2N3904}\n.op")
    m = [hs.quantities_of(hs.simulate_data(n)) for n in (n7, n7.replace("BF=416.4", "BF=100"))]
    der = {"ic_change_pct": "ic"}
    ok_if(not without_origin({"ic_change_pct": -1.01}, {}, [], der, m)
          and without_origin({"ic_change_pct": 7.8}, {}, [], der, m)
          and without_origin({"ic_change_pct": -1.01}, {}, [], der, m[:1]),
          "origin check: the Ic change must come from two of its own simulations")
    # Real case: both circuits (nominal BF and BF = 100) in one netlist. Done wrong: sources in
    # parallel and two .model with the same name. Done right: separate nodes and models
    q100 = C.MODEL_2N3904.replace("Q2N3904", "QB").replace("BF=416.4", "BF=100")
    bad = (n7.replace("\n.op", "") + "\nVCC2 vcc 0 DC 12\nR1B vcc b2 4.7k\nR2B b2 0 7.5k\n"
           "RC2 vcc c2 240\nRE2 e2 0 6.8k\nQ2 c2 b2 e2 Q2N3904\n"
           + C.MODEL_2N3904.replace("BF=416.4", "BF=100") + "\n.op")
    good = (n7.replace("\n.op", "") + "\nVCC2 vcc2 0 DC 12\nR1B vcc2 b2 4.7k\nR2B b2 0 7.5k\n"
            f"RC2 vcc2 c2 240\nRE2 e2 0 6.8k\nQ2 c2 b2 e2 QB\n{q100}\n.op")
    s = hs.simulate(bad)
    m2 = [hs.quantities_of(hs.simulate_data(good))]
    ok_if("Q1: no solution" in s and "VCC and VCC2 are voltage sources in parallel" in s
          and "There are 2 .model statements named q2n3904" in s and "active region" not in s
          and not without_origin({"ic_change_pct": -1.0}, {}, [], der, m2),
          "simulate warns about parallel sources and repeated models, and does not accept a "
          "solution with nan; the change is valid between two transistors of the same netlist")
    ok_if(not fits_another(40 * 60, 13 * 60, 45 * 60) and fits_another(30 * 60, 13 * 60, 45 * 60)
          and duration("1h30") == 5400 and duration("45") == 2700 and duration("30s") == 30,
          "time-limited batches: does not start an iteration that does not fit; durations")

    print(c("Challenge verifiers", BOLD))
    for cid, ch in C.CHALLENGES.items():
        if ch.verify is None:
            continue
        good = ch.verify(C.CORRECT_REFERENCES[cid])
        wrong = ch.verify(C.WRONG_REFERENCES[cid])
        ok_if(all(ok for ok, _ in good) and not all(ok for ok, _ in wrong),
              f"challenge {cid}: accepts the correct solution and rejects the wrong one")
        if not all(ok for ok, _ in good):
            for ok, t in good:
                print(("       ✅ " if ok else "       ❌ ") + t)
    print(c("\nSelf-test passed." if not failures else f"\n{failures} failure(s).",
            GREEN if not failures else RED))
    return failures
