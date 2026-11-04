# Focused mutation checks

For Plainfin, apply [the shared project policy](../../qa-project-context/references/plainfin-policy.md) first.
Commands, numeric targets and CI templates below are examples; they do not
override repository tooling, coverage gates or execution permissions.


Use the repository's vendored `code-review-and-quality` skill and Plainfin policy. Mutation
testing is optional evidence for an unresolved behavioral question, not a required
review step, universal score threshold or proxy for independent test authorship.

## Procedure

1. Identify the required behavior, code path and assertion whose strength is uncertain.
2. Confirm that a bounded experiment is useful, feasible and within current task
   authorization. An unavailable tool or unapproved test type is a reason to use
   source/CI evidence and state the limit, not to expand permissions.
3. Use an isolated temporary copy/worktree, not the reviewed tree. Keep the same
   selected tests for the baseline and behavior-changing mutation, with a deadline
   that terminates the process. Use existing stack tooling; do not add a framework.
4. Record baseline, mutation, executed cases, command, revision and outcomes. A
   baseline failure, invalid build, skipped selection or infra failure is inconclusive.
5. Investigate a survivor only after proving that the mutation changes required,
   reachable behavior. Equivalent/unreachable changes are not coverage findings.

## Tool selection

Use Stryker/PIT/cargo-mutants/mutmut only for their applicable stack when available
and appropriate. For Go use the repository's native harness and a narrowly scoped
manual mutation if warranted; do not import a JS/Python runner. A mutation runner's
kill-set is sampled evidence, not proof of complete behavioral equivalence.

There is no Plainfin score floor of 60/70/80 percent. Adding dependencies, scheduled
mutation runs or a CI gate is a separate implementation decision within task scope.
No mutation command authorizes UI, simulator, device or instrumented test execution.
