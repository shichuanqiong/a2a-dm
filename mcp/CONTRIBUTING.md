# Contributing to agoradigest

Thanks for considering a contribution. Five rules keep the codebase
maintainable and reviews fast.

## 1. Open an issue first for anything non-trivial

Drive-by PRs that add features without prior discussion are usually
closed without merge. Trivial typo fixes / docstring edits are fine
without an issue.

For bug reports: include the failing call, the actual response, the
expected response, and the SDK version (`pip show agoradigest`).

## 2. Run tests locally before pushing

```bash
pip install -e ".[dev]"
pytest -q
```

The full suite runs in under 1 second. CI runs the same command on
every PR — pre-flighting saves us both a round-trip.

## 3. Follow existing patterns; don't reformat the world

If the surrounding code uses `from __future__ import annotations`,
your file uses it. If existing dataclasses use `field(default_factory=list)`,
yours does too. **Do not** run `black` or `isort` over files you
didn't touch — formatting-only diffs in unrelated files are
rejected on sight.

When in doubt, mirror the closest similar file.

## 4. Every public function gets a docstring

Public = exported via `__all__` or accessible without leading
underscore. Format:

```python
def my_function(arg: str, *, kwarg: int = 0) -> bool:
    """One-line summary in the imperative ("Return X" not "Returns X").

    Optional paragraph for context. Document failure modes — what
    raises, when, and why.

    Args:
      arg: what this is, semantically.
      kwarg: default + meaningful range.

    Returns:
      What you actually return, including edge cases (None when ...).

    Raises:
      ValueError: when arg is empty.
    """
```

Private helpers (`_underscore_prefix`) can be terser but still need
at least a one-line summary.

## 5. PR description tells the reviewer what / why / how tested

Use this template:

```markdown
## What
One-line change summary.

## Why
The problem this solves, ideally linked to an issue.

## How tested
- Added test_X in tests/test_Y.py covering the new path.
- Re-ran full suite: 239 passed.
- Manually exercised against staging with: <command>
```

A PR description that doesn't say what was tested gets a
review-stopping "how did you verify this?" comment.

## Versioning and release

Maintainer-only: tag pushes trigger PyPI publish via
`.github/workflows/release.yml`. Contributors don't touch version
strings — bump happens at release time.

We follow semver-ish:

- Patch (`0.8.0 → 0.8.1`) — bug fixes, doc updates, internal refactors
- Minor (`0.8.x → 0.9.0`) — additive features, new tools, new optional kwargs
- Major (`0.x.y → 1.0.0`) — breaking changes (rare; pre-1.0 we may
  break with notice in CHANGELOG)

## Security

If you've found a security issue, **do NOT open a public issue**.
See [SECURITY.md](SECURITY.md).

## License

Contributions are accepted under the project's Apache-2.0 license.
By submitting a PR you confirm you have the right to license your
contribution under those terms.
