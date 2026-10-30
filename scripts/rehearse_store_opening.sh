#!/usr/bin/env bash
# Rehearse The Wardrobe's opening on a local copy of the server (deploy/compose.rehearsal.yaml):
# a fresh database, the tenant and the store as the runbook builds them, the PT opening as the
# goods worker's durable job (with the API restarted mid-load), then the legacy SOH sync, then
# scripts/rehearsal_assert.py. Never touches the server.
#
#   scripts/rehearse_store_opening.sh            # one run
#   scripts/rehearse_store_opening.sh --twice    # then restore the baseline and run again
#
# Needs the git-ignored real files (or REHEARSAL_PT / REHEARSAL_SOH pointing at them):
#   docs/wardrobe-ratu-road/normalized/WARDROBE_PT_combined.xlsx
#   docs/wardrobe-ratu-road/SOH/SOH_WARDROBE_28-10-2026.xlsx
# Writes deploy/.env.rehearsal (random secrets, git-ignored) the first time, and its dumps,
# logs and the synthetic logins file under .local/rehearsal/ (git-ignored). Prints no password.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TWICE=0
for arg in "$@"; do
  case "$arg" in
    --twice) TWICE=1 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

STORE=WRR
UPLOADER=debanjan@wardrobe.test
PT_IN=/rehearsal/data/WARDROBE_PT_combined.xlsx
SOH_IN=/rehearsal/data/SOH_WARDROBE.xlsx
OUT="${REHEARSAL_OUT:-$ROOT/.local/rehearsal}"
export REHEARSAL_OUT="$OUT"
ENV_FILE=deploy/.env.rehearsal
RESULTS="$OUT/results.txt"

for f in "${REHEARSAL_PT:-docs/wardrobe-ratu-road/normalized/WARDROBE_PT_combined.xlsx}" \
         "${REHEARSAL_SOH:-docs/wardrobe-ratu-road/SOH/SOH_WARDROBE_28-10-2026.xlsx}"; do
  [[ -f "$f" ]] || { echo "missing data file: $f (git-ignored; copy it in or set REHEARSAL_PT/REHEARSAL_SOH)" >&2; exit 2; }
done

