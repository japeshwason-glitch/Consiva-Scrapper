#!/bin/bash
# Waits for the baseline run to finish, then replays the SAME companies through
# the filtered resolver so the before/after is on identical input.
cd /home/japesh/Dev/consiva/scrapper
until grep -q "^resolved .*duplicate-domain" logs/stage02_newflow.log 2>/dev/null; do sleep 15; done
echo "=== BASELINE COMPLETE ==="
grep -E "^resolved" logs/stage02_newflow.log
echo "  walls hit: $(grep -c '^  wall' logs/stage02_newflow.log)"
grep -E "^real" logs/stage02_newflow.log

venv/bin/python - <<'PY'
import config, psycopg2, json, collections
c=psycopg2.connect(config.DATABASE_URL); cur=c.cursor()
cur.execute("""SELECT cin,status FROM companies
               WHERE updated_at > NOW() - INTERVAL '3 hours'
                 AND (status <> 'discovered' OR retry_count > 0)""")
rows=cur.fetchall(); cins=[r[0] for r in rows]
json.dump(cins, open("data/exports/.newflow_cohort.json","w"))
print(f"\ncohort: {len(cins)}", dict(collections.Counter(r[1] for r in rows)))
# reset them, and backdate updated_at so get_pending picks exactly these first
cur.execute("""UPDATE companies SET status='discovered', retry_count=0, last_error=NULL,
                      resolved_domain=NULL, tld_used=NULL, resolve_confidence=NULL,
                      resolve_match_score=NULL, resolve_evidence=NULL,
                      resolve_verified_by=NULL,
                      updated_at = NOW() - INTERVAL '10 years'
               WHERE cin = ANY(%s)""",(cins,))
c.commit(); print(f"reset {cur.rowcount} rows to 'discovered'")
PY

N=$(venv/bin/python -c "import json;print(len(json.load(open('data/exports/.newflow_cohort.json'))))")
echo "=== RERUN WITH SLUG FILTER (n=$N) ==="
{ time venv/bin/python -u stages/02_resolve_domain.py --limit "$N"; } > logs/stage02_filtered.log 2>&1
grep -E "^resolved|^real" logs/stage02_filtered.log
echo "  walls hit: $(grep -c '^  wall' logs/stage02_filtered.log)"
