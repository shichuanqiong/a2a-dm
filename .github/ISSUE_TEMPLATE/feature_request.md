---
name: Feature request
about: Suggest a new SDK capability
labels: enhancement
---

## What

<!-- One-line summary of the new capability -->

## Why

<!-- The problem you're trying to solve. Concrete use case > abstract wish. -->

## Sketch

<!-- What the API should look like, ideally as a code snippet -->

```python
# Today you have to do this:
client.friends.get("x")  # then check memory, then update, etc.

# Ideally:
client.friends.merge_memory("x", {"new_key": "value"})
```

## Alternatives considered

<!-- Have you tried solving this in user code? What didn't work? -->

## Out of scope?

<!-- Acknowledge if this might be better as a separate package /
your own helper / a PR to a downstream tool. -->
