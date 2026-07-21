---
name: "lift-paper"
description: "One-stop skill for the LIFT NeurIPS paper (docs/neurips/): environment setup (install tectonic without polluting repo root), one-shot builds, live rebuild while editing, VS Code / Trae live-preview configuration, and release publishing. Invoke when a user asks to build main.pdf, preview the paper, set up LaTeX, run `build.sh`, or configure their editor for the paper. Applies only when working with docs/neurips/."
---

# LIFT paper workflow

Everything a contributor needs to write, compile, preview, and ship the LIFT NeurIPS paper (`docs/neurips/`). Covers first-time environment setup, day-to-day editing loops, and milestone releases.

## When to invoke

Any of these:
- "How do I build the paper / generate main.pdf?"
- "How do I preview `main.tex` live?"
- "`bash docs/neurips/build.sh` says `no LaTeX engine found`."
- "I just cloned the repo, how do I set up LaTeX?"
- "Configure my editor (VS Code / Trae / Cursor) for the paper."
- "How do I release the paper PDF?"
- Any mention of `tectonic`, `latexmk`, `.tex`, `neurips_2023.sty` **combined with** the LIFT repo.

**Do not** invoke for unrelated LaTeX documents outside `docs/neurips/`.

## Layout

```
docs/neurips/
├── main.tex             # paper body (English, pdfLaTeX-compatible, no CJK)
├── refs.bib             # bibliography (arXiv-verified)
├── neurips_2023.sty     # official conference style (kept in-tree)
├── build.sh             # ← the build script; the skill wires everything around it
├── main.pdf             # ← NOT committed; produced by build.sh; released via GitHub Releases
└── dataset-release/     # HF dataset card + Gebru datasheet (separate release channel)
```

## Chapter 1 — First-time environment setup

Prerequisite for chapters 2, 3, 4. **Only needed once per machine.**

### Detect current state

```bash
command -v tectonic && tectonic --version || echo "tectonic not on PATH"
command -v latexmk && echo "latexmk present (build.sh will fall back to it)"
ls -la <repo_root>/tectonic 2>/dev/null && echo "WARN: stray binary at repo root"
```

Branches:
- **tectonic on PATH** → skip to Chapter 2, done.
- **latexmk on PATH, no tectonic** → skip to Chapter 2, done. `build.sh` uses `latexmk`.
- **stray `tectonic` at repo root** → skip installer, go to "Put tectonic on PATH".
- **nothing** → install first.

### Install tectonic

Pick by OS. **Never run the Linux installer from the repo root** — always `cd /tmp` first, otherwise the binary lands in the current directory (common trap).

```bash
# macOS
brew install tectonic

# Linux (single ~55MB binary)
cd /tmp && curl --proto '=https' --tlsv1.2 -fsSL https://drop-sh.fullyjustified.net | sh
# binary is now at /tmp/tectonic — continue to "Put tectonic on PATH"

# Windows (PowerShell)
winget install TectonicProject.Tectonic
```

Fallbacks: if `curl | sh` is blocked, download a release asset from
https://github.com/tectonic-typesetting/tectonic/releases and continue below.

### Put tectonic on PATH

`brew` and `winget` handle PATH themselves; the Linux tarball path does not.

**Option A — system-wide (recommended, needs sudo):**
```bash
sudo mv <source_path> /usr/local/bin/tectonic && sudo chmod +x /usr/local/bin/tectonic
```

**Option B — user-only (no sudo):**
```bash
mkdir -p ~/.local/bin
mv <source_path> ~/.local/bin/tectonic && chmod +x ~/.local/bin/tectonic
case ":$PATH:" in *":$HOME/.local/bin:"*) ;;
  *) echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
     [ -f ~/.zshrc ] && echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
     echo "reload with: source ~/.bashrc  (or ~/.zshrc)";;
esac
```

`<source_path>` is either `/tmp/tectonic` (fresh Linux install) or `<repo_root>/tectonic` (the stray-at-root case). **Always confirm with the user before `sudo`.**

### Verify

```bash
tectonic --version                 # prints Tectonic 0.x.y
bash docs/neurips/build.sh         # first run downloads the TeX bundle (~200MB, cached forever)
ls -la docs/neurips/main.pdf       # created if build succeeded
```

## Chapter 2 — One-shot build (day-to-day)

The default action any time you edited `main.tex` or `refs.bib`.

```bash
bash docs/neurips/build.sh
```

