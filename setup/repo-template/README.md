# Harborline Analytics

Data platform and department dashboards for Harborline Logistics, a fictional third-party logistics company. Practice
program; all data is simulated.

The simulator that produces the source data lives in its own folder (`harborline-lab`) and is not part of this repository.

## What's here

| Folder | What goes in it |
|---|---|
| `docs/adr/` | Architecture decision records, starting with `0001-platform.md` |
| `docs/metrics/` | Every metric definition agreed, with its owner |

Add `ingestion/`, `transform/`, `orchestration/`, `infra/`, `dashboards/` and `tests/` as the work needs them.

## How work happens

One branch per ticket (`feat/DATA-<n>-short-slug`), Conventional Commits carrying the ticket key, a pull request with
self-review, and `main` protected. Secrets never go in this repository: pre-commit scans every commit.
