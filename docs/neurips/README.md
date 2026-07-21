# LIFT paper — `docs/neurips/`

NeurIPS-formatted source of the LIFT paper.

- [`main.tex`](main.tex) — paper body (English, pdfLaTeX-compatible, no CJK).
- [`refs.bib`](refs.bib) — bibliography (arXiv-verified).
- [`neurips_2023.sty`](neurips_2023.sty) — official NeurIPS 2023 style (kept in-tree).
- [`build.sh`](build.sh) — build script.
- [`dataset-release/`](dataset-release/) — EALE HuggingFace dataset card + datasheet (separate release channel).

`main.pdf` is **not** committed. Milestone PDFs are published on GitHub Releases; contributors rebuild locally.

## How to work with the paper

Ask an agent to invoke the [`lift-paper`](../../skill/lift-paper/SKILL.md) skill. It automates:

- **Build the paper** — LaTeX toolchain setup + one-shot compile (`bash build.sh` → `main.pdf`).
- **Live preview while editing** — either `build.sh --watch` (any editor) or VS Code / Trae LaTeX Workshop with SyncTeX.

Typical prompts: *"build the paper"*, *"set up LaTeX preview"*.

Releasing a milestone PDF to GitHub Releases is documented in the skill (Chapter 4) but must be run by a human — it involves `git tag` + `git push` + uploading `main.pdf` on the GitHub Releases UI, none of which the agent can do on your behalf.
