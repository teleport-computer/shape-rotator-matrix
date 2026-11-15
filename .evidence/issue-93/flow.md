# Issue #93 — sync-handler exceptions + /sync cursor retry (Tier 1)

Backend behavior change with no UI surface. Evidence = focused unit + live
transcripts against a real continuwuity with real megolm traffic, produced by
`tests/sync_cursor_unit.py` and `tests/sync_cursor_live.py` (both committed).
`/_api/version` pin: N/A — this repo serves no such endpoint (same convention
as merged #86/#87 and PR #92's rework note).

## What was actually wrong (three layers, not one)

The issue named layer 3. The live run found layers 1–2 underneath it:

1. **mautrix swallows every handler exception.** `Syncer._catch_errors`
   (mautrix/client/syncer.py:262) catches `Exception`, logs
   "Failed to run handler", returns normally. So *no* gather — with or
   without `return_exceptions=True` — can ever see a handler failure.
2. **the room-key handler is not in the gather at all.** OlmMachine registers
   `handle_to_device_event` with the default `wait_sync=False`
   (mautrix/crypto/machine.py:112) → dispatched via `background_task.create`,
   detached from `handle_sync`'s returned task list.
3. **`next_batch` persisted before handlers ran** (the bug the issue named).

Combined effect, reproduced live (transcript-live-red.txt): a transient
crypto.db write error while storing a room key → exception swallowed → cursor
advances → the to-device key share is consumed by the server → the megolm key
is lost **forever** → "Failed to decrypt megolm event: no session with given
ID" on every later message. That is the production outage signature.

## Fix

- `bridge/mx.py`: `_FailFastClient(Client)` keeps mautrix's log line and
  re-raises in `_catch_errors`; `make_client` re-registers every handler
  `wait_sync=True` (including the crypto setter's automatic decryptor);
  `sync_once` gathers plain (no `return_exceptions`) and persists
  `next_batch` only after all handlers succeed.
- `bridge/relay.py`: `_mx_sync` gets the same ordering (gather → persist).
  Shutdown/cleanup gathers (`mx.py::_shutdown`, `relay.py::run` finally) are
  deliberately untouched — they reap cancelled tasks, not sync results.

## Acceptance mapping

| Issue criterion | Evidence |
|---|---|
| 1. every sync-handler gather exception logged w/ details or propagated; a failing Olm/room-key handler never silently discarded | `_FailFastClient` logs the full traceback (`log.exception`) AND re-raises; propagation asserted in transcript-unit-green.txt (raises, `puts == []`) and proven on the real failure path in transcript-live-green.txt (`sync_once raised OperationalError: database is locked`, from `update_session` inside `handle_to_device_event`) |
| 2. cursor not persisted on handler failure; previous cursor readable; same batch retried; persisted on success | unit: retry syncs from `CURSOR_OLD`, advances exactly once on success. live: cursor `969→969` across the failure, `→976` after recovery; after the client restart the retried batch stores the key and M2 decrypts. live-red (pre-fix code): cursor advanced to 1187 past the failed batch and M2 never decrypted — the outage reproduced and gone |
| 3. after redeploy, failure visible in live relay log; after handler succeeds a new #shape-rotator-general message relays | **Not verifiable from this box** (see below). Local equivalent proven: the induced failure surfaces as an exception + traceback in the process log, and recovery completes after restart. |

## One nuance the fix surfaces (documented in MATRIX_ONBOARDING.md)

Olm to-device messages are at-most-once per process: the first `decrypt`
ratchets the session even when *persisting* it failed, so the in-process
retry of an olm-class failure raises `MatchingSessionDecryptionError`
(transcript-live-green.txt shows it). The held cursor is what makes a process
**restart** recover the key: fresh client → pre-ratchet session from the
store → re-delivered batch decrypts → key stored. Megolm-class store failures
(`put_group_session`) are idempotent upserts and replay in-process.

## Not verifiable here (operator steps)

This checkout has no `~/.shape-bridge-bot/`, no `~/.teleport-travel/`
(test venue or production fixtures), no Telegram session, and no `phala` CLI
— `bridge/acceptance.sh`'s live round-trip and the pod.dstack redeploy are
undrivable from here (checked by direct `ls`, not assumed). Remaining steps
for the operator: redeploy the relay on pod.dstack from this branch; the
relay log should now show `Failed to run handler` tracebacks + `sync error:
...` lines naming the real store failure (instead of silence) when the next
key share fails, and a restart after the underlying error clears re-relays
new #shape-rotator-general messages.

## Transcript files

- `transcript-unit-red.txt` — unit suite against pre-fix bridge code: FAIL
  (exit 1) — failures swallowed, cursor persisted, wiring missing.
- `transcript-unit-green.txt` — unit suite (5/5, exit 0) + self_heal_unit +
  sas_in_room_unit green (no regression).
- `transcript-live-red.txt` — live dev-stack run against pre-fix code
  (exit 1): no exception surfaced, cursor advanced past the failed batch,
  key lost forever.
- `transcript-live-green.txt` — live dev-stack run against this branch
  (7/7, exit 0): raised, cursor held, M2 undecryptable while held,
  restart-retry stored the key, M2 decrypted, cursor advanced.
