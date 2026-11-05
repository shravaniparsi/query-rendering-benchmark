# Query and rendering benchmark execution package

This package contains synthetic inventory-reservation and event-time analytics applications, query/rendering benchmark collectors, independent business-state references, software tests, and the unchanged development-trained feasibility model with its fitting inputs.

The development fit retained the informed query-only action for all 864 decisions. Its joint and query-only development scores were both 579/864. Those measurements came from one UTC date and are not the reserved multi-day evaluation.

The reserved comparative-performance campaign is disabled. It requires an actual seven-artifact frozen release, successful public-runner validation, a dedicated repository-restricted ephemeral network identity, a fixed calendar, and one retained attempt per date. Main collection uses standard public Linux runners, with two explicitly selected CPUs per machine. No paid runners, Actions caches or Actions artifact uploads are configured here. Research output is retained as release assets.

The original private repository, manuscript, accepted document and private Git history are excluded. `public-package-manifest.json` inventories every copied file by byte length and SHA-256. The package is assembled from an explicit entry-point list and local import closure; it does not copy a working directory or Git history wholesale.

Local software checks:

```sh
python scripts/check_decision_contracts.py --output /tmp/decision-contracts.json
python scripts/check_main_contracts.py --output /tmp/main-contracts.json
python scripts/check_experimental_fit.py --data data/endtoend-distribution/v1-1 --fit data/decision-artifact/development-v1-1 --output /tmp/fit-replay.json
```

Exact experimental model replay uses the declared Linux/Python analysis platform. SQL and browser correctness validation uses pinned PostgreSQL 18.0 and Playwright dependencies. It exports semantic snapshots and content-matched visible paint markers without comparative timing estimates. Software-only test fixtures are synthetic and are not experimental measurements.
