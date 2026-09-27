# Agent harness operations

The user harness runs only on Studios. The ops harness is a separate image and is the only harness
allowed on core. Both reach models through the authenticated Coire `/v1` gateway; neither connects
to an engine port.

## Observe

Use the Agent Harness Grafana panels and query `coire_harness_runs_total`,
`coire_harness_failures_total`, retry histograms, and context truncations. Logs contain run,
variant, profile, and outcome identifiers only; prompt, reasoning, tool output, and credentials are
never logged.

## Evaluate

Create a JSON scorecard matching `HarnessEvaluationSubmission`, then run:

```bash
COIRE_API_URL=https://coire-core.example COIRE_API_TOKEN=... \
  uv run coire eval harness scorecard.json
```

A pass marks only that exact registry variant write-eligible. A deterministic failure clears its
verification; infrastructure errors append evidence without changing prior eligibility.

## Kill and rollback

Kill an active run through the run broker's kill switch (feature 011). Before that broker exists,
stop the ephemeral harness container through coire-node. Roll back the user and ops image digests
together with the API migration-compatible release. Do not delete scorecards or manually set
verification flags. Re-run the suite after rollback.
