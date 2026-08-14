# RateReplay operations

These assets define the observable surface for the `LOCAL_REPRODUCIBLE` deployment class.
They are not evidence of a hosted deployment, and all hosted-operation claims remain withheld until a separate hosted qualification passes.

Run `make operations-config-check` after changing metrics, alerts, dashboards, or this runbook.

The API exposes its process metrics at `/metrics` and health at `/healthz` and `/readyz`.
The unified worker runtime exposes its own process metrics on the configured loopback address and port.
Metrics, logs, and traces use fixed labels and random internal correlation identifiers only.
They must never contain interval values, filenames, account facts, source identifiers, or report contents.

## API server errors

Confirm `/readyz` first, then group `ratereplay_http_requests_total` by normalized route and status.
Use the request ID to correlate the redacted structured event and trace.
Do not copy exception text or request bodies into incident notes.

## Durable job backlog

Compare queue depth with worker polling outcomes for the same fixed job kind.
Check dependency readiness before changing concurrency.
Never modify a durable job row manually because lease and lifecycle-generation fences are part of correctness.

## Stalled lease

Inspect the fixed job kind and job ID from the structured worker event.
Allow the durable rescue deadline to expire or use the tested recovery workflow.
Do not force-publish staged output from a stale attempt.

## Repeated worker failures

Use the stable failure code stored on the durable job and confirm whether the failure is retryable.
Preserve staged artifacts for the orphan sweeper and keep the original operation identity.

## Deletion failure

Keep the account fenced and verify the separately protected ledger before retrying the same deletion identity.
Do not remove deletion control rows, rotate away historical restore keys, expose a restored service, or bypass a quarantine hold.

## Alert thresholds

The committed thresholds are conservative development defaults for the reproducible local topology.
They are operational checks, not measured hosted service objectives.
Hosted thresholds require a workload study and hosted validation before they may replace these values.