mkdir -p "$OUT/secrets"
chmod 700 "$OUT/secrets"
: > "$RESULTS"
rm -f "$OUT"/assert-*.txt "$OUT"/*.log

if [[ ! -f "$ENV_FILE" ]]; then
  # Fresh local secrets every new checkout; never the server's.
  sed -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(openssl rand -hex 24)|" \
      -e "s|^DJANGO_SECRET_KEY=.*|DJANGO_SECRET_KEY=$(openssl rand -hex 32)|" \
      -e "s|^KDPS_DEPLOYMENT_KEY=.*|KDPS_DEPLOYMENT_KEY=$(python3 -c 'import uuid; print(uuid.uuid4())')|" \
      -e "s|^KDPS_BOOTSTRAP_ADMIN_PASSWORD=.*|KDPS_BOOTSTRAP_ADMIN_PASSWORD=$(openssl rand -hex 16)|" \
      deploy/rehearsal.env.example > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "wrote $ENV_FILE with fresh random secrets"
fi

DC=(docker compose -p kdps-rehearsal --env-file "$ENV_FILE" -f deploy/compose.yaml -f deploy/compose.rehearsal.yaml)

log() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
record() {  # record NAME PASS|FAIL DETAIL
  printf '%s\t%s\t%s\n' "$1" "$2" "$3" >> "$RESULTS"
  printf '  %-4s %s  %s\n' "$2" "$1" "$3"
}
expect() {  # expect NAME DETAIL -- command...   (PASS when the command succeeds)
  local name="$1" detail="$2"; shift 3
  if "$@"; then record "$name" PASS "$detail"; else record "$name" FAIL "$detail"; fi
}
psql_db() { "${DC[@]}" exec -T db psql -U kdps -d kdps -Atc "$1"; }
manage() { "${DC[@]}" exec -T api python manage.py "$@"; }

wait_healthy() {
  local deadline=$((SECONDS + ${REHEARSAL_HEALTH_TIMEOUT:-900})) cid status
  while :; do
    cid="$("${DC[@]}" ps -q api || true)"
    status="$( [[ -n "$cid" ]] && docker inspect -f '{{.State.Health.Status}}' "$cid" 2>/dev/null || echo starting)"
    [[ "$status" == healthy ]] && return 0
    (( SECONDS > deadline )) && { echo "api not healthy in time (migrating an empty database takes ~6 min)" >&2; "${DC[@]}" logs --tail 80 api >&2; return 1; }
    sleep 3
  done
}

ql() {  # the PT source's quick_load field
  psql_db "SELECT coalesce(reconciliation->'quick_load'->>'$1', '') FROM ptmapper_sohimport
           WHERE source_metadata ? 'pt_opening' ORDER BY created_at DESC LIMIT 1" 2>/dev/null || true
}

db_now() { psql_db "SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"+00:00\"')"; }

# -- 1-4: a fresh server, the tenant, the store, the baseline -----------------------

setup_fresh() {
  log "1. Fresh stack (down -v, up --build)"
  touch scripts/rehearsal_assert.py
  "${DC[@]}" down -v --remove-orphans
  "${DC[@]}" up -d --build
  wait_healthy

  log "2. Tenant and owner (bootstrap_deployment --synthetic)"
  manage bootstrap_deployment --synthetic --code KDPS-TEST --name "The Wardrobe (test)" \
    --admin-email kdsingh@wardrobe.test --admin-name "K D Singh" --admin-staff-code OWN-01

  log "3. Store, warehouse and people (load_test_store --setup), API stopped as in the runbook"
  rm -f "$OUT/secrets/test-logins.txt"
  "${DC[@]}" stop api
  "${DC[@]}" run --rm --no-deps -T api python manage.py load_test_store \
    --setup /rehearsal/setup.json --password-file /rehearsal/out/test-logins.txt
  "${DC[@]}" start api
  wait_healthy

  log "4. Baseline (pg_dump -Fc and the /data volume)"
  "${DC[@]}" exec -T db pg_dump -U kdps -Fc kdps > "$OUT/baseline.dump"
  "${DC[@]}" exec -T api tar -C /data -cf - . > "$OUT/baseline-data.tar"
  ls -l "$OUT/baseline.dump" "$OUT/baseline-data.tar"
}

restore_baseline() {
  log "Restore the baseline (database and /data)"
  "${DC[@]}" stop api
  "${DC[@]}" exec -T db dropdb -U kdps --force kdps
  "${DC[@]}" exec -T db createdb -U kdps kdps
  "${DC[@]}" exec -T db pg_restore -U kdps -d kdps --no-owner < "$OUT/baseline.dump"
  "${DC[@]}" run --rm --no-deps -T api sh -c 'rm -rf /data/* /data/.[!.]* 2>/dev/null; tar -C /data -xf -' \
    < "$OUT/baseline-data.tar"
  "${DC[@]}" start api
  wait_healthy
}

# -- 5-10: open, restart mid-load, repeat, sync, repeat, stale, assert -------------

run_cycle() {
  local n="$1" t0 restart_at resumed_at restart_step open_rc open_s sync_s source_id intents_before
  echo "== cycle $n ==" >> "$RESULTS"

  log "5. [$n] open_test_store --wait (followed from a separate container, so restarting the API leaves it running)"
  t0=$SECONDS
  set +e
  "${DC[@]}" run --rm --no-deps -T api python manage.py open_test_store --store "$STORE" --pt "$PT_IN" \
    --uploader "$UPLOADER" --wait --timeout 2400 > "$OUT/open-$n.log" 2>&1 &
  local follower=$!
  set -e

  # Restart the API (and so its goods worker) once the first batch has begun.
  restart_step=""
  while kill -0 "$follower" 2>/dev/null; do
    local step; step="$(ql step)"
    if [[ "$step" == "Batch 1 of"* ]]; then restart_step="$step"; break; fi
    sleep 2
  done
  if [[ -n "$restart_step" ]]; then
    log "   restarting api mid-load at: $restart_step"
    restart_at=$SECONDS
    "${DC[@]}" restart api
    wait_healthy
    # The old worker may finish one more step during the container's stop grace, so wait
    # for progress after the new container is up. A killed job resumes once its lease
    # (120 s, core/outbox.py) runs out.
    local healthy_at=$SECONDS healthy_step; healthy_step="$(ql step)"
    log "   api healthy again after $((healthy_at - restart_at)) s at '$healthy_step'; waiting for the job to resume"
    resumed_at=""
    while kill -0 "$follower" 2>/dev/null; do
      if [[ "$(ql step)" != "$healthy_step" || "$(ql state)" == done ]]; then resumed_at=$SECONDS; break; fi
      sleep 2
    done
    if [[ -n "$resumed_at" ]]; then
      record "load resumed after an API restart" PASS \
        "restart at '$restart_step' +$((restart_at - t0)) s; next step $((resumed_at - healthy_at)) s after healthy"
    else
      record "load resumed after an API restart" FAIL "no progress after the restart before the follower ended"
    fi
    echo "TIMING cycle=$n restart_to_progress_s=$((resumed_at - restart_at))" >> "$RESULTS"
  else
    record "load resumed after an API restart" FAIL "the load ended before batch 1 began; no restart done"
  fi

  set +e; wait "$follower"; open_rc=$?; set -e
  open_s=$((SECONDS - t0))
  tail -n 25 "$OUT/open-$n.log"
  if [[ $open_rc -eq 0 ]] && grep -q "^Done:" "$OUT/open-$n.log"; then
    record "opening done (open_test_store --wait)" PASS "wall ${open_s} s"
  else
    record "opening done (open_test_store --wait)" FAIL "exit $open_rc after ${open_s} s; see $OUT/open-$n.log"
  fi
  echo "TIMING cycle=$n opening_wall_s=$open_s" >> "$RESULTS"

  log "   opening assertions"
  set +e
  "${DC[@]}" exec -T api python /rehearsal/rehearsal_assert.py --phase opening --store "$STORE" --pt "$PT_IN" \
    | tee "$OUT/assert-opening-$n.txt"
  local arc=${PIPESTATUS[0]}; set -e
  record "rehearsal_assert --phase opening" "$([[ $arc -eq 0 ]] && echo PASS || echo FAIL)" "exit $arc"

  log "6. [$n] The same PT again: the same source, no new work"
  source_id="$(sed -n 's/^Source \([0-9a-f-]*\) .*/\1/p' "$OUT/open-$n.log" | head -1)"
  intents_before="$(psql_db "SELECT count(*) FROM core_outboxintent WHERE kind = 'opening_load'")"
  local sources_before; sources_before="$(psql_db "SELECT count(*) FROM ptmapper_sohimport")"
  manage open_test_store --store "$STORE" --pt "$PT_IN" --uploader "$UPLOADER" > "$OUT/open-again-$n.log" 2>&1 || true
  cat "$OUT/open-again-$n.log"
  local again_id; again_id="$(sed -n 's/^Source \([0-9a-f-]*\) .*/\1/p' "$OUT/open-again-$n.log" | head -1)"
  local intents_after sources_after
  intents_after="$(psql_db "SELECT count(*) FROM core_outboxintent WHERE kind = 'opening_load'")"
  sources_after="$(psql_db "SELECT count(*) FROM ptmapper_sohimport")"
  expect "same PT gives the same source" "first=$source_id again=$again_id" -- \
    test -n "$source_id" -a "$source_id" = "$again_id"
  expect "same PT queues no new work" "load jobs $intents_before->$intents_after, sources $sources_before->$sources_after" -- \
    test "$intents_before" = "$intents_after" -a "$sources_before" = "$sources_after"

  log "7. [$n] sync_soh --apply (exported now)"
  local now; now="$(db_now)"
  local t1=$SECONDS
  set +e
  manage sync_soh --store "$STORE" --soh "$SOH_IN" --exported-at "$now" --uploader "$UPLOADER" --apply \
    > "$OUT/sync-$n.log" 2>&1
  local sync_rc=$?; set -e
  sync_s=$((SECONDS - t1))
  cat "$OUT/sync-$n.log"
  expect "SOH sync applied" "exit $sync_rc, wall ${sync_s} s" -- \
    test $sync_rc -eq 0 -a -n "$(grep '^Applied in' "$OUT/sync-$n.log")"
  echo "TIMING cycle=$n soh_sync_wall_s=$sync_s" >> "$RESULTS"

  log "8. [$n] The same SOH again: a no-op"
  local syncs_before; syncs_before="$(psql_db "SELECT count(*) FROM ptmapper_stocksync")"
  local moves_before; moves_before="$(psql_db "SELECT count(*) FROM ptmapper_stocksyncrow")"
  set +e
  manage sync_soh --store "$STORE" --soh "$SOH_IN" --exported-at "$now" --uploader "$UPLOADER" --apply \
    > "$OUT/sync-again-$n.log" 2>&1
  local again_rc=$?; set -e
  cat "$OUT/sync-again-$n.log"
  local syncs_after moves_after
  syncs_after="$(psql_db "SELECT count(*) FROM ptmapper_stocksync")"
  moves_after="$(psql_db "SELECT count(*) FROM ptmapper_stocksyncrow")"
  expect "same SOH is a no-op" "exit $again_rc; syncs $syncs_before->$syncs_after, rows $moves_before->$moves_after" -- \
    test $again_rc -eq 0 -a "$syncs_before" = "$syncs_after" -a "$moves_before" = "$moves_after" \
         -a -n "$(grep 'nothing to apply' "$OUT/sync-again-$n.log")"

  log "9. [$n] An older SOH export: refused"
  local older; older="$(python3 -c "import datetime,sys; t=datetime.datetime.fromisoformat(sys.argv[1]); print((t-datetime.timedelta(days=1)).isoformat())" "$now")"
  # A different file (an earlier export), so it is judged on its time, not as a repeat.
  "${DC[@]}" exec -T api python -c "
