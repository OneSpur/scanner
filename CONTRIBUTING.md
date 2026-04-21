# Contributing to 1Spur Scanner

We want to make contributing to this project as easy and transparent as possible.

## Pull Requests

We actively welcome your pull requests.

1. Fork the repo and create your branch from `main`.
2. Make your code change.
3. Update the [README.md](/README.md) if you've changed functionality or added features.
4. Ensure your code follows the existing style (type hints, async/await for I/O, docstrings for non-obvious logic).
5. Address any feedback in code review promptly.

## What to Contribute

### Improve/add AI prompts

Edit files in `scanner/skills/`:
- `drift_detection.md` — main analysis prompt
- `batch_analysis.md` — batched analysis
- `judge.md` — false positive filter
- `config.yaml` — noise filters, grading thresholds

Guidelines:
- Be conservative (false positives erode trust)
- Keep prompts concise
- Always end with `Return JSON only. No markdown.`
- Test on a small repo before submitting

### Add integrations

**New issue trackers** (Asana, Monday, etc.):
1. Create `scanner/yourtracker.py` following `jira.py` or `linear.py`
2. Add OAuth config to `main.py` → `_OAUTH_CONFIGS`
3. Add enrichment logic to `run_analysis()` in `main.py`
4. Update `templates/index.html`

**New code hosts** (Bitbucket, etc.):
1. Create `scanner/yourhost.py` following `github.py`
2. Implement `fetch_repo_data()` with the same return schema
3. Add platform detection to `_detect_platform()` in `main.py`

### Fix bugs

- Include reproduction steps in your PR description
- Verify the fix doesn't break existing scans
- Test the HTML report renders correctly

## Issues

We use GitHub issues to track bugs and feature requests.

- Open a [GitHub Discussion](https://github.com/OneSpur/scanner/discussions) for general questions
- Open an [Issue](https://github.com/OneSpur/scanner/issues) for bugs or feature requests

## License

By contributing to 1Spur Scanner, you agree that your contributions will be licensed under the MIT License.
