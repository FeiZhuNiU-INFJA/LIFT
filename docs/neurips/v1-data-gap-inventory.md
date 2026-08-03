# v1 → v2 Experimental Data Gap Inventory

Snapshot of what §6 currently claims **vs** what the numbers on disk actually support, so v1 arXiv release is honest and v2 can be scoped without ambiguity.

## Runtime coverage matrix

| Runtime | Report on disk | RQ1 | RQ2 | RQ3 | RQ4 | Notes |
|---|---|:-:|:-:|:-:|:-:|---|
| `openclaw` | `lift-runid-openclaw-full-r10` | ✅ | ✅ | ✅ | ✅ | 10-repeat baseline family anchor |
| `openclaw_with_openspace` | `lift-runid-openclaw-openspace-full` | ✅ | ✅ | ✅ | ✅ | Pairs with `openclaw` for augmentation Gain |
| `hermes` | `lift-runid-hermes-10-a` | ✅ | ✅ | ✅ | ✅ | 10-repeat; the RQ1 winner |
| `hermes_with_openspace` | `lift-runid-hermes-openspace-full` | ✅ | ✅ | ✅ | ✅ | Pairs with `hermes` for augmentation Gain |
| `genericagent` | `lift-runid-genericagent-full` | ✅ | ⚠️ | ✅ | ✅ | Included in RQ1/3/4; not paired in RQ2 (no plugin variant) |
| `openclaw_with_evolve` | — | ❌ | ❌ | ❌ | ❌ | Never run in this sweep; §3 mentions it as a registered runtime |
| `openclaw_with_agentmemory` | — | ❌ | ❌ | ❌ | ❌ | Runtime works; sweep not scheduled |
| `hermes_with_agentmemory` | — | ❌ | ❌ | ❌ | ❌ | Same |
| `multi_user_openclaw` | — | ❌ | ❌ | ❌ | ❌ | Same |
| `genericagent_active_evolve` | — | ❌ | ❌ | ❌ | ❌ | Would give an active-reflection RQ2 pair for GenericAgent |
| `openhuman` | — | ❌ | ❌ | ❌ | ❌ | Runtime debugged (policy-blocked fix); no full sweep yet |
| `openhuman_with_agentmemory` | — | ❌ | ❌ | ❌ | ❌ | Same |
| `evoscientist` / `_active_evolve` | — | ❌ | ❌ | ❌ | ❌ | Runtime ready; no sweep |

**§3.3 claims twelve registered runtimes; §6 evaluates five.** Not misleading — §6 is explicit about which rows are in each table — but v2 should aim to close this to at least 8 runtimes for a stronger cross-runtime story.

## Claims-in-text audit (against current tables)

| §6 claim | Status |
|---|---|
| "Hermes' review-driven distillation delivers a strong, repeat-stable interaction-cost reduction (turns −17.2%, latency −23.0%, 95% CI fully below zero)." | ✅ backed by RQ3 CI table |
| "Only Hermes and Hermes+OpenSpace show 95% CIs on ΔTurns% that lie entirely below zero." | ✅ backed by RQ3 CI table |
| "OpenSpace rescues an otherwise-neutral base on OpenClaw (Gain: turns −6.7pp, tokens −7.6pp)." | ✅ backed by RQ2 delta-of-delta |
| "GenericAgent … not statistically distinguishable from zero at 10 repeats." | ✅ backed by RQ3 CI table |
| "Pass rate on EALE is saturated (0.99–1.00)." | ✅ backed by RQ4 absolute means |
| "12 registered runtimes" (§3.3) | ⚠️ true at runtime level; only 5 evaluated |

## What v2 should add (priority order)

1. **Sweep `openclaw_with_evolve`.** The paper's core narrative is "implicit accumulation vs. explicit review-driven distillation." Right now we have implicit (openclaw), review-driven (hermes) — but not the explicit-`learn review` OpenClaw variant that would make it a proper 3-way ablation of the *distillation-step* variable. This is the single most valuable addition for RQ1's story.
2. **Add `genericagent_active_evolve` as GenericAgent's RQ2 augmentation partner.** Currently GenericAgent has no augmentation row in RQ2; adding the `_active_evolve` pair gives us a third RQ2 family.
3. **Complete the memory-provider ablation.** `openclaw_with_agentmemory` + `hermes_with_agentmemory` together answer "does an external memory service supersede the container's committed FS-state?" — a natural extension of the RQ2 augmentation framing.
4. **Longer repeat count for the marginal effects.** Rows with $|\Delta| \lesssim 5\%$ (openclaw, genericagent) sit inside the 10-repeat CI band; a 20- or 30-repeat re-run could tighten these to a directional call. Not required — v1 explicitly recommends against reading small effects at 10-repeat resolution — but valuable if referees push.
5. **Include OpenHuman and/or EvoScientist in the runtime matrix.** Both runtimes are debugged and ready; adding either gives us a non-Claw, non-Hermes family to test the generalizability of the "review-driven distillation wins" thesis.

## Not v2 blockers (already documented as limitations)

- Cross-runtime ranking not claimed (§6.6 finding #5, §7 limitation #2). No experimental fix required.
- Pass-rate saturation (§6.6 finding #4). Benchmark design issue, not a run issue; belongs in a v3 or follow-up benchmark refresh.
- Safety regression (§2 last paragraph, §7 last paragraph). Explicit orthogonal-future-work carve-out.

## What NOT to change between v1 and v2

- The population-ratio Δ definition (locked in v1 to prevent per-task-ratio direction flip; changing again would confuse readers of both versions).
- The two-phase Suite JSON schema (§4). Any v2 change here would invalidate the benchmark's shipped hash.
- The three-layer architecture description (§3.3). Runtime additions don't change the abstraction.
