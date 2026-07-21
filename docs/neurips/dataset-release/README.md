---
license: cc-by-4.0
language:
  - en
  - zh
pretty_name: "EALE: Evaluating Agent Loaded Evolution (LIFT Benchmark)"
tags:
  - agent
  - llm-agent
  - self-evolving-agent
  - agent-evaluation
  - benchmark
task_categories:
  - other
size_categories:
  - n<1K
configs:
  - config_name: default
    data_files:
      - split: train
        path: "benchmark_mds/**/train/**"
      - split: test
        path: "benchmark_mds/**/test/**"
annotations_creators:
  - expert-generated
source_datasets:
  - original
---

# EALE — Evaluating Agent Loaded Evolution

**EALE** is the benchmark suite of the **LIFT** framework (*Loaded Impact on Final Task*).
It is designed to measure whether a **self-evolving LLM agent** actually does better on
**unseen** tasks *after* it has consolidated reusable artifacts (memory / skills / SOPs)
from a set of **warmup** tasks — not to measure the agent's out-of-the-box ability.

- **Paper:** *LIFT: A Counterfactual Framework and Benchmark for Disentangling Genuine Self-Evolution in LLM Agents*
- **Code / framework:** https://github.com/FeiZhuNiU-INFJA/LIFT
- **License:** CC BY 4.0

> ⚠️ **This is not a single flat table.** EALE is a *file-based, task-structured* benchmark:
> each task is a folder containing a human-readable task markdown plus its input materials
> (CSV / documents). The Hugging Face auto-loader (`load_dataset`) therefore cannot cast it
> into one tabular schema; consume it by cloning the repository tree, not via `load_dataset`.
> See **Usage** below.

## Dataset structure

Three layers: **total dataset → scene datasets → train/test tasks**.

```
benchmark_mds/
└── <scene>/                       # 14 scenes total
    ├── train/                     # 4 warmup tasks (drive artifact evolution)
    │   ├── q1_<shortname>/
    │   │   ├── q1_<shortname>.md   # task: query + requirements + trajectory requirements
    │   │   └── q1_materials/       # input files (csv/docs) for this task
    │   └── ...                     # q1..q4
    ├── test/                      # 2 holdout tasks (the LIFT paired Base-vs-Loaded contrast)
    │   ├── q5_<shortname>/ ...
    │   └── q6_<shortname>/
    └── skills/                    # optional scene-level seed skill
```

- **14 scenes × (4 warmup + 2 holdout) = 84 tasks** (56 warmup + 28 holdout).
- Each task markdown has three parts:
  - **query** — a colloquial, deliberately under-specified first instruction.
  - **requirements** — the hidden acceptance checklist (≥ 12 independently-verifiable items per scene).
  - **trajectory requirements** — constraints on the execution path (tool-call validity/efficiency, allowed sources).
- **Train/test requirement overlap is deliberately ~75% / 25%**: 75% of test requirements match train
  (does the agent apply distilled experience *in the right place*?), 25% are *variants of* or
  *contradictions to* train requirements (exposing over-fitting to literal training requirements).

## Scenes

Travel Planning · Business Travel · Healthy-Snack Guide · Cat-Food Guide ·
Stock Investment Decisions · data_analysis · Sales-Ops Weekly Analytics ·
Xiaohongshu Product Ops · PPT Creation · Startup BP / Investor Roadshow ·
Weekly-Report Auto-Generation · Team-Building Planning ·
information_search_gathering · English-Grammar Learning Guide.

## Languages

Bilingual: task queries/requirements and input materials are in **English (`en`)** and
**Chinese (`zh`)** depending on the scene.

## Usage

```bash
# Recommended: clone the repository tree (do NOT rely on load_dataset)
huggingface-cli download FeiZhuNiU-INFJA/EALE --repo-type dataset --local-dir ./EALE

# Or via the LIFT framework's preprocessor, which compiles the markdown tree
# into machine-readable suite JSON (assets/benchmarks/*.json):
python -m src.cli.preprocess          # BENCHMARK_SOURCE=huggingface
```

## Intended use & scope

- **In scope:** benchmarking the *marginal benefit of an agent's self-evolution*
  (Base vs. Loaded on held-out tasks); efficiency-first metrics (attempts / tool calls / tokens).
- **Out of scope:** measuring an agent's raw one-shot capability; head-to-head agent leaderboards.

## Curation & known limitations

- **Curation:** tasks are expert-authored to be *verifiable* (rule-checkable headings, fixed CSV
  columns, numeric thresholds) rather than optimized for product polish, to minimize grader variance.
- **Limitations:** requirements reflect the authors' domain assumptions; some scenes are
  culture/locale-specific (e.g., Chinese consumer scenarios); the benchmark measures *artifact
  usefulness*, not safety (safety regression is an optional, orthogonal dimension in LIFT).
- **Personal/sensitive data:** input materials (call records, surveys, financials, etc.) are
  **synthetic**, authored for the benchmark; they do not contain real personal data.

## Citation

```bibtex
@misc{lift2027,
  title        = {LIFT: A Counterfactual Framework and Benchmark for Disentangling Genuine Self-Evolution in LLM Agents},
  author       = {Anonymous},
  year         = {2027},
  howpublished = {\url{https://github.com/FeiZhuNiU-INFJA/LIFT}},
  note         = {Dataset: \url{https://huggingface.co/datasets/FeiZhuNiU-INFJA/EALE}}
}
```
