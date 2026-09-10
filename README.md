# shape-rotator-matrix

Deployment state for `mtrx.shaperotator.xyz` — the Shape Rotator community's
Matrix homeserver running in a Phala TEE.

This is a **specific instance**, not a generic example — for the example, see
`../dstack-matrix/`. What's Shape Rotator-specific here:

- Custom landing page at `/` and per-invite `/join?code=…` page (served by a
  small nginx sidecar in front of continuwuity)
- `knock-approver`: a tiny Python service with three surfaces —
  - **welcome rooms** (the `/join?code=…` path, issue #3): each invite code
    maps to one public `#welcome-<hash>:…` room minted idempotently by
    `POST /join/api`. Joining the room consumes one use of the code and
    triggers the space invite; no knock UI and no code-pasting.
  - **knock vetting** (still alive for experienced Matrix users): a Matrix
    `knock` on the space whose `reason` matches a code gets a fresh
    per-knock vetting room with a wikipedia-fact haiku captcha; a valid
    haiku reply earns the space invite.
  - **signup proxy** (`POST /signup/api`): code-gated account creation on
    this homeserver with an automatic space invite.
- dstack-ingress with Namecheap DNS-01 Let's Encrypt for the custom domain

## Layout

```
docker-compose.yml    compose bundling dstack-ingress + continuwuity + landing + knock-approver
continuwuity/         (reserved; currently server is configured purely via env vars)
landing/
  index.html          public landing page
  join.html           /join?code=… page (mints the code's welcome room, links it)
  signup.html         /signup page (form for creating a homeserver-hosted identity)
  nginx.conf          routes / /join /signup to static files, /signup/api and
                      /join/api to the approver service, everything else to
                      continuwuity
knock-approver/
  approver.py         (a) long-polls /sync twice — once as the main bot for
                      knocks (per-knock vetting room + haiku captcha, state in
                      /data/vetting.json) and admin commands, once as the
                      onboarding bot for welcome rooms (state in
                      /data/welcome_rooms.json); (b) aiohttp.web server on
                      :8001 exposing POST /join/api (code → welcome-room
                      alias) and POST /signup/api (code-gated registration
                      with the server-side CONDUWUIT_REGISTRATION_TOKEN)
skills/
  matrix-invite-join/ Hermes-style skill for agents to self-onboard via a
                      /join?code=… link (knock + accept, using their own token)
deploy/
  encode_env.sh       refreshes *_B64 env entries from the plaintext sources
.env.example          documented env vars; real .env is gitignored
```

## Deploy

```bash
# 1. First time: copy the template and fill in secrets (Namecheap keys,
#    registration token, knock-approver token, initial codes).
cp .env.example .env
$EDITOR .env

# 2. Re-encode the landing pages + approver into the *_B64 env entries.
./deploy/encode_env.sh

# 3. Push to the CVM.
phala deploy --cvm-id dstack-matrix -c docker-compose.yml -e .env
```

The CVM retains state across redeploys via the named volumes
(`continuwuity-data`, `cert-data`, `knock-data`). Do **not** delete the CVM
to redeploy — you'll lose the Matrix database and have to start fresh.

## Space + room layout

- Space:         `!4FL8uL5OEYLATG1VH4wC2CD3pfIV6BMFId9VT7rmm-g`  (`#shape-rotator:mtrx.shaperotator.xyz`)
- General:       `!z85RFatK8w0f04i8yVOCidnYRKXlZuRjK4kYkdXVhUc`
- Announcements: `!9p9ZAr8CFo8WjD8g0hKv_1sOewNWt0zTBCWMAkWnLxo`
- Bot Noise:     `!a8L-8zCDgQZhddUWkb4FYkCVjPBu0lY6QwtLVBXIRXc`

Join rules:
- Space: `knock`
- Child rooms: `restricted` (auto-join for anyone already in the space)

## Two onboarding paths

There are two distinct ways to end up in the Shape Rotator community, and
they use different kinds of codes. Don't confuse them.

**1. Invite code** (`/join?code=XYZ`) — for people who already have a Matrix
account somewhere (matrix.org, their own server, anywhere federated). They
open the link, click through to their code's public welcome room, and hit
its plain Join button — the bot invites them into the space from there.
**No account on this server is created.** Low commitment.

**2. Signup code** (`/signup` form) — for people or agents who want an
`@name:mtrx.shaperotator.xyz` identity hosted in this TEE homeserver. The
form POSTs to `/signup/api` which holds the real continuwuity registration
token server-side, creates the account, and auto-invites the new user to the
space. Higher commitment — produces a durable identity attested by this server.

Seed both with `INITIAL_CODES` / `INITIAL_SIGNUP_CODES` env vars on first start.
After that, edit `/data/codes.json` / `/data/signup_codes.json` directly on
the CVM (SSH + docker exec) to add more.

## Invite flow (UX)

1. Admin generates a code and sends `https://mtrx.shaperotator.xyz/join?code=XYZ`
   to the new member.
2. They open it → the page calls `POST /join/api {"code": XYZ}` and shows
   "Open the Shape Rotator welcome room in Element →".
3. Element opens on the code's public `#welcome-<hash>:mtrx.shaperotator.xyz`
   room — a plain **Join** button, no knock UI, nothing to paste. The room's
   alias localpart is `welcome-<sha256(code+server_secret)[:8]>`, so it
   can't be guessed from the code.
4. `knock-approver`'s welcome /sync loop (as `@onboarding-bot`) sees the
   first `membership=join` in a mapped welcome room: it invites the joiner
   to the space, and only once that invite went out consumes one use of
   the code (`uses_remaining` — neither the POST nor a failed space invite
   consumes anything, so a clicked-but-abandoned link never burns the code
   and a joiner whose invite failed can rejoin to retry) and posts "invite
   sent — accept it in Element and you're in." in the room.
   Duplicate/replayed joins are no-ops.
5. Accepting the space invite joins them in; the `restricted` rule on
   child rooms lets them auto-join General / Announcements / Bot Noise.
6. Cleanup: a joined welcome room is kicked + tombstoned + forgotten 30
   minutes after the join (`WELCOME_JOINED_TTL_SEC`); an unused one 48
   hours after minting (`WELCOME_ROOM_TTL_SEC`). State lives in
   `/data/welcome_rooms.json` as `{code: {room_id, room_alias, created_at,
   joined_by?, joined_at?}}`.

Experienced Matrix users who prefer knocking can still knock the space
with the code as the reason: that path (`knock → per-knock vetting room →
haiku captcha → space invite`, state in `/data/vetting.json`) is unchanged
— `/join` simply no longer points users at it.

## Managing invite codes

Codes live in a named volume (`knock-data:/data/codes.json`) inside the CVM.
Initial codes are seeded from `INITIAL_CODES` in `.env` on first start —
subsequent restarts only add codes that aren't already in the file (existing
codes keep their used count).

