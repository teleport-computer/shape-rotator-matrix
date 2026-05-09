#!/usr/bin/env bash
# Runs INSIDE the test-runner container. Source the env file the bootstrap
# container wrote, wait for the approver to be live, run every test, exit
# nonzero on the first failure.
set -euo pipefail

ENV_FILE=/shared/test.env
APPROVER=http://knock-approver:8001

echo "[runner] sourcing $ENV_FILE"
test -f "$ENV_FILE" || { echo "[runner] FAIL: $ENV_FILE not produced by bootstrap"; exit 1; }
set -a
. "$ENV_FILE"
set +a
# bootstrap.py exports HS pointing at continuwuity directly (that's what the
# approver wants); the runner instead must exercise the same entry point real
# clients hit, which is the landing nginx in front of everything. Override
# after sourcing.
export HS=http://landing:80
export HOMESERVER=$HS

echo "[runner] waiting for approver health"
for i in $(seq 1 60); do
  if curl -fsS "$APPROVER/health" >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS "$APPROVER/health" >/dev/null

echo "[runner] env summary:"
echo "  HS=$HS"
echo "  SPACE_ID=$SPACE_ID"
echo "  SPACE_CHILD_IDS=$SPACE_CHILD_IDS"
echo "  ADMIN_MXID=$ADMIN_MXID"

# Pure-logic unit tests for the approver. Don't need continuwuity or any
# /shared env — run first so a logic regression fails fast before the
# slower e2e tests boot.
echo "[runner] === announce_unit.py ==="
python3 tests/announce_unit.py

echo "[runner] === self_heal_unit.py ==="
python3 tests/self_heal_unit.py

# stdlib flow test (signup + knock-vetting). Uses landing nginx as HS so it
# hits both the matrix endpoints AND /signup/api in one shot.
echo "[runner] === smoke.py ==="
ADMIN_TOKEN="$ADMIN_TOKEN" \
  REG_TOKEN="$CONDUWUIT_REGISTRATION_TOKEN" \
  SIGNUP_CODE="$DEV_SIGNUP_CODE" \
  KNOCK_CODE="$DEV_KNOCK_CODE" \
  SPACE_ID="$SPACE_ID" \
  SPACE_CHILDREN="$SPACE_CHILD_IDS" \
  HOMESERVER="$HS" \
  python3 tests/smoke.py

# Real E2EE round-trip test of the new vetting flow.
echo "[runner] === vetting_e2e.py ==="
DEV_HS="$HS" \
  DEV_REG_TOKEN="$CONDUWUIT_REGISTRATION_TOKEN" \
  DEV_KNOCK_CODE="$DEV_KNOCK_CODE" \
  DEV_SIGNUP_CODE="$DEV_SIGNUP_CODE" \
  SPACE_ID="$SPACE_ID" \
  SPACE_CHILD_IDS="$SPACE_CHILD_IDS" \
  ADMIN_MXID="$ADMIN_MXID" \
  python3 tests/vetting_e2e.py

# Lobby flow: POST /join/api → fresh public room → haiku → space, with
# an E2EE round-trip in #bot-noise to prove the new path doesn't wedge
# crypto for users who arrive via the lobby instead of the knock.
echo "[runner] === lobby_e2e.py ==="
DEV_HS="$HS" \
  DEV_REG_TOKEN="$CONDUWUIT_REGISTRATION_TOKEN" \
  DEV_KNOCK_CODE="$DEV_KNOCK_CODE" \
  SPACE_ID="$SPACE_ID" \
  SPACE_CHILD_IDS="$SPACE_CHILD_IDS" \
  ADMIN_MXID="$ADMIN_MXID" \
  python3 tests/lobby_e2e.py

# E2EE admin-command test — verifies bot decrypts !mint in an encrypted
# room and replies encrypted. This is the regression gate for the
# mautrix-bot migration.
echo "[runner] === admin_e2ee.py ==="
DEV_HS="$HS" \
  DEV_REG_TOKEN="$CONDUWUIT_REGISTRATION_TOKEN" \
  ADMIN_COMMAND_ROOM="$ADMIN_COMMAND_ROOM" \
  ADMIN_TOKEN="$ADMIN_TOKEN" \
  ADMIN_MXID="$ADMIN_MXID" \
  python3 tests/admin_e2ee.py

# Paste A+B+C SAS verification end-to-end. **Informational**: the
# upstream SAS dance is tracked-flaky against continuwuity (issue #1) so
# we run the test for visibility but don't gate the PR on its outcome.
# The vetting flow's E2EE round-trip (above) is the real megolm gate.
echo "[runner] === sas_e2e.py === (informational; failures don't gate the PR)"
if DEV_HS="$HS" \
     DEV_REG_TOKEN="$CONDUWUIT_REGISTRATION_TOKEN" \
     DEV_SIGNUP_CODE="$DEV_SIGNUP_CODE" \
     python3 tests/sas_e2e.py; then
  echo "[runner] sas_e2e: PASS"
else
  echo "[runner] sas_e2e: FAIL (informational — see issue #1)"
fi

echo "[runner] all gating tests passed"
