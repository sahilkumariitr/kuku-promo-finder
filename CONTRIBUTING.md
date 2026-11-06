# Contributing

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[app,dev]"
pytest -q
```

## Guidelines

- The core package (`promo_finder/`) stays standard-library only; UI and test dependencies go in
  `pyproject.toml` extras.
- Every change to scoring or prompts should be checked with `promo-finder-eval` against labelled picks,
  and the before/after numbers noted in the pull request.
- Prompts live in `promo_finder/prompts.py`; tunables live in `promo_finder/config.py`.
- **Never commit show content.** Screenplays and outputs that quote them are confidential; `.gitignore`
  excludes `data/` and `outputs/`. Test fixtures must be original, synthetic content.
