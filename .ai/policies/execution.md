# Execution Policy

Load for mutation tasks and worker recovery.

## Change budget

Unless the task explicitly grants a nonzero budget, assume zero new dependency, service, abstraction, schema change, and unrelated refactor. Prefer existing mechanisms and the smallest sufficient delta.

A budget exception must name the exact mechanism and acceptance reason. "Cleaner", "future-proof", "more robust", or "best practice" is not sufficient by itself.

## Worker preflight

Before material mutation:

1. Read `core.md`, current `task.yaml`, and only applicable policies.
2. Verify current task id, governance hash, `validated_generation`, `contract_rev`, `dispatch_id`, `objective_ref`, `objective_spec_rev`, acceptance refs, dependency refs, scope, recovery mode, and forbidden surfaces.
3. Verify the current dispatch has not become stale.
4. Preserve unrelated and Human work.

Workers own HOW only. Do not infer missing product scope or architecture. Never mutate canonical `.prime/` except the current task checkpoint/result, including indirectly through shell commands.

Prime owns Git orchestration. Workers must not reset, clean, rebase, reclone, force-push, destructively checkout, discard existing work, or use stash as hidden memory.

## Recovery modes

- `redo`: partial work is cheap or unsafe to resume. Reconstruct from contract plus Git.
- `resume`: reuse meaningful current-rev checkpoint/source state after inspecting Git reality.
- `inspect`: inspect authoritative side-effect reality before deciding whether an external action may be repeated.

Checkpoint only meaningful proven boundaries. `source_dispatch` records which worker incarnation proved it. A replacement dispatch may reuse a checkpoint when task id + contract rev remain current.

## Failure and retry

Classify failures compactly, for example `MECHANICAL`, `INTERRUPTED`, `DEPENDENCY`, `BASELINE`, `METHOD`, `SCOPE`, `AUTH`, `EXTERNAL`, `UNKNOWN`.

Retry only when information, method, state, or capability materially changes. Quota/session/model replacement is interruption, not method failure. One materially different method may follow a method failure. Same causal boundary failing twice requires split/recontract/block.

No recursive delegation, duplicate writers, unbounded polling, or model-roulette retries.

## Result discipline

Before success:

1. Re-read the current task.
2. Confirm exact task/rev/dispatch identity.
3. Inspect actual source delta and scope.
4. Run only causal verification required by acceptance.
5. Record all proved acceptance refs and concise authoritative evidence.
6. Record every task-owned changed path, no unrelated paths.

If task/rev/dispatch is stale, do not overwrite the current result.

`result.yaml` is a worker claim. Prime and `ctl.py accept` determine project acceptance.
