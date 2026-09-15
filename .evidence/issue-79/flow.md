# Evidence — issue #79: filter expired sessions out of build_room_key_bundle (#76 chip 3)

Tier: **1 (backend behavior change, no user-visible UI).**

The Shape Rotator approver is a Matrix bot, not a web app, so it has no
`/_api/version` endpoint. Per the matrix lane convention (same as issue #78's
evidence), verification is the acceptance-test transcript against a real
continuwuity stack, driven through the production code paths. This file maps
the transcript (`transcript.txt`) to the issue's `## Acceptance` criteria and
states honestly how the "old" session was produced.

## What ran

```
bash tests/run_e2e.sh     # the full PR gate: fresh continuwuity + approver + landing + test-runner
```

Gate result: **EXIT 0 — all gating tests passed**, including the two the issue
names as regression guards, unmodified:
`history_bundle_e2e.py` ("MSC4268 bundle round-trip: 2 sessions exported,
uploaded, downloaded, imported, decrypted") and
`history_bundle_responder_e2e.py` ("inviter bundle imported; outsider
rejected; pre-join message decrypted").

The new acceptance test `tests/retention_bundle_e2e.py` (registered in
`tests/run_in_runner.sh`) passed **19/19**. Base commit `93e6eb0`
(origin/staging). Stack image digest in `devstack-versions.txt`.

## Acceptance criteria → evidence

| # | Criterion (from issue body) | Evidence (check name in transcript) |
|---|---|---|
| 1 | Retention room with a 90d policy; messages at T-100d and T-1d | `1a` (90d record: `window=7776000s`), `1b` (two distinct megolm sessions), `1c` (index: old at −100d, recent at −0d) |
| 2 | New member vetted in, receives the bundle | `2f` (`_send_room_key_bundle` production funnel, "sent to 1 device(s)"), `2g` (member joined after invite); invitee runs the production `responder.py` handler ("imported 1 room key sessions") |
| 3 | Recent message decrypts | `3c` — body matches `message at T-1d …` |
| 4 | Old message does NOT decrypt — missing/withheld session, not a delivery/server error | `4` — `SessionNotFound`, the mautrix missing-session exception |
| 5 | Control room with no `m.room.retention`: both messages decrypt | `5a` (no retention state event, no policy record), `5b` (bundle keeps both sessions, `withheld=0`), `5d`/`5e` (both decrypt) |
| 6 | `history_bundle_e2e.py` + `history_bundle_responder_e2e.py` still pass | both PASS in the same gate run, unmodified |

Extra assertions beyond the issue's list: `2a`–`2e` (expired session in
`withheld`, not `room_keys`, never both — the MSC4268 MUST NOT; code
`m.unauthorised`; reason names the `90d` window) and `3a`/`3b` (the crypto
store still holds every inbound session and the bot still decrypts the old
message after the build — proving the escrow/durability path was not pruned).

## How "T-100d" was produced (honest method note)

The CS API cannot backdate an event's `origin_server_ts`, so the 100-day-old
message is created by sending a real message in a second megolm session and
seeding that session's entry in the chip-1 age index to T-100d via the same
production `record_session()` the sync hook calls (it keeps the minimum ts).
The index is the only clock chip 3 reads — the crypto store carries no
timestamps, which is precisely why chip 1 (#77) built it. The filtering,
bundle upload, delivery, import, and decrypt outcomes are all real.

## Design choices (deliberate, vs the issue text / pre-plan)

- **Policy source**: the issue says "read the room's `m.room.retention`"; the
  implementation reads the write-once `RETENTION_PATH` record instead. Chip 2
  (#78) made that store the authoritative in-force policy precisely because
  wire state is mutable (its test proves the server accepts an attacker PL100
  PUT; the record is never overwritten). Reading live state would let a later
  state event weaken the enforced window. This follows the design chip 2's
  code comments already state for chip 3.
- **Withheld code**: `m.unauthorised` (Matrix spec: "the user/device is not
  allowed to have the key"). The bot *holds* the expired session and policy
  denies it to the recipient — `m.unavailable` ("device … does not have the
  requested key") would be false, and MSC4268's own `m.history_not_shared`
  is defined for sessions lacking the `shared_history` flag, a different
  condition.
- **Unindexed session in a retention room** (no age-index entry): withheld
  with its own reason ("age unknown … withheld as if expired") — fail closed
  and visible on the wire, rather than shipping a session whose age cannot be
  proven inside the window.

## What I could NOT verify (honest)

- **Prod deploy**: out of scope — this PR ships the enforcement; deploying to
  `mtrx.shaperotator.xyz` and creating a live retention room is the operator's
  promotion step.
- **A literally 100-day-old session on a live server**: impossible in a test
  run; simulated through the age index as described above.
- **Lane spec unreadable**: `/srv/swarm-inbox/paseo-batch/specs/matrix-ready-worker.md`
  is mode 0640 `amiller:amiller` on this box (the `specs/` deploy lost the
  setgid group, unlike the top-level files which are `amiller:swarm`). The
  CONSTITUTION worker loop and the sibling issues' conventions were followed
  instead; flagging the permission regression here since nothing alerts on it.
