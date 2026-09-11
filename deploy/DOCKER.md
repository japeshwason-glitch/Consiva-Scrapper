# GB10 deployment (Docker)

Postgres, the API and the scheduler run in containers. **The model does not** —
`llama-server` stays on the host so it keeps direct GPU access.

## 1. Start the model on the host

    llama-server -m /path/to/qwen3.6-35b-a3b.gguf \
                 --port 8080 --parallel 8 --cont-batching

`--parallel 8` matters. The pipeline runs six workers; with the default single
slot they queue behind each other and the concurrency is wasted.

## 2. Configure

    cp .env.docker.example .env.docker
    # fill in APOLLO_API_KEY and CRM_DB_PASSWORD

Hostnames differ from a bare-metal `.env` and that is deliberate:

| Setting | Value | Why |
|---|---|---|
| `DATABASE_URL` | `...@postgres:5432/consiva` | the compose service name, not localhost |
| `LLM_BASE_URL` | `http://host.docker.internal:8080` | inside a container, `localhost` is the container |

## 3. Bring it up

    docker compose up -d --build

That starts Postgres, waits for it to be healthy, runs migrations once, then
starts the API on **:8012** and the scheduler.

    curl localhost:8012/api/health
    curl localhost:8012/api/pipeline/status

## 4. Load MCA data (first time only)

    docker compose run --rm scheduler python stages/01_ingest_mca.py data/raw/<file>.csv
    docker compose run --rm scheduler python build_generic_words.py

`./data` is bind-mounted, so drop CSVs in `data/raw/` on the host and the
container sees them.

## Guardrails

Two independent stops, both enforced every cycle:

| Guardrail | Default | Set via |
|---|---|---|
| Leads delivered to the CRM | 500/day | `DAILY_LEAD_TARGET` |
| Websites scraped | 2000/day | `DAILY_MAX_SCRAPES` |

Whichever is reached first ends the day's run. The lead target is the goal; the
scrape ceiling is the safety net that stops a low-yield batch from crawling all
night chasing a target it cannot reach.

**Only real leads reach the CRM.** `crm_push` filters on
`has_cmp = false AND consent_gap_score > 0` in the query itself, so a company
with no consent gap cannot occupy a row in their table regardless of its
pipeline status.

## Operating

    docker compose logs -f scheduler                        # watch a run
    docker compose run --rm scheduler python run_daily.py --status
    docker compose exec postgres psql -U postgres consiva   # database shell
    docker compose restart api

## After a power cut

Nothing to do. `restart: unless-stopped` brings the containers back with the
machine, and the scheduler checks on boot whether today's window passed with the
target unmet — if so it runs immediately instead of skipping the day.

Underneath that, the pipeline's whole state is the `status` column: Postgres
runs `fsync=on`, every stage writes per company, and the daily count is counted
from `delivered_at` rather than stored. A restart resumes; it never repeats or
double-counts.

To confirm what happened:

    curl localhost:8012/api/pipeline/runs | head

A row with `finished_at: null` is a stage that started and never finished —
harmless, redone next run, but it tells you when the box went down.

## Ports

| Port | Service |
|---|---|
| 8012 | API + dashboard (8000 is taken on this machine) |
| 5433 | Postgres, bound to 127.0.0.1 only |
| 8080 | llama-server, on the host |
