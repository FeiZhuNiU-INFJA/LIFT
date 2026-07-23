# LIFT: A Counterfactual Framework and Benchmark for Disentangling Genuine Self-Evolution in LLM Agents

> NeurIPS 2027 submission draft (Evaluations and Datasets Track). English, submission-ready structure. Experimental numbers are placeholders marked `[TODO-DATA]` pending the full run. This document is self-contained: it merges the evaluation protocol (framework) and the benchmark suite into a single paper. Working subtitle / acronym anchor: **LIFT = Loaded Impact on Final Task.**

---

## Abstract

Self-evolving LLM agents continuously accumulate reusable experience *artifacts* — skills, memories, standard operating procedures (SOPs), tool boxes, personas — produced during interaction and reloaded on later tasks. Yet mainstream evaluation cannot separate *genuine* capability generalization from *pseudo* performance growth introduced by evaluation confounds: training-memory leakage (the agent replaying an earlier task's output into the next), cross-session context pollution, grader self-preference, and non-reproducible runtimes. As a result, "the agent evolved a pile of artifacts" is routinely mistaken for "the artifacts help downstream."

We present **LIFT (Loaded Impact on Final Task)**, a counterfactual evaluation framework that makes the *Base vs. Loaded* contrast on held-out tasks its single scientific question. LIFT operationalizes this contrast with three mechanisms: (i) **paired A/B runs** of the same held-out task with and without consolidated artifacts; (ii) **strict separation** of warmup (training) tasks from holdout (unseen) tasks; and (iii) **Docker container snapshots** (`docker commit` → delta image) that solidify the runtime agent state as an artifact-agnostic, cross-machine-reproducible carrier. Together these isolate the confounds above, so that a positive delta on an unseen task is *attributable to the artifacts* rather than to leakage, context pollution, or environment drift. A **three-layer decoupling** (pipeline / runtime adapter / evaluation kernel) hosts twelve heterogeneous agent runtimes behind four hooks; a **work-agent + judge-agent review loop** scores each task; and multi-level concurrency plus Langfuse trace backfill make every run auditable in both *conclusion* and *process*. We release a self-contained **benchmark of 14 scenes (84 tasks)** built on a two-phase schema with a deliberate ~75%/25% train-test requirement overlap designed to expose over-fitting to literal training requirements. LIFT provides a standardized, attributable pipeline for empirical research on self-evolving agents and makes precise the methodological gaps in existing agent benchmarks. All code, container images, and datasets are released.

---

## 1 Introduction

### 1.1 From model evaluation to agent evaluation

LLM evaluation began as "feed a prompt, check the output." Agents shattered that: a single task becomes a long chain of *planning → tool calls → file-system operations → multi-turn feedback → final deliverable*, and the model is only one node. An agent that appears to "answer correctly" may do so because (i) the task itself is flawed (the ABC Checklist reports overestimation of up to 100% on ill-posed items), (ii) it *replays* the previous task's output into the next task (pseudo-evolution), (iii) the grader is the same model family (self-preference bias), or (iv) environment fidelity is too low (a safety-benchmark taxonomy reports Kendall's *W* ≈ 0.10 — near-random cross-benchmark safety rankings).

### 1.2 Self-evolving agents magnify the problem

Over the past year, self-evolving agents accumulate reloadable artifacts — skills, memories, SOPs, MCP boxes, personas — during interaction. Commercially, "an agent that keeps learning" is the headline; academically, the four-component loop (System Inputs → Agent System → Environment → Optimisers) has crystallized. But a blunt empirical fact stands out: on SkillsBench's 86 tasks, *curated* skills yield +16.2pp on average, while *self-generated* skills are on average useless or harmful. Hence:

> "The agent evolved a pile of artifacts" ≠ "the artifacts help downstream."

These must be evaluated separately, and the method must be a **causal contrast** — not how high the evolved agent scores in absolute terms, but the score difference on *the same task, same agent, same tool set, artifacts loaded vs. not*.

### 1.3 Three fault lines in current evaluation

Surveying 32 open-source works (surveys / benchmarks / SDKs) along the axis "can it answer *do the artifacts help?*", we find three shared fault lines:

| Fault line | Symptom | Representative work |
|---|---|---|
| **No loaded/unloaded contrast** | Benchmarks measure a bare agent's pass rate; no artifact-dimension A/B | AgentBench, GAIA, SWE-bench, REAL, τ-bench |
| **Train and test tasks entangled** | Agent learns on Q1..Qn then is tested on the same Q1..Qn — cannot separate "learned" from "memorized" | most episodic self-evolution setups |
| **Runtime not reproducible / not isolated** | Multi-step FS ops, concurrency, missing trace correlation → unstable deltas, huge variance across repeats | most host-machine eval scripts |

The third is the most underrated. Once evaluation must be *run repeatedly* — for commercial artifact certification, for iterating an evolution mechanism, for comparing runtimes — reproducibility flips from nice-to-have to hard requirement. REAL's NeurIPS Oral centered exactly on making deterministic simulation a first-class citizen of web-agent evaluation, evidencing the community's weight on this.

### 1.4 Contributions

We propose **LIFT**, whose thesis is one sentence:

> To evaluate a self-evolving agent, ask "does it do better on *unseen* tasks after loading its artifacts?" — not "how high does it score after running through all tasks?"

