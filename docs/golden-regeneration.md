# Golden-fixture regeneration

SPEC-12 Amendment v1.1 §G11. Golden files pin the exact output of
end-to-end runs so a regression that silently reshuffles the output
shows up as a diff on review, not as "the tests still pass."

Blind regeneration — `pytest --snapshot-update` or an equivalent
sweep — turns the suite into a snapshot of whatever the code currently
does, not a check of what it should do. This document is the process
rule for how to update them legitimately.

## Which files are "golden"

Any committed file whose content is treated as the expected output of
a test rather than as input to one. In this repo:

- `tests/golden/**` — hand-authored expected outputs for CLI runs
- Any `*.expected.*` or `*.golden.*` fixture file
- `tests/integration/full_pipeline/fixtures/**` — these are **inputs**
  (synthetic projects to analyze), not goldens. They can be edited
  freely; the goldens are the tests that *consume* them.

If you're unsure whether a file is golden, grep for it in tests: if a
test reads it and compares against actual output, it's golden.

## The rule

A golden file changes **only** in a commit that:

1. Lists each changed expectation in the commit message, with
2. The ticket (SPEC-16 R-number, G-number, or an explicit bug ID) that
   justifies the change, and
3. The reason the old expectation was wrong — not just "output changed."

No bulk `--update`. No "regenerated to match current behavior."

## Why

Golden tests catch two classes of bug the regular suite can't:

- **Silent reshuffles.** A refactor that reorders fields in JSON output
  breaks downstream consumers but passes every assert-level test.
- **Severity drift.** A finding that used to render as `error` and
  now renders as `warning` because an unrelated change touched the
  comparator — the summary count went down and no `assert` noticed.

If goldens are rewritten any time the code's output changes, both
classes slip through unobserved. The guard is social, not technical:
regeneration must be a reviewed, justified act.

## When regeneration is legitimate

- A SPEC ticket (any Rn, Gn, Scenario-E/F) explicitly changes the
  observable contract. Example: `R7` added the coverage line to text
  output, so every text-output golden that didn't have one needed a
  reviewed update.
- A bug fix changes the output to what it *should have been*. The
  commit message must say what the old output was wrong about, not
  just that it changed.
- A new field is added to a versioned output envelope and the schema
  version bumps alongside the goldens.

## When regeneration is **not** legitimate

- "Tests fail after my refactor" — the refactor broke a contract or
  the test caught a real regression; the fix isn't the golden, it's
  the refactor.
- "I ran the suite with `--update` and committed what came out" —
  this is the exact pattern the rule exists to prevent.
- "The output got longer / shorter / nicer" — if no ticket required
  that change, it's drift, and the test caught it correctly.

## Reviewer checklist

When a PR touches goldens, the reviewer looks for, in order:

1. **A ticket reference** in the commit message tying each golden
   change to a specific SPEC-16 ticket or amendment G-number.
2. **The diff tells a story.** Each changed expectation should make
   sense on its own — a new field added here, a reordering there — not
   a wholesale rewrite.
3. **Nothing unrelated snuck in.** A commit titled "R7: coverage line"
   that also rewrites a dozen unrelated goldens is suspicious; the
   extras need their own justification.

If any of those is missing, the PR goes back for the author to split
the commit, cite the ticket, or revert the goldens.

## Mechanical aid

For the common case — one ticket that genuinely changes expected
output across several goldens — regenerate in-place:

```bash
GRAVITAR_UPDATE_GOLDENS=1 pytest tests/<focused path>
git diff tests/golden
# inspect every hunk; make sure each matches the ticket
git add -p tests/golden/<the files you reviewed>
```

Then write a commit message that lists each change and the ticket.
`GRAVITAR_UPDATE_GOLDENS=1` is a convention — tests that honor it
rewrite their expected files instead of asserting; tests that don't
honor it continue to assert normally and the sweep leaves them alone.

## Amendment log

- 2026-10-12: created to close SPEC-12 Amendment v1.1 §G11.
