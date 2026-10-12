# Lead gen pipeline

Run order: stages/01 through stages/07, each one a cron job reading rows at
one status and advancing them to the next. See db.py for the shared
read/advance/fail helpers every stage script uses.

## Running things in a container: `scripts/run.sh`

`scripts/run.sh` runs one command in a project container with the working tree mounted at `/app` (so code changes need
no rebuild) and without starting any other service. Run it from anywhere inside the repo.

```bash
scripts/run.sh [docker compose run flags] <service> <command...>

scripts/run.sh campaign python -m campaign.jobs prepare --dry-run
scripts/run.sh -e AE_NB_MODE=live campaign python -m campaign.jobs prepare --production --ids 3930,4092
scripts/run.sh campaign python -m unittest tests.test_campaign tests.test_campaign_scan -v
scripts/run.sh api python migrate.py --status
scripts/run.sh api python - < my_readonly_check.py        # a script on stdin
```

- **Service:** `campaign` is the image built for the email campaign (`docker compose --profile campaign build campaign`);
  it has Chromium, the report libraries and `dnspython`. Postgres must already be up (`docker compose up -d postgres`).
- **Before the campaign image is built:** use the `api` service and give it the extra libraries once:
  ```bash
  export RUN_PYLIBS="$HOME/.cache/consiva-pylibs"
  scripts/run.sh api pip install -q --target /pylibs -r requirements-reports.txt -r requirements-campaign.txt
  ```
  While `RUN_PYLIBS` is set, that folder is mounted at `/pylibs` and put on `PYTHONPATH` for every run.
- **Environment:** values come from `.env.docker`; add or override one with `-e NAME=value`.
- **Exit status:** the container's exit status is the script's exit status.

The campaign pipeline itself is described in `CLAUDE.md` (rules) and `docs/report_pipeline.md`, `docs/crm_schema.md`.