Concretely we contribute:

1. **A scientific protocol** — train/test separation (`warmup_tasks` / `holdout_tasks`) + same-task paired runs (baseline / evolved) + a work-judge review loop (§3).
2. **An engineering carrier, released** — container snapshots (`docker commit` → delta image) as the artifact-agnostic form of evolved state; each holdout task starts an isolated container; runtime / pipeline / evaluation are decoupled into three layers (§3.3, §3.5). We release the framework code, twelve runtime adapters, container images, and datasets, so the protocol is reusable, criticizable, and comparable.
3. **Repeatability & observability** — multi-level concurrency and pre-chat + Langfuse trace backfill, so both conclusion and process are re-queryable (§3.6–3.7).
4. **A self-contained benchmark suite** — 14 scenes / 84 tasks on a two-phase schema with a deliberate 75%/25% train-test requirement overlap (§4).

---

## 2 Related Work

**General agent benchmarks.** AgentBench, GAIA, REAL, τ-bench / τ²-bench, SWE-bench, and harness-comparison benchmarks answer "can the agent do the job on a static task set." They are LIFT's *task source*, not its contrast object. Some already do cross-harness comparison and tiered evaluation (script verification / point-wise verification / pairwise comparison), a design we reuse — but none reach the artifact-dimension causal contrast.

**Artifact benchmarks.** SkillsBench, EvolveTool-Bench, and memory benchmarks (EvoMemBench, Evo-Memory) begin to probe the artifact dimension but each binds to one artifact form (skill / tool / memory). They provide LIFT's key motivating evidence — "self-generated skills are on average ineffective." LIFT's **artifact-source-agnostic** stance abstracts over them: whether the artifact is a skill, a memory, or an SOP, the verification logic is *Base vs. Loaded*.

**Lifelong / sequential learning.** LifeLongAgentBench and SEA-Eval provide learning-curve metrics (FWT / BWT / RecoveryRate). LIFT positions these as **evolution-specific diagnostics**: if the Base-vs-Loaded test is conclusive, no curve is needed; when it is not, learning curves help diagnose forgetting vs. non-transfer. This differs from prior work that treats them as primary metrics, and it avoids coupling evolution-mechanism complexity into the main evaluation path.

**Evaluation methodology.** The ABC Checklist, Agent-as-a-Judge, and MAJ-Eval answer "how do we grade reliably." LIFT's review loop borrows Agent-as-a-Judge's *tool-augmented verification*: the judge runs in a separate session of the *same* runtime, so it can call real tools to verify outputs rather than grading on surface text.

**Evaluation SDKs.** DeepEval, Opik, and Promptfoo target LLM-API-layer developer evaluation at the granularity of prompt × model. LIFT's granularity is *in-container agent × holdout task × baseline/evolved* — a different layer. Langfuse plays Opik's *trace* role in LIFT — we use only its trace capability, not its evaluator, because the evaluator role is filled by the work-judge loop.

**Safety evaluation.** OS-Harm and RAS-Eval flag that loading artifacts introduces new safety risk. LIFT keeps SafetyRegression as an optional cross-cutting dimension (off by default, to avoid entangling it with functional artifact evaluation); a full safety protocol is orthogonal future work.

---

## 3 The LIFT Framework

LIFT = **L**oaded **I**mpact on **F**inal **T**ask. The scientific question: **after loading artifacts, is there positive impact on the final (holdout) task?**

### 3.1 Design principles

We treat the survey conclusions as hard constraints that all engineering must obey:

| Principle | Meaning |
|---|---|
| **P1 Single causal contrast** | The only variable under test is "loaded vs. not"; everything else held constant |
| **P2 Train/test separation** | The tasks used to evolve (warmup) and the tasks used to test (holdout) must not overlap |
| **P3 Artifact-source agnostic** | Whether the artifact is a skill / memory / SOP / MCP box, the evaluation logic is identical |
| **P4 Reproducible runtime** | The same `run_id` re-run on any machine should produce a consistent structure (including traces) |
| **P5 Optional evolution diagnostics** | Learning curves (FWT / BWT) are used only when P1 is inconclusive |

### 3.2 Protocol layer: what a "LIFT run" is

A **Suite** consists of two task groups — this is P2 enforced at the data-structure level:

```text
SuiteSpec
├── warmup_tasks[]   ← trigger artifact production (learn / evolve)
└── holdout_tasks[]  ← final tasks, the tasks under test
```

warmup and holdout are **explicit** separate fields in the suite JSON; the runtime no longer slices by question index.

**Control groups.** Each holdout task is run twice, forming one TaskRun:

```text
TaskRun
├── baseline   PhaseRun  ← clean container from base image, no artifacts
└── evolved    PhaseRun  ← clean container from delta image, with artifacts
```

**Key engineering trade-offs.**

