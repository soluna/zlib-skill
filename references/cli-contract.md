# CLI contract (schema 2)

Every command supports `--json` and returns a schema-2 envelope with `ok`,
`schema_version`, and `skill_version`. A controlled failure keeps the process
exit code non-zero and places a sanitized, stable `error` object in the same
envelope.

Remote commands also accept `--deadline-seconds` (0.001–3600 seconds). The
deadline is a total operation budget, not a per-request retry timeout. Batch
uses one budget for the whole file: entries that have not started when the
budget expires are reported as `OPERATION_CANCELLED`, while an entry that is
already running may report `OPERATION_TIMED_OUT`.

## Doctor semantics

`doctor --json` always reports a successful diagnostic invocation with
`overall_status` set to `healthy`, `degraded`, or `unavailable`. `usable` is
true when at least one source can be used, and `usable_source_count` gives the
corresponding count. A diagnostic invocation can therefore have `ok: true` and
`usable: false` when every source is unavailable.

Each source keeps its historical `status` value and adds an additive
`outcome`: `ok`, `unavailable`, `timed_out`, or `cancelled`. `next_actions` is
an array of structured `{code, source, message}` recovery suggestions.

