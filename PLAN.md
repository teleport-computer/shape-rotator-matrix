# PLAN — issue #3: replace knock-rule flow with ephemeral per-code welcome rooms

Derived from the issue's `## Acceptance`. Base: `staging`. Branch: `ready-3`.

## Goal
`/join?code=…` stops pushing users through Element's knock UI (or the haiku
lobby): each code maps to ONE public `#welcome-<hash>:server` room, minted
idempotently by `POST /join/api`, consumed by the first user join (not by the
POST), which triggers the space invite + a confirmation message. Rooms are
tombstoned + forgotten on a timer. The knock path (knock → vetting room →
haiku → space) stays alive untouched.

## Work items
- [x] `knock-approver/approver.py`
  - `WELCOME_PATH` (`/data/welcome_rooms.json`), keyed by code:
    `{room_id, room_alias, created_at, joined_by?, joined_at?}`.
  - `POST /join/api`: distinct `invalid_code` / `code_exhausted`; idempotent
    mint per live code (room-liveness = no `m.room.tombstone`, bot still in
    room); NO decrement at POST time; response is `{"room_alias": …}`.
  - Alias localpart `welcome-<sha256(code+secret)[:8]>`, secret generated
    once and persisted (`/data/welcome_secret`) so aliases are unguessable
    offline (issue's open question, resolved as suggested).
  - Sync loop (onboarding bot): first non-bot `membership=join` in a mapped
    room → decrement + persist exactly once, invite joiner to the space,
    post "invite sent — accept it in Element and you're in.", set
    `joined_by`/`joined_at`. Guard on `joined_by` makes replays no-ops.
  - Cleanup each cycle: joined room reaped at `joined_at +
    WELCOME_JOINED_TTL_SEC` (30m), unused at `created_at +
    WELCOME_ROOM_TTL_SEC` (48h) — kick members, tombstone, leave, delete
    mapping; on failure keep the mapping and retry.
  - One-shot migration: leave every room in legacy `/data/lobby.json`,
    then remove the file (no orphaned haiku lobbies).
  - `announce_lobby_events` → `announce_welcome_events` (join announcements
    only; unused-expiry = ghost, suppressed), `_stats_last_24h` updated to
    the new audit events + pending = unjoined welcome rooms.
  - Kept from the old lobby impl deliberately: `room_version: "11"`,
    `world_readable` history, `visibility: private` (documented continuwuity
    federation/visibility bugs — see comments in `_create_welcome_room`).
- [x] `landing/join.html` — welcome-room copy, button label, error text
  ("this code is no longer valid, ask whoever sent you the link for a new
  one."), reads `room_alias` from the response, links `matrix.to/#/<alias>`.
- [x] `landing/nginx.conf` — `/join/api` already routed; comment updated.
- [x] `tests/smoke.py` — welcome path: invalid/exhausted errors, idempotent
  double-mint of a single-use code (proves POST doesn't decrement), join →
  confirmation + space invite, outsider joins the live room and gets
  nothing, cleanup → `code_exhausted` (proves join-time decrement + mapping
  removal).
- [x] `tests/lobby_e2e.py` — rewritten: two users via two codes, E2EE
  round-trip in the encrypted child, already-member redo path.
- [x] `tests/announce_unit.py` — rewritten against the welcome store (flood
  regression preserved).
- [x] `tests/run_in_runner.sh`, `tests/docker-compose.test.yml`,
  `dev/bootstrap.py` — welcome codes + short CI TTLs.
- [x] `README.md`, `MATRIX_ONBOARDING.md` — flow docs corrected.

## Verification
- `bash tests/run_e2e.sh` (full local stack: continuwuity + bootstrap +
  approver + landing nginx + runner) — the repo's gate.
- CI runs the same on the PR, plus the path-filtered ephemeral CVM
  validation (landing/**, knock-approver/**, tests/smoke.py all match).
- Tier 2 walk with the envoy real-browser rig against the local stack
  (landing page → Element join → confirmation → space invite), screenshots
  to `.evidence/issue-3/`.

## Deliberate calls (vs the issue text / prior plan)
- The issue's `createRoom` sketch omits version/visibility/history details;
  the shipped lobby's hard-won `room_version 11` + `world_readable` +
  unlisted visibility are kept.
- The /join confirmation is exactly the issue's message; the welcome signup
  code / onboarding-doc link / child-room invites stay on the KNOCK path
  only (issue step 7: restricted-join covers children).