import shutil, zipfile
shutil.copy('$SOH_IN', '/tmp/SOH_older.xlsx')
with zipfile.ZipFile('/tmp/SOH_older.xlsx', 'a') as z:
    z.comment = b'rehearsal: an earlier export'
"
  set +e
  manage sync_soh --store "$STORE" --soh /tmp/SOH_older.xlsx --exported-at "$older" --uploader "$UPLOADER" --apply \
    > "$OUT/sync-older-$n.log" 2>&1
  local older_rc=$?
  manage sync_soh --store "$STORE" --soh "$SOH_IN" --exported-at "$older" --uploader "$UPLOADER" --apply \
    > "$OUT/sync-older-same-$n.log" 2>&1
  local older_same_rc=$?; set -e
  cat "$OUT/sync-older-$n.log" "$OUT/sync-older-same-$n.log"
  expect "older SOH refused STOCK_SYNC_STALE" "exit $older_rc" -- \
    test $older_rc -ne 0 -a -n "$(grep STOCK_SYNC_STALE "$OUT/sync-older-$n.log")"
  expect "same SOH file with an older time refused" "exit $older_same_rc: $(grep -o '[A-Z_]\{6,\}' "$OUT/sync-older-same-$n.log" | head -1)" -- \
    test $older_same_rc -ne 0

  log "10. [$n] rehearsal_assert --phase sync"
  set +e
  "${DC[@]}" exec -T api python /rehearsal/rehearsal_assert.py --phase sync --store "$STORE" --pt "$PT_IN" \
    --soh "$SOH_IN" | tee "$OUT/assert-sync-$n.txt"
  arc=${PIPESTATUS[0]}; set -e
  record "rehearsal_assert --phase sync" "$([[ $arc -eq 0 ]] && echo PASS || echo FAIL)" "exit $arc"
}

setup_fresh
run_cycle 1
if [[ $TWICE -eq 1 ]]; then
  restore_baseline
  run_cycle 2
fi

log "Summary ($(git rev-parse --short HEAD)$(git diff --quiet HEAD -- . ':!docs/wardrobe-ratu-road' || echo ', with local changes'))"
column -t -s $'\t' "$RESULTS" || cat "$RESULTS"
for n in 1 2; do
  for phase in opening sync; do
    f="$OUT/assert-$phase-$n.txt"
    [[ -f "$f" ]] && grep -E "^[0-9]+ PASS, [0-9]+ FAIL" "$f" | sed "s/^/cycle $n assert: /"
  done
done
if grep -q $'\tFAIL\t' "$RESULTS"; then
  echo "REHEARSAL FAILED"; exit 1
fi
echo "REHEARSAL PASSED"
