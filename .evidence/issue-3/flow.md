# Issue #3 — welcome-room flow, Tier 2 walk (real browser, real Element)

Rig: the repo's **production `landing/nginx.conf`** + **`knock-approver/approver.py` from this
branch** (bind-mounted, unmodified) + local continuwuity + an Element Web instance, all in docker
on this box. Browser: real Firefox 136 on Xvfb, driven by **real pointer/keyboard events**
(xdotool) — no CDP/WebDriver (per the standing LESSONS rule). Screenshots grabbed with
`ffmpeg x11grab`. Every step below is grounded in the **nginx/homeserver access log line that
the browser generated at that moment**, quoted under each shot.

Users: `@welk2_1788024311:localhost:46167` (the walked user, registered fresh for this walk)
and `@welk_1788024311:localhost:46167` (an earlier API-level walk of the same code paths).
Codes: `dev-welcome-77a81a29fab7` (welk2's room, unconsumed before shot 05) and
`dev-welcome-7447bc9a9531` / `dev-welcome-dead` (landing states).

| Shot | What it shows | Server-side proof at capture time |
|---|---|---|
| `01-landing-welcome-ready.png` | `/join?code=<valid>` — new copy, "Open the Shape Rotator welcome room in Element →" button armed, status "Welcome room ready: #welcome-…" | `GET /join?code=dev-welcome-7447bc9a9531` 200 + `POST /join/api` 200 `{"room_alias": "#welcome-019e8c67:localhost:46167"}` (referrer = the join page) |
| `02-landing-dead-code.png` | `/join?code=<0-use code>` — "This code is no longer valid — ask whoever sent you the link for a new one." | `POST /join/api` → 403 `{"error": "code_exhausted"}` |
| `03-element-login.png` | Element Web (real client) signed-in flow for `@welk2…` | `POST /_matrix/client/v3/login` → 200 |
| `04-element-welcome-room-join.png` | Element's preview of the public welcome room (world-readable history: the pinned "welcome — sit tight…" message) with its plain **Join** control — no knock UI, nothing to paste | `GET /directory/room/#welcome-8f959187…` 200 + `initialSync` 200 |
| `05-element-confirmation.png` | Joined room: pinned welcome message **and the bot's confirmation "invite sent — accept it in Element and you're in."** in the timeline; room header shows 2 members | approver log: `[welcome] @welk2_1788025672:localhost:46167 joined via dev-welcome-77a81a29fab7 (uses_left=98)` — the join consumed the use and triggered the invite + confirmation |
| `06-element-space-invite.png` | The space invite card: "admin invites you — @admin:localhost:46167 — Shape Rotator (dev)" with Decline/Accept | invite issued by the approver's `_lobby_invite_to_space` (same log cycle as above) |
| `07-element-inside-space.png` | After clicking Accept: inside the space; Element surfaces the restricted-join child rooms ("#announcements-dev… Do you want to join it?") | `POST /_matrix/client/v3/join/!WjYrN_R3naLqs…` (the space) → 200 |

The child-room joins via the restricted rule are asserted over the real API in
`tests/smoke.py` ("joined all 3 children via restricted rule") rather than clicked one by one here.

## Acceptance assertions covered elsewhere in this PR
- **Idempotent mint / no decrement at POST**: `tests/smoke.py` mints a **single-use** code
  twice (same alias both times) and the code still works afterwards — two POSTs would have
  exhausted it under the old behavior.
- **`invalid_code` vs `code_exhausted`, no room created**: smoke asserts both bodies + 403s.
- **Join decrements exactly once, invites, posts the specified message**: smoke + the walk
  above; duplicate/replayed joins are no-ops (smoke: an outsider joining the live room gets
  no invite at all).
- **Cleanup (kick + tombstone + mapping delete) and re-mint**: smoke polls until the joiner is
  kicked, then until the same code mints a fresh room (new room_id under the deterministic
  alias). CI runs the approver with short TTLs for this (`WELCOME_JOINED_TTL_SEC=25`).
- **Liveness after silent bot loss (PR #91 review finding)**: a mapped room the bot has LEFT —
  no tombstone, so the tombstone read alone returned "alive" — must remint, not hand out the
  stale, unusable alias. `welcome_liveness_unit.py` covers the response shapes (6 cases);
  `lobby_e2e` "[eviction]" proves it over the real HS: the bot leaves a freshly minted room
  (an outside user's world_readable state read confirms 404/no tombstone) and the next POST
  returns the same alias now backed by a NEW room_id.
- **A hard-failing space invite must not burn the code (PR #91 review finding #2)**: the use
  used to be decremented and `joined_by` armed *before* the invite was attempted, so any
  non-200/403 status permanently consumed the code and suppressed retry. `welcome_consume_unit.py`
  pins the semantics — 500/429 consume nothing, arm no guard, post nothing; a retry
  then consumes exactly once; 403 already-member still consumes. `lobby_e2e` "[invite-fail]"
  proves it over the real HS: a federated joiner (the production norm) whose space invite the
  HS hard-fails with a real 500 M_UNKNOWN leaves `uses_remaining` untouched, no `joined_by`,
  `welcome_invite_failed` audited, no confirmation; the retry with a local joiner consumes
  exactly one use and delivers the invite + confirmation.
- **A failed confirmation post must not enable a second consumption (PR #91 review finding #3)**:
  the decrement used to be persisted before the confirmation and the `joined_by` guard, so a
  send failure or crash after the decrement left the guard unset — a replayed join re-invited
  and decremented again. The guard and the decrement are now persisted together, before the
  confirmation post (guard first, so a crash between the two adjacent writes strands a use
  unconsumed rather than consuming twice). `welcome_consume_unit.py` adds two cases: a 500 on
  the send still consumes exactly once with the guard on disk (the send raises loudly); a
  fault injected between the two saves leaves the use unconsumed and the guard armed.
- **Full gate**: `run_e2e.log` — announce/self-heal/trust/liveness/consume units, smoke 39/39,
  vetting_e2e, lobby_e2e 33/33 (two-code E2EE round-trip + already-member redo + eviction
  remint + invite-fail/retry), admin_e2ee, retention, escrow, history bundles: **all gating
  tests passed**.

## Honest limits
- Element shows an "unsupported browser" banner (Firefox 136 on Xvfb); it does not affect the
  flow. OCR was used to locate buttons because this worker session cannot view images; each
  screenshot's content is grounded in the quoted access-log lines captured at the same moment.
- The shared envoy/neko bridge rig was down during this run (`/health` reported
  `wsClients: 0` with commands queued for ~1h); the Xvfb+xdotool rig above was used instead —
  same real-browser, real-input-events requirement, no CDP.
