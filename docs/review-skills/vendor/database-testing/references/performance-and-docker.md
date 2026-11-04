# Query performance and test databases

For Plainfin, apply [the shared project policy](../../qa-project-context/references/plainfin-policy.md).
Use existing workload targets and PostgreSQL 18 pins, test provisioning and Go
harness. This reference is guidance, not permission for production SQL or new CI.

## Select a representative case

Identify the real query and caller, meaningful filters and owner scope, expected
volume/distribution, selectivity and parameter extremes. Include a small dataset
for semantics and larger representative data only where performance risk warrants
it. Keep fixtures synthetic; refresh relevant statistics in the disposable test
environment as needed. Record database configuration, statistics/cache conditions
and parameter values so comparisons are interpretable.

Check exact results before performance: duplicates, NULLs, sorting/ties, pagination,
user isolation and integer minor-unit values. A faster wrong query is a regression.
Also inspect caller request counts and pool/lock waiting: faster individual SQL does
not resolve N+1 or an exhausted connection budget.

## EXPLAIN safely

Begin with plain EXPLAIN for planning evidence. A structured output can be useful:

```sql
EXPLAIN (FORMAT JSON)
SELECT id, amount_minor
FROM operations
WHERE user_id = $1
ORDER BY occurred_at DESC, id DESC
LIMIT $2;
```

This is a query-shape illustration, not a project schema or executable psql script.
Bind its parameters through the actual driver and use the real repository query.
Do not change SQL-construction rules or invent schema/index changes from the example.

EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) executes the statement, including its side
effects. Run it only in an owned disposable or explicitly authorized environment,
with a context/statement timeout and bounded result artifacts. A rollback can undo
transactional writes but not every external effect or sequence advance. An ordinary
review does not require executing SQL; missing runtime evidence remains explicit.

Read the entire `Plan` tree recursively through child `Plans`; root nodes such as
Limit, Aggregate, Sort and joins do not identify the table access path. Compare
estimated and actual rows/loops where available, pages read/hit, rows removed,
sort work/spills and partition pruning. Do not sum per-node totals blindly because
nested values can include work already represented by children.

Seq Scan can be optimal for small tables or broad selections. Index Scan can be
expensive with poor selectivity, repeated loops or many heap visits. Neither a root
`/Index/` assertion nor a universal ban on Seq Scan is a meaningful performance
oracle. Do not disable sequential scans or pin one plan shape to make a test green.

## Assertions and index evidence

Choose only budgets the repository/workload actually requires. Record a comparable
baseline and repeat measurements when evaluating timing; allow for normal cache,
statistics and runner variance. Distinguish planned cost from measured runtime.
There is no generic 10 ms, 100 ms or 10K-row gate for all queries.

A catalog assertion is appropriate when a particular index is an intended schema
contract. Check access method, column/expression order, predicate, uniqueness and
validity as applicable, not merely whether an index definition contains `email` or
whether every queried column has some index. Consider write/storage cost.

Optional focused experiments can establish whether a proposed regression check is
sensitive to a real problem. If an index is removed in a disposable copy, compare
semantics and relevant workload measurements; a valid alternate plan is possible.
Do not require the plan to become Seq Scan, require every test to fail on DROP INDEX,
or drop/restore indexes in a shared suite. Retain failed/inconclusive evidence.

## Provisioning

Prefer the repository's existing real PostgreSQL test harness. Go code uses Go
`testing`, existing test helpers and `testcontainers-go` only where already declared.
Use the existing image/digest/default-image pin matching the intended PostgreSQL 18
setup, not a moving tag or a patch number claimed current by this reference.

If the harness supports `TEST_DATABASE_URL`, use an explicitly selected permitted
test server, with harness-owned isolated databases and cleanup. It is not authority
to reset a database found at a local default address. Redact credentials from output.

Container-managed random ports must come from the container runtime; a pre-existing
Compose server follows its declared port model. Do not mix both in one suite or
introduce new orchestration to replace a working harness. Ensure owned resources
stop on setup failure and normal completion. Database absence means a skipped or
blocked test as defined by the repository, never a passing integration suite.

## PostgreSQL references

- [Using EXPLAIN](https://www.postgresql.org/docs/18/using-explain.html): plan trees,
  scan choices, estimates and observed work.
- [EXPLAIN command](https://www.postgresql.org/docs/18/sql-explain.html): ANALYZE
  executes the statement and has side effects.
- [SQLSTATE codes](https://www.postgresql.org/docs/18/errcodes-appendix.html): structured
  errors rather than localized message matching.
