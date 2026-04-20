# Skills

This folder contains the AI prompts and deterministic rules that power SpecDrift's analysis pipeline. It is designed to be the primary place for open source contributors to tune the tool's behavior without touching Python code.

## Files

| File | Purpose |
|------|---------|
| `drift_detection.md` | System prompt for per-pair spec drift analysis |
| `batch_analysis.md` | System prompt for batched multi-pair analysis |
| `judge.md` | System prompt for the false-positive judge pass |
| `summary.md` | System prompt for the executive summary generation |
| `config.yaml` | Deterministic rules: noise filters, intent grading thresholds, stopwords |

## How prompts are loaded

Each `.md` file is loaded at startup by `analyzer.py` using `_load_skill(filename, fallback)`. If a file is missing, the hardcoded fallback string is used instead, so the tool is always runnable even with a partial skills folder.

## Tuning prompts

Edit the `.md` files directly. The full file content becomes the system prompt for that LLM call. Keep prompts concise — the tool runs many LLM calls per analysis and bloated prompts slow things down and increase token cost.

Guidelines:
- Always end with `Return JSON only. No markdown.` or equivalent — the pipeline parses raw JSON from the model output.
- Be conservative in drift detection prompts. False positives erode trust in the report. Fewer precise findings beat many vague ones.
- Test changes by running the tool against a small repo (30-day window, a few dozen issues) and inspecting the HTML report.

## Tuning config.yaml

`config.yaml` controls three deterministic (non-LLM) stages:

**`noise`** — Issues matching these patterns are dropped before any LLM call. Add patterns here to skip low-signal issue types like dependency bumps or typo fixes.

**`intent_grading`** — Each issue is scored to decide how deeply to analyze it:
- Score >= `rich_threshold` (default 4): full analysis, up to 4 requirements extracted
- Score >= `medium_threshold` (default 2): simplified analysis, up to 2 requirements
- Score < `medium_threshold`: skipped entirely

Adjust scoring weights and thresholds to control the analysis depth vs. cost tradeoff.

**`false_link_stopwords`** — Words ignored when checking if an issue and PR are actually related by topic. Extend this list if you see false links slipping through.

## Contributing

Pull requests that improve detection accuracy, reduce false positives, or add noise patterns for common issue types are very welcome. Please include a brief description of what repo or issue type motivated the change.
