# Contributing to Harborline Lab

Thanks for helping. The most useful thing you can send is a setup report from a machine we haven't seen yet.

## Good ways to help

- **Setup reports.** Did it work on your machine, or where did it stop? Use the [setup issue form](../../issues/new?template=setup-failed.yml).
- **New scenarios** for the Control Center, such as a carrier outage or a bad release.
- **New tickets or dashboard ideas** for the 8 projects.
- **Fixes to the handbook**, anywhere it confused you.

Please don't post answers to the hidden business problems in public issues. Say that something exists without saying where, so the lab stays fun for others.

## Working on the code

The simulator and API are Python in `simulator/`, and the web app is React and TypeScript in `web/`.

Run the tests against a throwaway database:

```bash
export HL_TEST_PG_PASSWORD="$(openssl rand -hex 16)"
docker run -d --name hl-test-pg -e POSTGRES_PASSWORD="$HL_TEST_PG_PASSWORD" -p 55434:5432 postgres:17 -c wal_level=logical -c max_replication_slots=10 -c max_wal_senders=10
```

```bash
cd simulator && python -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

```bash
HL_TEST_PG_PORT=55434 .venv/bin/python -m pytest -q
```

Work on the web app without a backend, using sample data:

```bash
cd web && npm install && npm run dev:mock
```

And check the types:

```bash
cd web && npx tsc -b
```

## Rules that keep it working

- `config/business_rules.yaml` is the single source of truth for the company. Colleagues, tickets and data must agree with it.
- Some files are generated, so change their source and regenerate. These are `handbook/team.html`, `handbook/known-issues.html`, `team/prompts/`, `setup/harborline-chat.zip` and `web/src/mock/fixtures/`. The generators are in `simulator/harborline/tools/`.
- `SETUP-WINDOWS.cmd` must stay ASCII with CRLF line endings, and run on PowerShell 5.1. `SETUP-MAC.command` must run on bash 3.2.
- Setup must never wipe finished data. See `test_setup_can_never_wipe_finished_data`.
- Don't add real company names to anything that behaves badly in the simulation. Real carriers appear only as neutral shipping options.
- Bumping `setup/VERSION` on `main` is what releases a new version to everyone: the next time anyone runs setup, it sees the higher number and offers the update. So bump it when you change anything setup copies, and only when `main` is ready to ship. Numbers compare part by part, so `2026.11.10` is newer than `2026.11.9`.
- `setup/UPDATE_REPO` names the repository updates come from. On a fork, change it to yours, or leave it blank to turn update checks off. `HL_UPDATE_REPO` in the environment overrides it for testing (on Windows, a single space counts as blank).
- Never commit a password or key, not even a sample. Setup generates them into `.env`, different on every install, and only fills in what is blank, so an update can't change them. CI runs gitleaks, and `test_no_key_is_in_the_package` fails on any of the old sample values.

## Pull requests

Keep them small and say how you tested. If you changed setup, say which operating system you ran it on.
