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

echo "[runner] === admin_trust_unit.py ==="
python3 tests/admin_trust_unit.py

echo "[runner] === welcome_liveness_unit.py ==="
python3 tests/welcome_liveness_unit.py

echo "[runner] === welcome_consume_unit.py ==="
python3 tests/welcome_consume_unit.py

echo "[runner] === welcome_mint_unit.py ==="
python3 tests/welcome_mint_unit.py

# stdlib flow test (signup + knock-vetting + welcome rooms). Uses landing
# nginx as HS so it hits both the matrix endpoints AND /signup/api +
# /join/api in one shot.
echo "[runner] === smoke.py ==="
ADMIN_TOKEN="$ADMIN_TOKEN" \
  REG_TOKEN="$CONDUWUIT_REGISTRATION_TOKEN" \
  SIGNUP_CODE="$DEV_SIGNUP_CODE" \
  KNOCK_CODE="$DEV_KNOCK_CODE" \
  WELCOME_CODE="$DEV_WELCOME_CODE" \
  WELCOME_SINGLE="$DEV_WELCOME_SINGLE" \
  WELCOME_DEAD="$DEV_WELCOME_DEAD" \
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

# Welcome-room flow (issue #3): POST /join/api → public welcome room →
# plain Join → space invite, with an E2EE round-trip in #bot-noise to prove
# the new path doesn't wedge crypto for users who arrive via a welcome room
# instead of the knock. DEV_LOBBY_TOKEN is the room bot's own token: the
# stack runs the lobby flow on the MATRIX_TOKEN identity (no
# ONBOARDING_BOT_TOKEN configured), so that is what the eviction case uses
# to make the bot leave a room.
echo "[runner] === lobby_e2e.py ==="
DEV_HS="$HS" \
  DEV_REG_TOKEN="$CONDUWUIT_REGISTRATION_TOKEN" \
  DEV_WELCOME_CODE="$DEV_WELCOME_CODE" \
  DEV_WELCOME_CODE_2="$DEV_WELCOME_CODE_2" \
  DEV_WELCOME_CODE_3="$DEV_WELCOME_CODE_3" \
  DEV_LOBBY_TOKEN="$MATRIX_TOKEN" \
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

# Retention room factory (issue #78 / epic #76 chip 2): bot-created room with
# bot sole PL100, E2EE, m.room.retention, restricted space join, a pinned
# honest policy message, and an immutable in-force policy store. Import-based
# (like history_bundle_e2e) — it self-provisions a bot + space, so it needs no
# /shared env beyond DEV_HS + DEV_REG_TOKEN.
echo "[runner] === retention_room_e2e.py ==="
DEV_HS="$HS" \
  DEV_REG_TOKEN="$CONDUWUIT_REGISTRATION_TOKEN" \
  python3 tests/retention_room_e2e.py

# Escrow durability (issue #60): runs the ACTUAL approver export/wipe/import
# against a re-mint under a NEW device_id and proves the re-minted bot still
# decrypts a pre-wipe message. Permanent regression gate — a future self-heal
# edit that silently re-breaks history-key survival fails here.
echo "[runner] === escrow_durability.py ==="
DEV_HS="$HS" \
  DEV_REG_TOKEN="$CONDUWUIT_REGISTRATION_TOKEN" \
  SPACE_ID="$SPACE_ID" \
  SPACE_CHILD_IDS="$SPACE_CHILD_IDS" \
  ADMIN_COMMAND_ROOM="$ADMIN_COMMAND_ROOM" \
  python3 tests/escrow_durability.py

echo "[runner] === history_bundle_e2e.py ==="
DEV_HS="$HS" \
  DEV_REG_TOKEN="$CONDUWUIT_REGISTRATION_TOKEN" \
  python3 tests/history_bundle_e2e.py

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

echo "[runner] === history_bundle_responder_e2e.py ==="
DEV_HS="$HS" \
  DEV_REG_TOKEN="$CONDUWUIT_REGISTRATION_TOKEN" \
  python3 tests/history_bundle_responder_e2e.py

# Issue #62: MSC4268 bundle sending wired into approver.py's own invite
# paths (_invite_to_children etc), not a hand-built bundle. Also proves
# the /data/endorsements.jsonl web-of-trust edge gets recorded.
echo "[runner] === history_bundle_invite_e2e.py ==="
DEV_HS="$HS" \
  DEV_REG_TOKEN="$CONDUWUIT_REGISTRATION_TOKEN" \
  python3 tests/history_bundle_invite_e2e.py

echo "[runner] all gating tests passed"
