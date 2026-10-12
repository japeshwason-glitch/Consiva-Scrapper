#!/usr/bin/env bash
# Run a command in a project container with the working tree mounted, from anywhere inside the repo.
#
#   scripts/run.sh [docker-compose-run flags] <service> <command...>
#
#   scripts/run.sh campaign python -m campaign.jobs prepare --dry-run
#   scripts/run.sh -e AE_NB_MODE=live campaign python -m campaign.jobs prepare --production --ids 3930,4092
#   scripts/run.sh campaign python -m unittest tests.test_campaign -v
#   scripts/run.sh api python migrate.py --status
#   scripts/run.sh api python - < some_script.py          # a script on stdin
#
# * The repo is mounted at /app, so code changes need no rebuild. It starts no service: --no-deps keeps Postgres
#   and everything else untouched (Postgres must already be up for anything that needs it).
# * `campaign` is the image built for this job (docker compose --profile campaign build campaign); it already has
#   Chromium, the report libraries and dnspython. Before it is built, use `api` and give it the extra libraries:
#       export RUN_PYLIBS="$HOME/.cache/consiva-pylibs"; mkdir -p "$RUN_PYLIBS"
#       scripts/run.sh api pip install -q --target /pylibs -r requirements-reports.txt -r requirements-campaign.txt
#   With RUN_PYLIBS set, that directory is mounted at /pylibs and put on PYTHONPATH for every run.
# * The container's exit status is this script's exit status. Environment comes from .env.docker (compose env_file);
#   add or override a value with -e NAME=value.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

extra=()
if [ -n "${RUN_PYLIBS:-}" ]; then
    mkdir -p "$RUN_PYLIBS"
    extra+=(-v "$RUN_PYLIBS:/pylibs" -e "PYTHONPATH=/pylibs")
fi

if [ "$#" -lt 2 ] && [ "${1:-}" != "--help" ]; then
    echo "usage: scripts/run.sh [docker compose run flags] <service> <command...>   (see the header of this file)" >&2
    exit 2
fi
[ "${1:-}" = "--help" ] && { sed -n '2,23p' "$0"; exit 0; }

# The campaign image must exist before it can be used: say so, with the command, rather than failing obscurely.
service=""
for arg in "$@"; do
    case "$arg" in
        campaign|api|reports|migrate) service="$arg"; break ;;
    esac
done
if [ "$service" = "campaign" ] && ! docker image inspect consiva-scrapper-campaign >/dev/null 2>&1; then
    echo "the campaign image is not built yet. Build it once with:" >&2
    echo "    docker compose --profile campaign build campaign" >&2
    exit 3
fi

# -T: no pseudo-terminal, so stdin scripts and pipes work. sed only drops compose's own "Container ... " chatter.
docker compose --profile campaign run --rm --no-deps -T -v "$ROOT:/app" "${extra[@]}" "$@" 2>&1 \
    | sed -u '/^ Container .*\(Creating\|Created\|Starting\|Started\|Running\) *$/d'
exit "${PIPESTATUS[0]}"