- **Warmup container orchestration is configurable.** In a single-agent evolution scenario, all warmup tasks share one container by default (`parallel_single`; continuous file-system state is a natural requirement of the evolution plugin). The framework also supports `parallel_multi` for scenarios where artifacts should live outside the container (e.g., an external memory service): each task gets its own container, artifacts land in the external service, and `docker commit` degenerates to a no-op. Both forms are transparent to the upper pipeline.
- **Holdout starts a new container per task.** Unlike warmup, this is a protocol-level hard constraint — baseline must be an *uncontaminated* environment, and per-task workspaces must be isolated. Hence `HoldoutContainerPolicy` (`src/lift/policies/container.py`) offers only `SERIAL_MULTI` / `PARALLEL_MULTI` "multi-container" forms, with no single-container option.
- **All holdout tasks share one delta.** The artifact is a suite-level constant and must not vary across holdout tasks.

**Report hierarchy.**

```text
EvalReport
└── runs[]              ← --repeat iterations
      └── suites[]      ← --suite multiple suites
            └── tasks[]
                  ├── baseline: PhaseRun
                  └── evolved:  PhaseRun
```

### 3.3 Abstraction layer: runtime / pipeline / eval decoupling

LIFT's code follows three abstraction layers, each with a single concern:

```text
CLI / Pipeline           ← slice tasks, loop, concurrency, write report (doesn't know what OpenClaw is)
        ↓
AgentRuntimeAdapter      ← container, artifact materialization, how to chat (doesn't know what holdout / repeat is)
        ↓
lift/eval (work + judge) ← per-task review loop (doesn't know what Docker is)
```

The direct payoff: **integrating a new runtime is near-zero-cost.** Registered runtimes include:

| runtime | image | evolve behavior |
|---|---|---|
| `openclaw` | base image, no evolution plugin | no-op after warmup; only `docker commit` |
| `openclaw_with_openspace` | with-openspace image (OpenSpace MCP skill hub) | reuses base warmup / commit flow; MCP-side skill hub is exercised during warmup |
| `openclaw_with_agentmemory` | with-agentmemory image | container-local agentmemory server on `:3111` (offline embedding); bridge network; base warmup / commit flow captures `/root/.agentmemory` |
| `genericagent` / `genericagent_active_evolve` | `lift-genericagent:latest` | baseline file I/O; active variant does reflection chat |
| `hermes` | `lift-hermes:latest` | Hermes review flow writes `/opt/hermes-state`, then commit |
| `hermes_with_openspace` | `lift-hermes-with-openspace:latest` | Hermes review flow + OpenSpace MCP registered in `config.yaml`; commit |
| `hermes_with_agentmemory` | `lift-hermes-with-agentmemory:latest` | Hermes review flow + agentmemory provider (container-local `:3111`, bridge network); commit |
| `openhuman` | `lift-openhuman:latest` | Rust JSON-RPC runtime; long-term memory / wiki paths enter the delta |
| `openhuman_with_agentmemory` | `lift-openhuman-with-agentmemory:latest` | `config.toml` `[memory] backend=agentmemory` routes to container-local `:3111`; bridge network; commit captures `/root/.agentmemory` |
| `evoscientist` / `evoscientist_active_evolve` | `lift-evoscientist:latest` | baseline captures natural state change; active variant triggers EvoMemory AutoSkills, then commit |

> **MCP note.** OpenSpace variants are only added to MCP-capable runtimes (OpenClaw, Hermes). GenericAgent (fixed atomic tools) and OpenHuman (no `mcp_servers` field) are not MCP clients, so they do not receive an OpenSpace variant.

A new runtime implements four hooks — `resolve_docker_image` / `start_container` / `worker_judger_factory` / `evolve_after_warmup` — on the container base class `ContainerAgentRuntimeAdapter` (`src/lift/adapters/container/adapter.py`), which reuses the entire `docker commit` / holdout-orchestration logic. This directly realizes P3: different runtimes place artifacts in the image / group memory / file system, all transparent to the pipeline.

**Counter-example (design origin).** An early host-machine implementation bound OpenClaw directly into the eval script. When we tried to integrate a second runtime (Hermes), the whole pipeline had to be rewritten — the direct motivation for the current three-layer abstraction.

### 3.4 Per-task kernel: the work + judge review loop

The single task is LIFT's minimal scoring unit. We use an *execute → review → feedback → retry* loop.

**Why a loop, and why this shape.** Real-user interaction is neither one-shot nor rubric-driven. A user rarely writes their full acceptance criteria up front; they issue a query, look at the deliverable, and only then surface an unmet requirement — *one at a time*, in natural language, in the tone of *"oh, and I also wanted X."* An agent operating under this loop must not only produce a plausible first draft, but also **interpret and act on partial, unordered feedback** — a strictly harder skill than answering a fully-specified prompt. Any evaluation that collapses this to a single "answer → score" step loses the most discriminative signal about artifact usefulness: whether the evolved agent needs *fewer* clarification rounds than the base. LIFT therefore mirrors the interaction rather than the grading rubric:

- **Query only, checklist hidden.** The agent receives what a user would actually type; the ground-truth `content_reqs` list never enters the agent's context (§4.2).
- **Judge as the user, not a rubric adjudicator.** The judge role-plays the person who wrote the query, compares the deliverable against the hidden checklist, and returns a natural-language `reason` that surfaces *the top unmet requirement(s)* only — not the full diff, reconstructing the "user suddenly remembers one more thing" dynamic.
- **Feedback becomes the next prompt.** That `reason` is fed back verbatim as `current_prompt`, so the follow-up turn is indistinguishable in shape from a first turn — another human-style utterance, no metadata leak, no structured tool-call trace injected on the user's behalf.
- **Bounded by a shared `max_turns`.** The loop terminates when the judge marks success or the budget is exhausted; `baseline` and `evolved` see the same budget, so extra rounds cannot masquerade as artifact merit.

