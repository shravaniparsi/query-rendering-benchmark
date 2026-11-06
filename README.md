# Query and rendering benchmark execution package

This package contains synthetic inventory-reservation and event-time analytics applications, query/rendering benchmark collectors, independent business-state references, software tests, and the unchanged development-trained feasibility model with its fitting inputs.

The development fit retained the informed query-only action for all 864 decisions. Its joint and query-only development scores were both 579/864. Those measurements came from one UTC date and are not the reserved multi-day evaluation.

The reserved comparative-performance campaign is disabled. It requires an actual seven-artifact frozen release, successful public-runner validation, a dedicated repository-restricted workload identity, verified free network capacity, a fixed calendar, and one retained attempt per date. Main collection uses standard public Linux runners, with two explicitly selected CPUs per machine. No paid runners, Actions caches or Actions artifact uploads are configured here. Research output is retained as release assets.

The manual `network-check` mode of Reserved Main Evaluation is an engineering check before that release. It uses four standard public Linux runners and ordinary tagged nodes, tests two private direct connections, and verifies removal of each job's own node. It requires an explicitly configured network approval and a separately recorded `QRL_NETWORK_EXECUTION_SHA`. It acquires no application or database performance data. The identity's Devices Core Write permission is tailnet-wide; the script's exact own-node checks are application safeguards. This tailnet must remain dedicated to the research check. Full run logs and completed or interrupted role bundles are retained in separate release assets. Qualification and main enable variables remain unset until their actual release is ready.

Ordinary registration is verified through the provider's explicit non-ephemeral, single-use auth-key capabilities, requested with a ten-minute expiry through OIDC. The secret is used from a private temporary file and revoked after registration; only the non-secret acknowledgement is retained. Cleanup checks the exact node identity even after a failed mode check. A recovery option is restricted to the recorded nodes in the retained first failed engineering attempt.

The original private repository, manuscript, accepted document and private Git history are excluded. `public-package-manifest.json` inventories every copied file by byte length and SHA-256. The package is assembled from an explicit entry-point list and local import closure; it does not copy a working directory or Git history wholesale.

Local software checks:

```sh
python scripts/check_decision_contracts.py --output /tmp/decision-contracts.json
python scripts/check_main_contracts.py --output /tmp/main-contracts.json
python scripts/check_experimental_fit.py --data data/endtoend-distribution/v1-1 --fit data/decision-artifact/development-v1-1 --output /tmp/fit-replay.json
```

Exact experimental model replay uses the declared Linux/Python analysis platform. SQL and browser correctness validation uses pinned PostgreSQL 18.0 and Playwright dependencies. It exports semantic snapshots and content-matched visible paint markers without comparative timing estimates. Software-only test fixtures are synthetic and are not experimental measurements.
