# PLAN — issue #79: filter expired sessions out of build_room_key_bundle (#76 chip 3)

Derived from the issue's `## Acceptance`. Base: `staging`. Branch: `ready-79`.

## Goal
`build_room_key_bundle()` (`knock-approver/approver.py`) returns every inbound
session for a room today. In a retention room (#78), sessions older than the
policy window must be left out of `room_keys` and reported in `withheld` with a
retention-specific reason. Rooms with no policy keep today's behavior exactly
(the #58 regression guard: `history_bundle_e2e.py` /
`history_bundle_responder_e2e.py` must keep passing unmodified). `ESCROW_PATH`
and the crypto store rows are not pruned — the escrow is the #60 durability
mechanism.

## Work items (from the issue)
- [x] Read the in-force retention policy for the room — the write-once
      `RETENTION_PATH` record (chip 2's designated source for chip 3), NOT the
      live `m.room.retention` state, which is mutable on the wire (#78 proved
      the server accepts attacker PUTs; only the bot's record is immutable).
- [x] Filter `room_keys` by the chip-1 index: `session_age_index(room_id)`
      gives each session's earliest origin_server_ts; a session whose earliest
      ts is before `now - max_lifetime_ms` is expired. A session with NO index
      entry in a retention room is withheld too (fail closed, named reason).
- [x] Populate `withheld` for policy-expired sessions: code `m.unauthorised`
      (spec: "the user/device is not allowed to have the key" — the bot holds
      the session; policy denies the recipient; `m.unavailable` would falsely
      claim the key is missing), reason names the retention window.
- [x] Prune only the outbound bundle: no `ESCROW_PATH` or crypto-row changes;
      the builder logs a `[room_key_bundle] retention window withheld N
      session(s)` line for operator visibility.

## Acceptance (restate) + how each is verified
A test in `tests/` against the dev stack (style of `history_e2ee_repro.py`) →
`tests/retention_bundle_e2e.py` (registered in `tests/run_in_runner.sh`).

1. Retention room with a 90d policy; messages at T-100d and T-1d.
   → factory (`_create_retention_room`) makes the room + record (1a); two
     distinct megolm sessions via outbound-rotation (1b); the T-100d age is
     seeded through the production `record_session()` (1c) — the CS API cannot
     backdate `origin_server_ts`, and the index is chip 3's only clock.
2. New member vetted in, receives the bundle.
   → bot invite + the production `_send_room_key_bundle` funnel; the invitee
     runs the production responder handler (2f, 2g).
3. Recent message decrypts. (3c)
4. Old message does not decrypt — `SessionNotFound` (missing/withheld
   session), not a delivery failure or server error. (4)
5. Control room with no `m.room.retention`: both messages decrypt.
   (5a–5e: state has no retention event and no record; bundle keeps every
   session with an empty `withheld` list; both decrypt for the invitee.)
6. `history_bundle_e2e.py` and `history_bundle_responder_e2e.py` still pass.
   → run unmodified by the full `tests/run_e2e.sh` gate alongside the new
     test.

Extra regression assertions in the same test: the expired session appears in
`withheld` but not `room_keys` and in never both (MSC4268 MUST NOT) (2a–2e);
the crypto store still holds every inbound session and the bot still decrypts
the old message after the build (3a, 3b) — proving the escrow path was not
pruned.