The code block below encodes exactly this loop:

```text
run_task:
  while turn < max_conversation_turns:
      work_result   = work_agent.chat(current_prompt)
      judge_result  = judge_agent.chat(...) → JSON {success, reason, score}
      if judge_result.success: return True
      current_prompt = judge_result.reason      # disclose the top unmet requirement, human-style
  return False
```

Design decisions:

1. **The judge runs in a sibling container of the same runtime** — started from the same image and workspace as the work container, but sharing neither its file system nor its process space. This simulates a real user reviewing, introduces no cross-model bias, and lets the judge call real tools to verify outputs (Agent-as-a-Judge's *executable verification*). The physical split also prevents the work agent from reading the judge's tool state, and — combined with §3.5 — keeps any reviewing artifact out of the delta.
2. **First-round and final pass rates are reported separately** — FirstRoundPassRate reflects "artifacts let the agent get it right in one shot"; FinalPassRate reflects "artifacts + feedback loop." Good artifacts show up mainly in the former.
3. **baseline and evolved must share `max_turns`** — otherwise evolved wins by getting two more feedback rounds, not by artifact merit.
4. **Tokens include the judge's consumption** — if Loaded retries less, the judge's token savings count toward the artifact's contribution.

**Scoring.** The judge, role-playing "the user," compares the deliverable against the hidden `content_reqs` checklist and returns a single scalar `score = (satisfied requirements) / (total requirements)` (`score = 1` on success), together with `success` and a natural-language `reason`. There is no rule/rubric weighted blend — the score is one judge call. Content requirements and trajectory requirements are scored on two separate lines (§4.2).

### 3.5 Artifact materialization: why `docker commit`

The carrier form of the artifact is an engineering choice that *looks incidental but determines reproducibility*. We considered three options:

| Option | Pro | Fatal con |
|---|---|---|
| Host-machine toggle load | simple | state depends on host FS; concurrency cross-contaminates; not replayable across machines |
| Structured export (YAML/JSON artifact pack) | readable, versionable | hard to cover FS-level evolution (OpenClaw's entire `~/.openclaw/` tree) |
| **Container snapshot (`docker commit`)** ✓ | full FS capture; naturally cross-machine replayable; naturally concurrency-isolated | delta images have size; need cleanup |

LIFT chooses `docker commit`. A full run's timeline:

```text
Pipeline → Adapter: warmup tasks (Q1..Qn-1)
Adapter  → Warmup container: start one container, run tasks continuously (default parallel_single)
Warmup container: evolve_after_warmup (in-container learn review)
Warmup container: docker commit → delta image
Adapter: delete warmup container

for each holdout task:
    Pipeline → Adapter: baseline  → new container from BASE image  → run + score
    Pipeline → Adapter: evolved   → new container from DELTA image → run + score

Pipeline: write report.json; on suite end, docker rmi the delta
```

Implicit constraints:

- **Delta is a suite-level temporary artifact** — `docker rmi`'d when the suite finishes, never polluting the local image list.
- **Holdout workspace must be explicitly seeded** — to avoid a first-run agent asking for a name/emoji and confusing scoring.
- **`docker commit` operates on the work container only** — the judge sibling (§3.4) is torn down without commit, so no reviewing artifact can leak into `evolved`; tool counts, tokens, and `evolve_after_warmup` hooks are likewise scoped to the work side.
- **External-artifact runtimes** use the same interface `DeltaRef(image_tag=base_image, owned=False)` (`owned=False` skips `docker rmi`), unifying "artifacts live externally" under one abstraction.

### 3.6 Concurrency & isolation: making "run it repeatedly" cheap

As evaluation goes from "produce one report" → "iterate continuously" → "commercial artifact certification," the cost of repeated runs becomes increasingly sensitive. LIFT adds concurrency + isolation at three levels:

| Level | Flag | Default |
|---|---|---|
| Between matrix cells (repeat × suite Cartesian product) | `--max-parallel-suites` | 3 |
| Between tasks (within a phase) | `--max-concurrent-tasks` | no cap |
| Between phases (within a task) | `--holdout-phase-policy` | parallel |
| Warmup container policy | `--warmup-container-policy` | parallel_single |
| Holdout container policy | `--holdout-container-policy` | parallel_multi |

Each level of concurrency rests on container isolation: every holdout task gets its own container, workspace subdirectory, and ephemeral port; cleanup is centrally tracked by a `SuiteRunResources` registry. Failure isolation prevents cascades; first-pass failed cells are retried once globally, phases/tasks retry once each, and the chat layer retries provider errors 5× / judge-JSON parse 8×.

### 3.7 Observability: an execution-time + post-processing dual chain

LIFT writes its report in two passes:

| Phase | Content | File |
|---|---|---|
| Execution | conclusion: success, score, session, workspace paths | `report.json` |
| Post-process | Langfuse trace backfill, comparison CSV, trajectory judging, HTML report | `*_backfilled.json`, `*_comparison_metrics.csv` |

Why two passes: at execution time, traces are still in the Langfuse worker queue — synchronously waiting would slow the main path; and post-processing can be re-run independently (`--evaluate-only`) to diagnose or add new metrics. Execution-time `emit_pre_chat_state` stamps `session_id` and phase metadata before each chat; post-processing `stitch_phase_langfuse_traces` merges the agent's `*_agent` trace with the plugin trace by `session_id` + time window into `PhaseRun.langfuse`. This contract guarantees **the report can open the trace** to show what actually happened on that task.

### 3.8 Metric system

- **Required**: `DownstreamPassDelta = PassRate(Loaded) − PassRate(Base)`, FirstRoundPassRate, FinalPassRate, AvgAttempts, `Outcome_i = score` (the judge's single completion ratio = satisfied/total, 1 on success), TotalTokens (incl. judge), TotalLatency.
- **Efficiency (primary for evolution)**: `impr_metric = evolved_metric / baseline_metric × 100%` over {attempts, tool calls, tokens} — an evolution that reaches the user's requirements in fewer interactions on similar tasks is beneficial; the greater the reduction, the better the mechanism.
- **Sequential**: Pass@k, RecoveryRate.
- **Evolution-specific (optional)**: attribution triplet *No-Evo / Only-Products / Evo-On*; curve diagnostics FWT / BWT.
- **Cross-cutting**: static DistillateConflictRate / SafetyConcernCount, dynamic SafetyRegression, cost ColdStartTTV / EvolutionROI.

LIFT itself only emits a **structured execution record (Pydantic)**; which metrics are *required* is decided by the benchmark — hence the benchmark is described as a distinct component, orthogonal to the protocol.

### 3.9 Contrast with replay mode

An early replay mode (all-suite baseline → one evolve → all-suite evolved) is not the main path; it fits only as an ablation:

|  | LIFT (this paper) | replay |
|---|---|---|
| Focus | loaded contrast on holdout final | run all tasks once before and once after evolution |
| Artifact stage | warmup → Δ image, then test | evolve once after all-suite baseline |
| Risk | none | Q5 baseline may be polluted by Q1..Q4 state; Q5 evolved may directly reuse Q1..Q4 outputs |
| Scientific question | do artifacts help on *unseen* tasks (**extrapolation**) | per-task evolution magnitude (**interpolation**, diagnostic) |

LIFT picks extrapolation as the **main** protocol precisely to exclude the second fault line of §1.3.

---

## 4 The LIFT Benchmark Suite

> This section is self-contained: the benchmark design (schema, task-authoring principles, rubric design, the 14 scenes) is described here rather than deferred to a companion paper.

### 4.1 Three-layer structure

The dataset is organized in three layers — **total dataset → scene datasets → train/test tasks** — stored human-readably as folders + task markdowns and compiled by `python -m src.cli.preprocess` into machine-readable suite JSON (`Suite` in `src/models.py`):

```text
<scene>/
├── train/                    # warmup tasks (artifact evolution)
│   ├── q1_<shortname>/{q1_<shortname>.md, q1_materials/}
│   └── ...                   # 4 tasks
├── test/                     # final / holdout tasks (LIFT paired contrast)
│   ├── q5_<shortname>/ ...
│   └── q6_<shortname>/       # 2 tasks
└── skills/                   # optional, scene-level skill
```

The compiled `Suite` explicitly separates `warmup_tasks[]` (← `train/`) from `holdout_tasks[]` (← `test/`), the data-structure enforcement of P2.

### 4.2 Task anatomy: query / requirements / trajectory requirements

Each task reconstructs a real interaction where the user *first asks vaguely, then gradually clarifies, and silently inspects the process*. It has three parts:

1. **query** — a colloquial, deliberately under-specified first instruction, carrying only entry info essential to start (which `qN_materials` to use; where to save the deliverable, `result/result_qN`). It simulates a user who has not yet worked out the details.
2. **requirements (`content_reqs`)** — the true acceptance checklist the user reveals after seeing an unsatisfactory draft: content, format, fields, organization, business rules, personalized preferences. Each item must be **independently verifiable** and at the **same level** (no nested sub-requirements); a scene carries **≥ 12** items. This is the main signal for both the judge and the agent's evolution.
3. **trajectory requirements (`trajectory_reqs`)** — constrains/inspects the execution path (tool-call validity and efficiency, allowed information sources, allowed tools/skills; catching hallucination such as fabricating instead of searching). Trajectory requirements are derived *only* from the requirements and must not contradict them.

Content and trajectory are **scored on two separate lines**. Because the judge discloses only the top unmet requirement(s) per turn (§3.4), the query alone is sent to the agent; the checklist stays hidden, reconstructing the "user suddenly remembers one more thing" dynamic.

**Concrete example** (travel scene, warmup Q1, translated): the *query* is one sentence — "Plan a same-day round-trip family itinerary from Hangzhou to Fuyang on 2026-06-01, two adults + one child, no budget limit; save the plan to `result/result_q1`." The 12 *requirements* are the hidden checklist (local weather in a basics module; round-trip transport in a transport module; itinerary split by date and morning/afternoon/evening; child-friendly items flagged; walking distance shown separately and ≤ 3 km/day; all facts date-verified with sources shown; …). The 4 *trajectory requirements* constrain the path (facts must come from authoritative web search, not fabricated; search destination content before planning; save to the specified folder).

### 4.3 The 75% / 25% train-test overlap

For the test set we deliberately require **~75%** of requirements to match the train set and **~25%** to be *variants of* or *contradictions to* train requirements. This ratio is not arbitrary: the highly-overlapping part checks whether the agent applies distilled experience **in the right place**; the 25% variant/contradiction part acts as a mirror that exposes (i) over-fitting to the *literal* train requirements, (ii) mistaking a temporary preference for a universal rule, and (iii) failure to override old experience under a new constraint.

Example: a train requirement "on trips, visit more cultural/historical sites" has, in the test set, a **variant** "when planning routes, add more scenic natural spots" and a **contradiction** "do not include any cultural/historical site visits — I don't like them." Non-generalizable "noise" requirements are strictly confined to appearing sparingly in the **train** set only, so that pseudo-experience is not retroactively rationalized by the test set — preserving the purity of the evolution-ability measurement.

### 4.4 Rubric design and grading

Requirements are engineered for **verifiability, not product quality**, to minimize the judge's randomness on the attempt-count metric. Deterministic requirements are phrased for rule-checkable judgment (fixed headings, fixed CSV columns, fixed metric formulas, numeric thresholds); open-ended parts are graded by the LLM judge against the checklist. The judge is pinned (temperature 0, fixed model, versioned rubric). Cross-runtime neutrality is required: a `query` must not bind to a specific agent's tool naming, to avoid gifting points to any one runtime.

### 4.5 Suite statistics

The released suite has **14 scenes**, each with **4 warmup + 2 holdout = 6 tasks**, totaling **56 warmup + 28 holdout = 84 tasks**, thematically spanning:

| Category | Scenes |
|---|---|
| Travel & trip planning | Travel Planning, Business Travel |
| Shopping / consumer & finance decisions | Healthy-Snack Guide, Cat-Food Guide, Stock Investment Decisions |
| Data analysis | data_analysis, Sales-Ops Weekly Analytics |
| Content creation & document/deck authoring | Xiaohongshu Product Ops, PPT Creation, Startup BP / Investor Roadshow |
| Workplace reporting & ops planning | Weekly-Report Auto-Generation, Team-Building Planning |
| Information retrieval & research | information_search_gathering |
| Learning / education | English-Grammar Learning Guide |

A minimal smoke suite `assets/benchmarks_demo/hello.json` (1 warmup "回复一下你好" + 1 holdout "自我介绍一下你自己") is shipped for framework-level regression testing. Full benchmarks are compiled from external markdown sources via `python -m src.cli.preprocess`.

### 4.6 The minimal contract (for third-party benchmarks)

Any benchmark plugged into LIFT must satisfy: (1) **two-phase schema** — explicit `warmup_tasks[]` / `holdout_tasks[]`, each task carrying `query` / `requirements` / `expected_result.{content_reqs, trajectory_reqs}`; (2) **judge-friendly** — `content_reqs` must be a machine-adjudicable checklist (rules for deterministic parts, LLM rubric for open parts); (3) **cross-runtime neutral** — no tool-name binding; (4) **scenario diversity**.

---

## 5 Implementation

| Topic | Key file | One-liner |
|---|---|---|
| Pipeline orchestration | `src/lift/pipeline/lift_pipeline.py` | repeat × suite × phase multi-level concurrency |
| Adapter contract (base) | `src/lift/adapters/base.py` | abstract `AgentRuntimeAdapter`: `worker_judger_factory` / `evolve_after_warmup` hooks |
| Container orchestration | `src/lift/adapters/container/adapter.py` | `ContainerAgentRuntimeAdapter`: `resolve_docker_image` / `start_container` + default `docker commit` |
| Per-task kernel | `src/lift/eval/run_task.py` | work + judge review loop |
| Container policies | `src/lift/policies/container.py` | warmup / holdout / phase orchestration enums |
| Status visualization | `src/lift/status/` | TUI (rich) + HTTP dashboard |
| Trace backfill | `src/postprocess/trace_backfill.py` | `session_id` correlation (calls `stitch_phase_langfuse_traces`) |
| Data models | `src/models.py` | `Suite` / `EvalReport` / `PhaseRun` / Langfuse trace schema |
| Image build | `agent-runtimes/openclaw/build-image.sh` | base / with-evolve dual artifacts |

**Token accounting.** LIFT enforces a fixed 5-field token schema across all runtimes: `input_tokens` (excl. cache), `cache_write_tokens`, `cache_read_tokens`, `output_tokens` (incl. `reasoning_tokens` as a subset), `reasoning_tokens` (⊂ output). `total_tokens = input + cache_write + cache_read + output` — reasoning is not added again. Evolution cost (`evolve_after_warmup` tokens) is booked as *training* cost, not Loaded inference cost, so EvolutionROI is not under-counted.

---

## 6 Experiments

> `[TODO-DATA]` All numbers in this section are pending the full run. Structure and research questions are fixed; tables are placeholders.

We fix the agent model, work/judge endpoints, `max_conversation_turns`, and container resource passthrough across all cells; the only manipulated variable is Base vs. Loaded (P1). Unless noted, `--repeat 5`, `--max-parallel-suites 20`. We report mean ± std across repeats.

### 6.1 Interaction-efficiency leaderboard

On EALE the `content_score` is close to saturation for most scenes (deterministic checklists, capable underlying LLM), so absolute pass rate is a low-resolution signal of *self-evolution*. What genuinely improves as an agent internalizes artifacts is **the interaction cost of completing the same task**: fewer conversation turns and fewer tool invocations for the same score. The leaderboard therefore ranks runtimes on *interaction-cost reduction*, not on absolute score.

**Metrics.** Let *B* / *E* denote the baseline / evolved phase means (averaged over holdout tasks × `--repeat`):

- ΔTurns% = (E_turns − B_turns) / B_turns
- ΔTools% = (E_tools − B_tools) / B_tools
- **Composite = 0.8 · ΔTurns% + 0.2 · ΔTools%**  (lower is stronger)

Turns dominate the composite because they map most directly to user-perceived latency. Both baseline and evolved `content_score` are reported alongside for transparency but do *not* participate in ranking; no pass-rate gate is applied, so regressions (positive Δ) surface directly in the composite. Turns and tool-call counts are sourced from the postprocess CSV columns `trials` / `tool_use_num` (Langfuse-derived), aggregated per-runtime.

> **[MOCK TABLE — illustrative structure only; all numbers are placeholders pending the full `--repeat 5` run.]** Interaction-Efficiency Leaderboard across the nine runtimes reported in the paper. Ranked by Composite ↑ (lower is stronger).

| Rank | Runtime | Category | Score (B / E) | Turns B | Turns E | ΔTurns% | Tools B | Tools E | ΔTools% | Composite |
|:---:|---|---|:---:|:---:|:---:|---:|:---:|:---:|---:|---:|
| 1 | `[TODO]` | Augmented | -- / -- | -- | -- | -- | -- | -- | -- | -- |
| 2 | `[TODO]` | Augmented | -- / -- | -- | -- | -- | -- | -- | -- | -- |
| 3 | `[TODO]` | Augmented | -- / -- | -- | -- | -- | -- | -- | -- | -- |
| 4 | `[TODO]` | Augmented | -- / -- | -- | -- | -- | -- | -- | -- | -- |
| 5 | `[TODO]` | Implicit | -- / -- | -- | -- | -- | -- | -- | -- | -- |
| 6 | `[TODO]` | Implicit | -- / -- | -- | -- | -- | -- | -- | -- | -- |
| 7 | `[TODO]` | Implicit | -- / -- | -- | -- | -- | -- | -- | -- | -- |
| 8 | `[TODO]` | Implicit | -- / -- | -- | -- | -- | -- | -- | -- | -- |
| 9 | `[TODO]` | Implicit | -- / -- | -- | -- | -- | -- | -- | -- | -- |

Runtimes entering the leaderboard — *Implicit* (natural state accumulation during warmup): `openclaw`, `genericagent`, `hermes`, `openhuman`, `evoscientist`; *Augmented* (explicit reflection / skill hub / external memory / AutoSkills after warmup): `openclaw_with_openspace`, `openclaw_with_agentmemory`, `genericagent_active_evolve`, `hermes_with_openspace`, `hermes_with_agentmemory`, `openhuman_with_agentmemory`, `evoscientist_active_evolve`.

**Caveat.** Tool-call counts for runtimes without a native counter (EvoScientist, OpenHuman) are populated via Langfuse `trace_backfill`; if the trace overlay is unavailable, the cell is annotated `n/a` and that runtime is scored on ΔTurns% alone.

### 6.2 Research questions

**RQ1 — Do evolved artifacts help on unseen tasks?** Base vs. Loaded across the 14 scenes.

| Scene | Base FinalPassRate | Loaded FinalPassRate | DownstreamPassDelta | Base FirstRound | Loaded FirstRound |
|---|---|---|---|---|---|
| (14 rows) | `[TODO-DATA]` | `[TODO-DATA]` | `[TODO-DATA]` | `[TODO-DATA]` | `[TODO-DATA]` |
| **Macro-avg** | `[TODO-DATA]` | `[TODO-DATA]` | `[TODO-DATA]` | `[TODO-DATA]` | `[TODO-DATA]` |

**RQ2 — Do augmentation hooks (skill hub / external memory / active reflection) add value on top of implicit evolution?** `openclaw` vs. its augmented siblings (`openclaw_with_openspace`, `openclaw_with_agentmemory`); `hermes` vs. `hermes_with_openspace` / `hermes_with_agentmemory`; `openhuman` vs. `openhuman_with_agentmemory`; `genericagent` vs. `genericagent_active_evolve`; `evoscientist` vs. `evoscientist_active_evolve`. Each pair holds the base runtime fixed and toggles exactly one augmentation. `[TODO-DATA]`

**RQ3 — Is the delta stable across repeats?** Variance of DownstreamPassDelta over `--repeat` on one suite; report std and CI. `[TODO-DATA]`

**RQ4 — Cross-runtime bare baseline and Loaded delta.** On the same benchmark, per-runtime (`openclaw`, `hermes`, `openhuman`, `evoscientist`, …) bare baseline and Loaded delta. Per §7, we do **not** compare runtimes head-to-head; each is its own Base-vs-Loaded, and we compare *delta magnitudes*. `[TODO-DATA]`

**RQ5 — Cross-scene transfer (ablation).** Artifacts evolved on scene A applied to scene B tasks. `[TODO-DATA]`

**RQ6 — Cost view.** TotalTokens and EvolutionROI; `impr_metric` on {attempts, tool calls, tokens}. `[TODO-DATA]`

**Efficiency observation (preliminary, single-suite integration runs).** In integration-check runs on the `evoscientist_active_evolve` runtime, evolved reached the same 100% pass rate as baseline while reducing tokens, latency, and conversation turns — consistent with LIFT's efficiency-first hypothesis. Full-suite numbers are `[TODO-DATA]`.

---

## 7 Discussion and Limitations

1. **Benchmark data contamination.** LIFT cannot prevent the gold set from being contaminated by LLM training data; ABC-Checklist-style tools must gate this at benchmark-design time.
2. **No direct cross-agent comparison.** We deliberately do **not** support head-to-head *OpenClaw+plugin vs. Hermes* comparison — too many confounds make conclusions non-attributable. The correct move is per-agent Base vs. Loaded, then compare delta magnitudes.
3. **Evolution ≠ artifacts.** LIFT is artifact-source-agnostic by default. To isolate "the evolution *mechanism* has value," use the attribution triplet (No-Evo / Only-Products / Evo-On) — an optional appendix, not a required metric.
4. **Judge bias.** When both work and judge are LLMs, self-preference is a risk. Current mitigation: the judge role-plays "the user," adjudicates the `content_reqs` checklist item-by-item into a single completion ratio, and is pinned (temperature 0, fixed model, versioned rubric); work and judge are additionally split into sibling containers (§3.4) so self-preference cannot compound with state leakage — neither party can read or write the other's runtime state. Making deterministic requirements fully machine-checkable — shrinking the subjective surface further — is future work.
5. **Environment fidelity.** The container is still a *controlled Linux environment*; fidelity gaps remain versus desktop-GUI / browser agents — future work.
6. **Evolution-cost attribution.** `evolve_after_warmup` tokens are training cost, not Loaded inference cost; LIFT books them separately so EvolutionROI is not under-counted.

---

## 8 Conclusion

Our stance in one sentence: **evaluating a self-evolving agent is not about how much better it gets on tasks it has already seen, but how much better it gets on *unseen* tasks by virtue of its distilled artifacts.** LIFT realizes this with train/test separation + same-task paired runs + container snapshots + a three-layer abstraction + multi-level concurrency + trace backfill, and ships a self-contained 14-scene / 84-task benchmark. We hope to provide a reusable, criticizable, comparable base for evaluation infrastructure.

---

## Reproducibility & Release

- **Code**: framework, twelve runtime adapters, status dashboard, post-processing.
- **Images**: `lift-openclaw-base` / `lift-openclaw-with-openspace` / `lift-openclaw-with-agentmemory` and `lift-{genericagent,hermes,hermes-with-openspace,hermes-with-agentmemory,openhuman,openhuman-with-agentmemory,evoscientist}:latest` build scripts.
- **Data**: 14-scene benchmark (Croissant metadata + hosted repository `[TODO-HOSTING]`), plus the demo smoke suite in-repo.
- **Entry points**: `python -m src.cli.lift_main -r <runtime> --benchmark_dir <dir> --suite <name>.json --run_id <id>`; `--evaluate-only` for post-process replay.

## Appendix A — 32 surveyed open-source works (by type)

`[carry over from the design draft; to be finalized with citations]` Surveys (6); Methods/Judge (4): ABC Checklist, Agent-as-a-Judge, MAJ-Eval, AgentDistill; Evolution/artifact benchmarks (5): SEA-Eval, EvolveTool-Bench, SkillsBench, EvoMemBench, Evo-Memory; Lifelong/sequential (1): LifeLongAgentBench; General task benchmarks (10): REAL, MLR-bench, AgentIF, τ-bench/τ²-bench, AgentBench, GAIA, SWE-bench, and harness-comparison benchmarks; Safety (3): OS-Harm, RAS-Eval, MultiBreak; SDKs (3): DeepEval, Opik, Promptfoo.

## Appendix B — LIFT vs. existing work (relation matrix)

| Dimension | LIFT's stance | Direct contrast |
|---|---|---|
| Artifact source | source-agnostic; test whether the artifact itself helps | SkillsBench empirically shows self-generated skills often useless; LIFT provides the infrastructure to test this |
| Causal contrast | Base vs. Loaded, same-task paired | most benchmarks lack this control group |
| Train / test | explicit separation (warmup/holdout) | most self-evolution papers mix them |
| Per-task scoring | work + judge review loop | borrows Agent-as-a-Judge tool-augmented verification |
| Artifact materialization | `docker commit` → delta image | more reproducible than toggle-load, more complete than structured export |
| Learning curves | evolution-specific diagnostic (optional) | LifeLongAgentBench makes FWT/BWT primary; LIFT makes them diagnostic |
| Cross-agent comparison | per-agent Base-vs-Loaded, compare deltas; no head-to-head | harness-comparison benchmarks compare bare agents |
| Safety | cross-cutting, optional | OS-Harm / RAS-Eval provide dedicated safety benchmarks; LIFT does not redo them |
| Trace | Langfuse backfill | like Opik, but trace-only, no evaluator |