To add more codes without redeploying, you'll currently need to SSH into the
CVM and edit `/data/codes.json` directly. A tiny admin Matrix command inside
the approver is a nice-to-have next step.

File format:

```json
{
  "abc123xyz": {"uses_remaining": 5, "label": "batch A"},
  "hfuy89kl":  {"uses_remaining": 1, "label": "one-shot for X"}
}
```

All approvals and rejections are appended to `/data/log.jsonl`.

## Admin command room and E2EE decision

Issue #7 uses the mautrix-based approver for the admin command room, so the room
may be encrypted; the bot decrypts incoming commands and encrypts its replies.
This is the v1 decision rather than requiring a special cleartext admin room.
Each human admin uses their own MXID and must have power level 50 or higher.
Commands are limited to the configured room, while `!kick`, `!ban`, and `!unban`
apply to the Shape Rotator space. `!stats` reports the last 24 hours from the
approver audit log and current vetting/welcome state.

## Secrets that belong in .env

- `NAMECHEAP_USERNAME`, `NAMECHEAP_API_KEY` — DNS-01 for Let's Encrypt
- `REGISTRATION_TOKEN` — continuwuity signup gate (for bots/agents; humans
  should create a matrix.org account and federate in)
- `KNOCK_APPROVER_TOKEN` — access token of a Matrix user with PL ≥ 50 in
  the space (currently `@shape-rotator-2:mtrx.shaperotator.xyz`)
- `DSTACK_AUTHORIZED_KEYS` — ssh pubkey for `phala ssh` access

Rotate any of them by updating `.env` and redeploying.
