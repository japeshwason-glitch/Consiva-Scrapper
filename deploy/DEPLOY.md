# Deploying on the server

Everything runs on one box: Postgres, the pipeline, the local model, the API.

## 1. Prerequisites

    sudo apt install -y python3-venv postgresql
    # llama-server for the local model, with parallel slots:
    #   llama-server -m <model.gguf> --port 8080 --parallel 8 --cont-batching

## 2. Database

    sudo -u postgres psql -c "CREATE ROLE consiva LOGIN PASSWORD '<password>';"
    sudo -u postgres psql -c "CREATE DATABASE consiva OWNER consiva;"

## 3. Code and environment

    cd /home/japesh/Dev/consiva/scrapper
    python3 -m venv venv
    ./venv/bin/pip install -r requirements.txt
    ./venv/bin/playwright install chromium      # pip alone does NOT fetch the browser
    cp .env.example .env                        # then fill in every value

`.env` needs: `DATABASE_URL`, `LLM_BASE_URL` (point at localhost once the model
is on this box), `APOLLO_API_KEY`, and the `CRM_DB_*` block.

## 4. Schema

    ./venv/bin/python migrate.py --status       # what is pending
    ./venv/bin/python migrate.py                # apply it

Safe to run on every deploy -- it is a no-op when nothing is outstanding, and it
refuses to run if a previously-applied migration file has been edited.

## 5. Load MCA data

    ./venv/bin/python merge_mca.py ~/mca/*.csv --out data/raw/all.csv
    ./venv/bin/python stages/01_ingest_mca.py data/raw/all.csv
    ./venv/bin/python build_generic_words.py    # rebuild against the full corpus

The last step matters: the generic-word table controls which junk domains stage
02 wastes time on, and it must be derived from the data actually loaded.

## 6. Services

    sudo cp deploy/consiva-*.service deploy/consiva-*.timer /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now consiva-api.service      # the API, always up
    sudo systemctl enable --now consiva-pipeline.timer   # the daily run

## Operating it

    systemctl list-timers consiva-pipeline       # when the next run fires
    journalctl -u consiva-pipeline -f            # live output of a run
    ./venv/bin/python run_daily.py --status      # where today stands
    curl localhost:8000/api/pipeline/status      # same, over HTTP

## After a power cut

Nothing to do.

* Postgres runs `fsync=on`, so every committed row is on disk.
* Each stage writes per company, so a crash costs only the few in flight.
* The pipeline's entire state is the `status` column -- there is no queue and no
  in-memory progress, so a restart resumes exactly where it stopped.
* `Persistent=true` on the timer means a run missed while the box was off fires
  on boot rather than being skipped for the day.
* The daily count is COUNTED from `delivered_at`, never stored, so a restart
  mid-run continues toward the target instead of restarting at zero.

The one thing to check after an unclean shutdown:

    curl localhost:8000/api/pipeline/runs | head

A row with `finished_at: null` is a stage that started and never finished.
Harmless -- the work is re-done on the next run -- but it tells you when the
machine went down.

## API surface

| Endpoint | For |
|---|---|
| `GET /api/leads` | CRM pulls undelivered leads |
| `POST /api/leads/delivered` | CRM marks a batch taken |
| `GET /api/pipeline/status` | today's progress and queue depth |
| `GET /api/pipeline/runs` | stage execution history |
| `GET /api/pipeline/throughput` | leads per day |
| `GET /api/health` | liveness |
| `GET /api/companies`, `/api/companies/{cin}` | dashboard |
| `GET /` | the dashboard UI |

Interactive docs at `/docs`.

## Moving from staging to a production CRM database

Everything that names the sales team's database is an environment variable, so
the switch is configuration, not code. But two things do not travel with a
config change and will break silently if they are missed.

### 1. Their SQL Server needs the scraper's columns

The pipeline writes three columns that are not part of their original schema.
Apply them before pointing at a new database, or every insert fails:

    sqlcmd -S <host>,<port> -d <database> -U <user> -P <password> \
           -i deploy/crm_schema.sql

Idempotent, so it is safe to run against a database that already has them.

### 2. GapScore: 0 is the worst, 100 is aligned

    a lead   is  GapScore < 100
    severe   is  GapScore <= 50

The score is ALIGNMENT with DPDP: 0 is the most severe gap, 100 is perfectly
aligned and not a lead. Filtering "> 0" returns the compliant companies and
excludes every real lead, with no error to notice.

A production database with no rows yet needs no data conversion. One holding
rows written on the old scale does: the statements at the bottom of
`deploy/crm_schema.sql` are commented out on purpose, because `100 - GapScore`
is correct exactly once.

### Variables to change

| variable | staging | production |
|---|---|---|
| `CRM_DB_HOST` | 45.114.245.10 | their production host |
| `CRM_DB_PORT` | 15333 | |
| `CRM_DB_NAME` | CookieConsentDB_Staging | their production database |
| `CRM_DB_USER` / `CRM_DB_PASSWORD` | | |
| `CRM_DB_TABLE` | dbo.CRM_Scrapper | only if renamed |
| `CRM_LEADS_TABLE` | crm.CrmLeads | only if renamed |

Nothing in our own Postgres changes. `delivered_at` still marks what has been
sent, so pointing at a fresh production database does NOT re-send the leads
already delivered to staging -- clear `delivered_at` deliberately if that is
what you want.

## Always build with the profile

    docker compose --profile pipeline build      # NOT `docker compose build`
    docker compose --profile pipeline up -d

`docker compose build` silently skips any service behind a `profiles:` key,
and the scheduler is behind `pipeline`. Without the flag it builds postgres,
migrate and api, prints no warning, and leaves the scheduler on whatever image
it had -- which on the first GB10 deploy meant it ran code 35 commits old and
died on a bug that had been fixed days earlier. `docker compose ps` showed it
"Up", because it was: just running the wrong thing.

To confirm the scheduler is actually current after a build:

    docker compose exec scheduler git -C /app rev-parse --short HEAD 2>/dev/null \
      || docker compose exec scheduler python -c "import config; print(config.CRM_LEADS_DB_NAME)"

An AttributeError there means the image predates that setting.
