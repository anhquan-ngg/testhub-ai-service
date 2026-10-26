# Google SDK local simulation

2026-10-25: Postman CLI 1.74.0, synthetic provider content only; no Google key or
paid request. Run from the AI repository:

`postman simulation run postman/simulations/google-embedding.sim.yaml --port 4962 --no-history --output ndjson`

Set RUN_EMBEDDING_SIMULATION=1 in the test process and run `.venv/Scripts/python.exe -m pytest -q`.
SDK tests override only the base URL to http://127.0.0.1:4962; the SDK adds /v1beta.
The simulation fixture models Google embedding/generation responses, not semantic
answer quality. Planned prompts and legacy grounded prompts are both supported.

## Observations

<!-- begin:observations -->
2026-10-25 citation run: 165 passed / 1 real-pgvector opt-in skipped. The sole
member served all nine events; none were silent. Five embedding calls included
200/429/403; four generateContent 200 calls covered valid mixed slots, repeated
citations consolidated into one verified source, invalid answer flags rejected
without another call, and legacy grounded success. No live Google/browser/DB E2E.

2026-10-25 answer run: 156 passed / 1 skipped; eight events, no silent members.
SDK responseSchema included four-option bounds and boolean descriptions.
<!-- end:observations -->

Simulation was stopped after verification; synthetic HTTP logs are stored outside
the repo at workspace executions/review-20261025/google-answer-simulation.ndjson
and google-citation-simulation.ndjson. Test-only objectives
fixture-duplicate-citations and fixture-invalid-answers select synthetic faults.
