# arXiv v1 Submission Checklist — LIFT

This is the operational checklist for submitting **v1** of the LIFT paper to arXiv as a preprint. The companion NeurIPS submission requires a **separate build** (see §5 below).

## 1. Authors (final, three-author equal-contribution block)

| Name | Affiliation | Email |
|---|---|---|
| Lin Yu | Independent Researcher | `yulin.jay@gmail.com` |
| Tong Han | Independent Researcher | `iklare_hans@outlook.com` |
| Linsheng Zheng | Independent Researcher | `zzuzhangwen@aliyun.com` |

- Equal contribution; author order arbitrary.
- All three submit under personal capacity (no institutional affiliation on the byline).
- Rendered in `main.tex` via `\author{...\And...\And...}` with a shared `\thanks` note.

## 2. arXiv metadata (fill in the submission form)

| Field | Value |
|---|---|
| Title | LIFT: A Counterfactual Framework and Benchmark for Disentangling Genuine Self-Evolution in LLM Agents |
| Authors | Lin Yu, Tong Han, Linsheng Zheng |
| Primary category | **cs.AI** (Artificial Intelligence) |
| Cross-lists | **cs.LG** (Machine Learning), **cs.SE** (Software Engineering) |
| MSC / ACM class | Leave blank |
| Comments | `Preprint v1. 30 pages incl. appendix. Companion benchmark released at https://huggingface.co/datasets/FeiZhuNiU-INFJA/EALE. §6 reports population-ratio Δ from a partial cross-runtime sweep; full-suite results in v2.` |
| Report number | Leave blank |
| Journal reference | Leave blank |
| DOI | Leave blank (arXiv will mint one) |
| License | **CC BY 4.0** (recommended for max reusability of a benchmark paper); alternative: `arXiv non-exclusive license to distribute` if institution mandates it |

## 3. Endorsement & first-time submitter

- All three authors submit under personal (independent) capacity. First-time submitters in `cs.AI` require an **endorser** — arXiv will surface this after registration if triggered.
- Endorsement path: ask any colleague with recent `cs.AI` / `cs.LG` submissions (e.g. former co-workers at ByteDance who publish externally) to endorse via the arXiv endorsement portal. Endorsement is one-shot per category; once granted, all three can submit freely to `cs.AI`.
- Preferred submitter: whoever gets endorsement first. The other two are added as co-authors in the submission form (they don't each need to be endorsers).

## 4. Pre-flight (before hitting "Submit")

- [ ] `docs/neurips/main.pdf` compiles cleanly with the non-anonymous author block (already verified with tectonic; 133.68 KiB, 30 pages).
- [ ] Abstract's "Preprint status" sentence names v1 and promises a v2 with the full sweep — matches the actual data state in §6.
- [ ] Every table in §6 uses population-ratio Δ; no per-task-ratio residue remains. (Cross-checked against `scripts/extract_paper_tables.py`.)
- [ ] `refs.bib` compiles without warnings (a few `Underfull \hbox` warnings are typographic, not blocking).
- [ ] Code URL (`github.com/FeiZhuNiU-INFJA/LIFT`) and data URL (`huggingface.co/datasets/FeiZhuNiU-INFJA/EALE`) resolve.
- [ ] `.tex` + `refs.bib` + `neurips_2023.sty` bundle assembles for arXiv upload (arXiv compiles from source; do not upload only the PDF).

**Ready-to-upload tarball:** `docs/neurips/arxiv-v1-source.tar.gz` (29 KiB, verified to compile cleanly in an isolated `tectonic` sandbox → 134 KiB PDF). Contains `lift-arxiv-v1/{main.tex, refs.bib, neurips_2023.sty}`. Upload this directly to arXiv's "New Submission" form.

## 5. NeurIPS 2027 submission — separate build

The v1 arXiv build is **non-anonymous**. The NeurIPS submission requires a distinct build with:

1. Replace `\usepackage[preprint]{neurips_2023}` with `\usepackage{neurips_2023}` (anonymous).
2. Delete the `\author{...}` block; NeurIPS's `.sty` renders "Anonymous authors."
3. Strip the two URLs from the abstract's last sentence:
   `Code is released at ... and the benchmark at ...` → replace with `Code and data will be released upon acceptance.`
4. Remove the "Preprint status" sentence from the abstract (belongs on arXiv only).
5. Search-and-remove any accidental self-citation of the v1 arXiv preprint (currently none).
6. Track: **Datasets & Benchmarks** (matches the paper's positioning: framework + benchmark suite).

NeurIPS 2027 policy allows arXiv preprints predating submission; do not publicize the arXiv version on social media during the review window (author-anonymity guidance).

## 6. After v1 goes live

- Add the arXiv link to the GitHub README and the HuggingFace dataset card.
- When v2 is ready (full experimental sweep), update via arXiv's "replace" flow (same submission ID, new PDF + source).
