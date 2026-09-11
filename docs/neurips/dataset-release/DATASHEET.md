# Datasheet for EALE (LIFT Benchmark)

Following *Datasheets for Datasets* (Gebru et al., 2021). This datasheet documents the
**EALE** dataset (*Evaluating Agent Loaded Evolution*), the benchmark suite of the **LIFT**
framework (*Loaded Impact on Final Task*).

- **Dataset:** https://huggingface.co/datasets/FeiZhuNiU-INFJA/EALE
- **Code:** https://github.com/FeiZhuNiU-INFJA/LIFT
- **License:** CC BY 4.0

---

## Motivation

**For what purpose was the dataset created?**
To measure whether a *self-evolving* LLM agent genuinely improves on **unseen** tasks after
consolidating reusable artifacts (memory / skills / SOPs) from **warmup** tasks — isolating
*genuine* generalization from *pseudo* performance growth caused by evaluation confounds
(training-memory leakage, cross-session context pollution, grader self-preference,
non-reproducible runtimes). Existing agent benchmarks measure raw one-shot capability and
lack the Base-vs-Loaded control needed to attribute a gain to the artifacts themselves.

**Who created the dataset and on behalf of whom?**
The authors of the LIFT paper (anonymized for review).

**Who funded the creation of the dataset?**
To be disclosed in the camera-ready version.

---

## Composition

**What do the instances represent?**
Each instance is a **task**: a folder containing (i) a task markdown with a colloquial *query*,
a hidden *requirements* checklist (≥ 12 independently-verifiable items per scene), and
*trajectory requirements* (constraints on the execution path); and (ii) input *materials*
(CSV tables, documents) the task operates on.

**How many instances are there in total?**
**14 scenes × (4 warmup + 2 holdout) = 84 tasks** (56 warmup + 28 holdout), plus a 2-task
demo smoke suite shipped in the code repo (`hello.json`).

**Does the dataset contain all possible instances or is it a sample?**
It is a curated sample of realistic assistant scenarios; it is not exhaustive of any population.

**What data does each instance consist of?**
Human-readable markdown (task spec) + raw input files. There are no pre-computed labels; the
"label" is the *requirements checklist*, applied at evaluation time by a work-agent + judge-agent
review loop.

**Is there a label or target associated with each instance?**
Yes — the requirements checklist (content requirements + trajectory requirements) is the
grading target. Scoring is `score = satisfied_requirements / total_requirements` (1 on success).

**Is any information missing from individual instances?**
No essential fields are missing; some scenes intentionally under-specify the *query* (the
checklist is disclosed only progressively during evaluation, by design).

**Are there recommended data splits?**
Yes and they are load-bearing: **train/** = warmup (used to evolve the agent), **test/** = holdout
(used for the paired Base-vs-Loaded contrast). Train and test requirements overlap ~75%, with the
remaining ~25% being deliberate variants/contradictions to expose over-fitting.

**Are there errors, sources of noise, or redundancies?**
Non-generalizable "noise" requirements are *deliberately* confined to the train set only, so the
test set cannot retroactively rationalize pseudo-experience. Grader randomness is minimized by
authoring requirements for rule-checkability.

**Is the dataset self-contained?**
The task specs and materials are self-contained. Some *trajectory requirements* expect the agent
to perform live web search (facts must be sourced, not fabricated); reproducing those exactly
depends on the agent's tool environment, not on the dataset.

**Does the dataset contain data that might be confidential / offensive / sensitive?**
No. All input materials (call records, surveys, financials, inventories, etc.) are **synthetic**,
authored for the benchmark. No real personal data is included.

---

## Collection Process

**How was the data collected / produced?**
Tasks were **expert-authored** by the research team, each reconstructing a realistic interaction
(user asks vaguely, then clarifies) and engineered for *verifiability*. Input materials were
synthesized to fit each task.

**What mechanisms or procedures were used?**
Manual authoring against an authoring spec (`assets/suite_requirement.md`): three-part task
anatomy, ≥ 12 same-level verifiable requirements, cross-runtime-neutral phrasing, and the
75%/25% train-test overlap rule.

**Over what timeframe was the data collected?**
During LIFT framework development (2026).

**Were any ethical review processes conducted?**
Not applicable — no human subjects and no real personal data.

---

## Preprocessing / Cleaning / Labeling

**Was any preprocessing done?**
The human-readable markdown tree is compiled into machine-readable suite JSON
(`Suite` in `src/models.py`) by `python -m src.cli.preprocess`. The raw markdown/materials are
retained and released as the source of truth.

**Is the raw data available?**
Yes — the raw markdown + materials tree is the released artifact.

---

## Uses

**What (other) tasks could the dataset be used for?**
Evaluating agent memory systems, skill-acquisition mechanisms, SOP distillation, and lifelong /
continual-learning diagnostics (FWT/BWT-style) — always under the Base-vs-Loaded protocol.

**Is there anything about the composition or collection that might impact future uses?**
Several scenes are locale-specific (e.g., Chinese consumer/finance scenarios); results should not
be over-generalized across cultures. The benchmark measures *artifact usefulness*, not safety.

**Are there tasks for which the dataset should not be used?**
It should not be used as a raw one-shot capability leaderboard, nor for head-to-head agent
ranking without the paired Base-vs-Loaded control.

---

## Distribution

**How is the dataset distributed?**
Public Hugging Face dataset repository (mirror on ModelScope); the framework code is on GitHub.

**License / Terms of use.**
**CC BY 4.0.** The framework code is released under the repository's code license.

**Croissant metadata.**
Hugging Face auto-generates Croissant metadata (`conformsTo: mlcommons/croissant 1.1`). Because
EALE is file-structured rather than a single table, the auto-generated `recordSet` is not
populated; the authoritative structure is documented in this datasheet and the dataset card.

---

## Maintenance

**Who will support/host/maintain the dataset?**
The authors, via the GitHub and Hugging Face repositories (contact to be disclosed in
camera-ready).

**Will the dataset be updated?**
Yes — corrections and additional scenes will be released as versioned updates on Hugging Face,
with changes noted in the repository.

**Will older versions continue to be supported?**
Prior revisions remain accessible via the Hugging Face git history.

**How can others contribute / extend?**
Third parties can add scenes that satisfy the minimal authoring contract (train/test folders,
≥ 12 verifiable requirements, 75%/25% overlap); see the paper's "minimal contract" section and
`assets/suite_requirement.md`.