Behavior:
- Prefers `tectonic`, falls back to `latexmk`.
- Writes `main.pdf` in place.
- `main.pdf` is `.gitignore`'d — will never be committed by accident.
- Intermediate files (`.aux/.bbl/.log/.synctex.gz/.fls/.fdb_latexmk`) are also ignored.

If it fails:
- **`no LaTeX engine found`** → go back to Chapter 1.
- **network error on first run** → the TeX bundle download is resumable; just re-run.
- **LaTeX error in a specific line** → the message points at the source line in `main.tex`; fix and rerun.

## Chapter 3 — Live rebuild while editing

Two flavors. Pick one; don't run both simultaneously.

### 3a. Editor-agnostic watch mode (works with any editor including Trae)

```bash
bash docs/neurips/build.sh --watch
```

Polls `main.tex` / `refs.bib` mtime every second. On change, rebuilds `main.pdf`. Open `main.pdf` in your editor's preview tab or any PDF viewer that auto-reloads — it refreshes on every save.

Stop with `Ctrl+C`. This is the most portable option; no plugins, no per-repo config.

### 3b. VS Code / Trae / Cursor with LaTeX Workshop plugin

Better UX than `--watch` because it adds **SyncTeX** (Ctrl/Cmd+click a line in `main.tex` → jumps to that point in the PDF, and vice versa) and inline error markers.

**One-time setup:**

1. Install the extension **LaTeX Workshop** (`james-yu.latex-workshop`) from the marketplace. Trae and Cursor use the same VS Code extension ecosystem, so it works there too.

2. Copy the template `vscode-settings.json` (shipped alongside this skill file) into your local `.vscode/settings.json`:

   ```bash
   mkdir -p .vscode
   cp skill/lift-paper/vscode-settings.json .vscode/settings.json
   ```

   `.vscode/settings.json` is per-user and gitignored on purpose — it's a personal preference, not a project artifact.

3. Open `main.tex`, press **Ctrl/Cmd+S**. A PDF tab opens to the right and refreshes on every save.

**If your editor is not a VS Code fork** (e.g., IntelliJ, TeXstudio, Emacs), use flavor 3a instead — the extension ecosystem differs, and mixing configs causes confusion.

## Chapter 4 — Release the PDF (milestone builds)

`main.pdf` is not in git. External readers get it via GitHub Releases.

1. Verify the current source builds clean:
   ```bash
   bash docs/neurips/build.sh
   ```
2. Tag the commit (semantic: `paper-vN`, `submission-<venue>-<year>`, etc.):
   ```bash
   git tag paper-v0.3 -m "Draft revision N"
   git push origin paper-v0.3
   ```
3. On GitHub → **Releases → Draft a new release** → pick the tag → upload `docs/neurips/main.pdf` as an asset → publish.
4. Update any shared link (Feishu doc, Slack message, arXiv note) to point at the new release.

**Anonymization reminder for double-blind submissions:** before tagging a submission release, temporarily replace the GitHub / HuggingFace `\url{...}` links in `main.tex` (Abstract + Reproducibility) with `anonymous.4open.science` mirrors. Revert after acceptance.

## Chapter 5 — Common operations & pitfalls

| Task | Command / Action |
|---|---|
| Full rebuild from clean state | `rm -f docs/neurips/main.aux docs/neurips/main.bbl && bash docs/neurips/build.sh` |
| Check what will be committed (make sure no PDF sneaks in) | `git status docs/neurips/` |
| Move stray tectonic out of repo root | `mv ./tectonic ~/.local/bin/` |
| Upload sources to Overleaf / Papeeria | Zip `main.tex refs.bib neurips_2023.sty` (do NOT include `main.pdf` or `.sty`-less setup); upload the zip |
| Anonymize links for review | Search `\url{https://github.com/FeiZhuNiU-INFJA/LIFT}` and `\url{https://huggingface.co/datasets/FeiZhuNiU-INFJA/EALE}` → replace with anonymized mirrors |
| Add a new citation | Add BibTeX entry to `refs.bib` → cite with `\citep{key}` in `main.tex` → `bash build.sh` twice (bibtex needs a second pass; `tectonic` handles this in one command) |

### Hard rules

1. **Never** commit `main.pdf` or any `.aux/.bbl/.log/.synctex.gz/.fls/.fdb_latexmk`. `.gitignore` is set up correctly; don't fight it.
2. **Never** leave a `tectonic` binary at the repo root when finished.
3. **Never** run `sudo` without explicit user consent.
4. **Never** edit `build.sh`, `.gitignore`, or paper source when the user only asked for build/preview/release actions. Ask first.
5. `main.tex` is English-only, no CJK. If the user drafts a Chinese passage, translate before committing.
