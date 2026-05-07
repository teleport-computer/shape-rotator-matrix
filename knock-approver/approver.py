"""Auto-approve Matrix knocks on the Shape Rotator space, AND proxy signups.

Three responsibilities, all running in one process:

1. **Knock approver** (long-running /sync loop).
   Watches the space for membership=knock events. When the knock reason matches
   an entry in /data/codes.json, POSTs /invite to approve.

2. **Lobby door** (HTTP /join/api + sync-loop handler).
   POST /join/api {code} validates the code, mints a fresh public room with a
   random alias, returns the matrix.to URL. The user clicks through, joins
   instantly (no Element knock UI), the bot sees the join in /sync, posts a
   wikipedia haiku challenge, and on success invites the user to the space
   and leaves the lobby room (which then dies).

3. **Signup auth proxy** (HTTP /signup/api).
   POST /signup/api  body: {"code", "username", "password"}
   Validates code against /data/signup_codes.json, completes continuwuity
   registration using the server-side CONDUWUIT_REGISTRATION_TOKEN (never
   exposed to clients), and auto-invites the new account to the space.

Env:
  HS                           homeserver URL (https://mtrx.shaperotator.xyz)
  MATRIX_TOKEN                 access token for a user with PL >= 50 in the space
  SPACE_ID                     unsuffixed space room id
  CONDUWUIT_REGISTRATION_TOKEN shared reg token (kept server-side)
  SERVER_NAME                  homeserver name for room aliases (e.g. mtrx.shaperotator.xyz)
  INITIAL_CODES                JSON seed for knock codes
  INITIAL_SIGNUP_CODES         JSON seed for signup codes

State files on the knock-data volume:
  /data/codes.json          knock codes
  /data/signup_codes.json   signup codes
  /data/lobby.json          live lobby rooms (per-/join/api room)
  /data/log.jsonl           audit log
  /data/sync_since.txt      /sync cursor
"""
import asyncio, base64, json, os, secrets, sys, time, urllib.parse
from pathlib import Path
import aiohttp
from aiohttp import web
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

# mautrix is the E2EE-aware Matrix client. Keep the imports top-level so a
# missing dep blows up at process start (with a clear traceback) rather
# than mid-sync. The HTTP /signup/api endpoint also runs in this process
# and uses raw aiohttp — those flows don't need mautrix.
from mautrix.api import HTTPAPI as _MAU_HTTPAPI
from mautrix.client import Client as _MAU_Client
from mautrix.client.state_store import (
    MemoryStateStore as _MAU_MemoryStateStore,
    MemorySyncStore as _MAU_MemorySyncStore,
)
from mautrix.types import (
    UserID as _MAU_UserID,
    EventType as _MAU_EventType,
    MessageType as _MAU_MessageType,
    TextMessageEventContent as _MAU_TextContent,
    TrustState as _MAU_TrustState,
)
from mautrix.crypto import OlmMachine as _MAU_OlmMachine
from mautrix.crypto.store.asyncpg import PgCryptoStore as _MAU_PgCryptoStore
from mautrix.util.async_db import Database as _MAU_Database

HS            = os.environ["HS"].rstrip("/")
# Public-facing URL returned to signup clients (the client needs to point Element
# at the public name, not the internal docker hostname).
HS_PUBLIC     = os.environ.get("HS_PUBLIC", HS).rstrip("/")
TOKEN         = os.environ["MATRIX_TOKEN"]
SPACE_ID      = os.environ["SPACE_ID"]
REG_TOKEN     = os.environ.get("CONDUWUIT_REGISTRATION_TOKEN", "")
CODES_PATH    = Path(os.environ.get("CODES_PATH",        "/data/codes.json"))
SIGNUP_PATH   = Path(os.environ.get("SIGNUP_CODES_PATH", "/data/signup_codes.json"))
LOG_PATH      = Path(os.environ.get("LOG_PATH",          "/data/log.jsonl"))
SYNC_STATE    = Path(os.environ.get("SYNC_STATE",        "/data/sync_since.txt"))
HTTP_PORT     = int(os.environ.get("HTTP_PORT", "8001"))
# Bot's E2EE crypto store (megolm sessions, identity keys, peer device keys).
# Lives on the same /data volume so it survives container restarts.
CRYPTO_DB     = Path(os.environ.get("CRYPTO_DB", "/data/bot_crypto.db"))

# Comma-separated list of space-child room IDs that a freshly-signed-up user
# should auto-join via the restricted rule. Typically: general, announcements,
# bot-noise. IDs MUST be unsuffixed (!foo, not !foo:server.tld).
SPACE_CHILD_IDS = [r.strip() for r in os.environ.get("SPACE_CHILD_IDS", "").split(",") if r.strip()]

# Default inviter MXID to DM from the new account when someone signs up.
# Per-code override: set "inviter" on the signup_codes.json entry.
ONBOARDING_INVITER_MXID = os.environ.get("ONBOARDING_INVITER_MXID", "").strip()

AUTH = {"Authorization": f"Bearer {TOKEN}"}

# Dedicated onboarding bot identity for the lobby flow. Different mxid from
# MATRIX_TOKEN's bot so the hermes-staging Claude agent (which shares the
# MATRIX_TOKEN mxid) auto-leaving a non-E2EE lobby doesn't evict the bot
# from its own room. Falls back to MATRIX_TOKEN for graceful rollout if
# the env var isn't configured.
LOBBY_TOKEN = os.environ.get("ONBOARDING_BOT_TOKEN", "").strip() or TOKEN
LOBBY_AUTH  = {"Authorization": f"Bearer {LOBBY_TOKEN}"}


# --- JSON-file helpers ---

def _load(path):
    if path.exists():
        return json.loads(path.read_text())
    return {}

def _save(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(path)

def audit(event):
    event["ts"] = time.time()
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(event) + "\n")

def merge_seed(path, env_key):
    """Merge JSON from env var into the codes file; only adds missing keys."""
    seed = os.environ.get(env_key, "").strip()
    if not seed:
        return
    try:
        data = json.loads(seed)
    except Exception as e:
        print(f"{env_key} parse error: {e}", flush=True)
        return
    existing = _load(path)
    added = 0
    for k, v in data.items():
        if k not in existing:
            existing[k] = v
            added += 1
    if added:
        _save(path, existing)
        print(f"seeded {added} new entries into {path.name} from {env_key}", flush=True)


# --- Knock approval (per-knock vetting room with a haiku captcha) ---
#
# A valid knock no longer invites straight to the space. Instead the approver
# creates a fresh 1:1 vetting room (invite-only, the knocker is the only
# invitee) and posts a wikipedia-fact haiku challenge. Once the knocker joins
# and replies with a 3-line haiku that contains the required keyword, the bot
# invites them to the space — and the existing `restricted` join rule on
# child rooms takes over from there.

VETTING_PATH      = Path(os.environ.get("VETTING_PATH",       "/data/vetting.json"))
VETTING_TIMEOUT   = int(os.environ.get("VETTING_TIMEOUT_SEC", "7200"))
VETTING_MAX_TRIES = int(os.environ.get("VETTING_MAX_TRIES",   "3"))

# Lobby flow — POST /join/api creates a fresh public room per code use.
LOBBY_PATH         = Path(os.environ.get("LOBBY_PATH",        "/data/lobby.json"))
LOBBY_TIMEOUT      = int(os.environ.get("LOBBY_TIMEOUT_SEC",  "7200"))
LOBBY_MAX_TRIES    = int(os.environ.get("LOBBY_MAX_TRIES",    "3"))
# Federated users (matrix.org) can fast-join the lobby before continuwuity
# has finished propagating room state outward. The first challenge message
# posted ~immediately after observing the join can race that propagation
# and never render in the user's client (confirmed via lsdan screenshot
# 2026-05-04). Mitigations:
#   LOBBY_CHALLENGE_DELAY_SEC: wait this long after seeing the join before
#                              posting the first challenge.
#   LOBBY_RESEND_AFTER_SEC:    if the user has not replied this many seconds
#                              after the first send, repost the challenge
#                              once. Capped at LOBBY_MAX_RESENDS=1 per user.
LOBBY_CHALLENGE_DELAY = int(os.environ.get("LOBBY_CHALLENGE_DELAY_SEC", "5"))
LOBBY_RESEND_AFTER    = int(os.environ.get("LOBBY_RESEND_AFTER_SEC", "120"))
LOBBY_MAX_RESENDS     = 1
# After successful promotion (lobby OR vetting), mint a multi-use signup
# code so the new member can register accounts/agents on this server
# without having to ping an admin. 0 disables the feature.
LOBBY_WELCOME_CODE_USES = int(os.environ.get("LOBBY_WELCOME_CODE_USES", "10"))
LOBBY_ALIAS_PREFIX = os.environ.get("LOBBY_ALIAS_PREFIX", "shape-rotator-lobby-")
# Server name for room aliases. May be overridden by env; otherwise resolved
# at startup from /whoami (parsing the homeserver's view of OUR_MXID).
SERVER_NAME        = os.environ.get("SERVER_NAME", "").strip()

# Stop-words too generic to use as the haiku-keyword constraint.
_STOPWORDS = {"with", "from", "that", "this", "their", "have", "been",
              "were", "into", "over", "when", "what", "where", "which",
              "would", "could", "should", "about", "after", "before"}


async def _fetch_wiki_challenge():
    """Random wikipedia article -> (title, longest non-stopword >=4-char alpha word).

    Retries cover three real failure modes seen in the e2e suite:
      - title with no usable candidate (all short / non-alpha / stopword)
      - wikipedia rate-limit (429, returns text/plain not JSON → ContentTypeError)
      - transient network / 5xx
    Falls back to ('Wikipedia', 'wikipedia') so /join/api never 500s.
    """
    url = "https://en.wikipedia.org/api/rest_v1/page/random/summary"
    headers = {"User-Agent": "shape-rotator-vetting/1.0", "Accept": "application/json"}
    title = ""
    async with aiohttp.ClientSession(headers=headers) as s:
        for attempt in range(5):
            try:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status != 200:
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                    body = await r.json()
            except (aiohttp.ClientError, asyncio.TimeoutError,
                    json.JSONDecodeError) as e:
                print(f"[wiki] attempt {attempt} failed: "
                      f"{type(e).__name__}: {str(e)[:120]}", flush=True)
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            title = body.get("title", "") or title
            words = [w.strip(".,;:'\"()[]") for w in title.split()]
            candidates = [w for w in words
                          if len(w) >= 4 and w.isalpha() and w.lower() not in _STOPWORDS]
            if candidates:
                return title, max(candidates, key=len)
    return title or "Wikipedia", "wikipedia"


async def _send_msg(client, room_id, text):
    """Send a plain text message. Auto-encrypts if room has m.room.encryption."""
    content = _MAU_TextContent(msgtype=_MAU_MessageType.TEXT, body=text)
    return await client.send_message_event(room_id, _MAU_EventType.ROOM_MESSAGE, content)


async def _send_msg_raw(room_id, text):
    """Send a cleartext m.room.message via raw HTTP as the lobby/onboarding
    bot. Bypasses mautrix encryption logic (lobbies are public + cleartext
    by design) and uses LOBBY_AUTH so it's the dedicated onboarding-bot
    identity, not the shared MATRIX_TOKEN bot that hermes-staging's Claude
    agent also uses."""
    txn = f"sr-lobby-{secrets.token_hex(8)}"
    body = {"msgtype": "m.text", "body": text}
    async with aiohttp.ClientSession(
        headers={**LOBBY_AUTH, "Content-Type": "application/json"}
    ) as s:
        url = (f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}"
               f"/send/m.room.message/{txn}")
        async with s.put(url, json=body) as r:
            if r.status != 200:
                raise RuntimeError(
                    f"send_msg_raw {r.status}: {(await r.text())[:200]}")
            return (await r.json()).get("event_id")


async def _lobby_invite_to_space(mxid):
    """Invite mxid to the space using the lobby/onboarding bot. Returns
    (status, body[:300]). Caller distinguishes 200 (invited) from 403
    (already a member — treat as success)."""
    async with aiohttp.ClientSession(
        headers={**LOBBY_AUTH, "Content-Type": "application/json"}
    ) as s:
        url = f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(SPACE_ID)}/invite"
        async with s.post(url, json={"user_id": mxid,
                                     "reason": "vetted via lobby airlock"}) as r:
            return r.status, (await r.text())[:300]


async def _invite_to_children(mxid):
    """Invite mxid to each SPACE_CHILD_IDS room using the main bot (TOKEN),
    which has admin PL across the children. Lobby users are typically
    remote (matrix.org), so we can't join on their behalf the way the
    signup flow does — we invite, and they accept.

    Returns list of child IDs successfully invited (status 200) or already
    a member (status 403). Per-child errors are logged but don't fail the
    overall promotion."""
    invited = []
    for child in SPACE_CHILD_IDS:
        st, body = await _admin_invite(mxid, child, reason="vetted via lobby airlock")
        if st in (200, 403):
            invited.append(child)
        else:
            print(f"[lobby] child {child} invite of {mxid} -> {st}: {body[:200]}",
                  flush=True)
    return invited


def _mint_welcome_signup_code(mxid):
    """Mint a fresh signup code labeled with mxid, persist to SIGNUP_PATH,
    and return (code, url). Returns (None, None) if disabled (uses=0).

    Same write surface as cmd_mint, but minted by the bot itself on
    successful airlock promotion so the member can self-onboard their
    agents without pinging an admin.
    """
    if LOBBY_WELCOME_CODE_USES <= 0:
        return None, None
    codes = _load(SIGNUP_PATH)
    code = _new_code()
    while code in codes:
        code = _new_code() + secrets.token_hex(2)
    codes[code] = {
        "uses_remaining": LOBBY_WELCOME_CODE_USES,
        "label": f"welcome:{mxid}",
        "inviter": mxid,
    }
    _save(SIGNUP_PATH, codes)
    return code, f"{HS_PUBLIC}/signup?code={code}"


async def _lobby_leave_room(room_id, reason="lobby done"):
    """Leave a lobby room as the lobby/onboarding bot. Best-effort —
    swallow errors so a failed leave doesn't strand the lobby state."""
    async with aiohttp.ClientSession(
        headers={**LOBBY_AUTH, "Content-Type": "application/json"}
    ) as s:
        url = f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/leave"
        try:
            async with s.post(url, json={"reason": reason}) as r:
                if r.status != 200:
                    print(f"[lobby leave warn] {room_id}: "
                          f"{r.status} {(await r.text())[:200]}", flush=True)
        except Exception as e:
            print(f"[lobby leave failed] {room_id}: {e}", flush=True)


async def _create_vetting_room(client, mxid):
    body = {
        "preset": "private_chat",   # creator PL 100, invitee PL 0
        "invite": [mxid],
        "is_direct": False,
        "name":  f"shape-rotator vetting · {mxid}",
        "topic": "captcha airlock — answer the challenge to be invited to the space.",
    }
    try:
        resp = await client.api.request("POST", "/_matrix/client/v3/createRoom", content=body)
        return resp["room_id"], None
    except Exception as e:
        return None, str(e)[:300]


def _vet(displayname, message, keyword):
    if not displayname:
        return False, "set a displayname in element first, then re-paste the haiku"
    text = (message or "").strip()
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) != 3:
        return False, "haiku is three lines"
    if not (30 <= len(text) <= 400):
        return False, "haiku should be roughly 30–400 chars"
    if keyword.lower() not in text.lower():
        return False, f"include the word '{keyword}' somewhere"
    return True, "ok"


async def _promote(client, mxid):
    try:
        await client.api.request("POST",
            f"/_matrix/client/v3/rooms/{SPACE_ID}/invite",
            content={"user_id": mxid, "reason": "vetted via airlock"})
        return 200, ""
    except Exception as e:
        return getattr(e, "http_status", 500), str(e)[:300]


async def _kick(client, room_id, user_id, reason="auto"):
    try:
        await client.api.request("POST",
            f"/_matrix/client/v3/rooms/{room_id}/kick",
            content={"user_id": user_id, "reason": reason})
    except Exception as e:
        print(f"[kick failed] {user_id} from {room_id}: {e}", flush=True)


async def _leave(client, room_id, reason="auto"):
    try:
        await client.api.request("POST",
            f"/_matrix/client/v3/rooms/{room_id}/leave",
            content={"reason": reason})
    except Exception as e:
        print(f"[leave failed] {room_id}: {e}", flush=True)


async def handle_knock(client, room_id, user_id, reason):
    code = (reason or "").strip()
    codes = _load(CODES_PATH)
    entry = codes.get(code)
    if not entry or entry.get("uses_remaining", 0) <= 0:
        audit({"type": "knock_rejected", "user": user_id, "room": room_id, "reason": reason})
        print(f"[knock rejected] {user_id}", flush=True)
        return

    title, keyword = await _fetch_wiki_challenge()
    vroom, err = await _create_vetting_room(client, user_id)
    if not vroom:
        audit({"type": "vetting_room_failed", "user": user_id,
               "code": code, "err": (err or "")[:200]})
        print(f"[vetting room failed] {user_id}: {err[:200]}", flush=True)
        return

    entry["uses_remaining"] -= 1
    codes[code] = entry
    _save(CODES_PATH, codes)

    state = _load(VETTING_PATH)
    state[vroom] = {
        "mxid": user_id, "code": code, "created": time.time(),
        "title": title, "keyword": keyword,
        "tries_left": VETTING_MAX_TRIES, "promoted": False, "closed": False,
    }
    _save(VETTING_PATH, state)

    await _send_msg(client, vroom,
        f"hi {user_id} — responsiveness check — confirms someone (human or agent) is on the line.\n\n"
        f"write a 3-line haiku about: {title}\n"
        f"include the word \"{keyword}\" somewhere.\n"
        f"reply in this room. {VETTING_MAX_TRIES} tries.")
    audit({"type": "vetting_room_created", "user": user_id, "code": code,
           "room": vroom, "title": title, "keyword": keyword})
    print(f"[vetting] {user_id} -> {vroom} ({title!r} / {keyword})", flush=True)


def iter_knock_events(rooms_data):
    for room_id, rd in rooms_data.get("join", {}).items():
        if room_id.split(":", 1)[0] != SPACE_ID.split(":", 1)[0]:
            continue
        for section in ("timeline", "state"):
            for ev in rd.get(section, {}).get("events", []):
                if ev.get("type") != "m.room.member":
                    continue
                c = ev.get("content") or {}
                if c.get("membership") != "knock":
                    continue
                yield room_id, ev["state_key"], c.get("reason", "")


def iter_vetting_rooms(rooms_data, vetting_state):
    """For each open vetting room we own, yield
    (room_id, meta, join_event_for_user_or_None, list_of_user_messages)."""
    for room_id, rd in rooms_data.get("join", {}).items():
        meta = vetting_state.get(room_id)
        if not meta or meta.get("promoted") or meta.get("closed"):
            continue
        join_ev = None
        for section in ("state", "timeline"):
            for ev in rd.get(section, {}).get("events", []):
                if (ev.get("type") == "m.room.member"
                        and ev.get("state_key") == meta["mxid"]
                        and (ev.get("content") or {}).get("membership") == "join"):
                    join_ev = ev
        msgs = [ev for ev in rd.get("timeline", {}).get("events", [])
                if ev.get("type") == "m.room.message"
                and ev.get("sender") == meta["mxid"]]
        yield room_id, meta, join_ev, msgs


async def process_vetting_room(client, room_id, meta, join_ev, msgs):
    """Process new messages in one vetting room. Returns updated meta or None."""
    # Persist displayname the first time we see the user's join event — Matrix
    # /sync returns the join event in one batch and the user's later messages
    # in subsequent batches, so we can't require both in the same cycle.
    if join_ev:
        meta["displayname"] = (join_ev.get("content") or {}).get("displayname", "")
    if not msgs:
        return meta if join_ev else None
    displayname = meta.get("displayname", "")
    keyword = meta["keyword"]
    for msg in msgs:
        text = (msg.get("content") or {}).get("body", "")
        ok, why = _vet(displayname, text, keyword)
        if ok:
            st, body = await _promote(client, meta["mxid"])
            if st == 200:
                meta["promoted"] = True
                meta["promoted_at"] = time.time()
                meta["displayname"] = displayname
                invited_children = await _invite_to_children(meta["mxid"])
                meta["invited_children"] = invited_children
                signup_code, signup_url = _mint_welcome_signup_code(meta["mxid"])
                ack = "nice — invited you to shape rotator. you can leave this room."
                if signup_url:
                    ack += (f"\n\nhere's a {LOBBY_WELCOME_CODE_USES}-use signup "
                            f"code for adding accounts/agents to this server: "
                            f"{signup_url}")
                await _send_msg(client, room_id, ack)
                # Relay the captcha + haiku to FEED_ROOM so the rest of
                # the community sees who joined and gets to enjoy their
                # haiku. send_message_event auto-encrypts if FEED_ROOM is E2EE.
                if FEED_ROOM:
                    haiku_lines = [f"> {l}" for l in (text or "").strip().splitlines() if l.strip()]
                    relay = "\n".join([
                        f"🌸 {displayname or meta['mxid']} ({meta['mxid']}) joined Shape Rotator",
                        f"captcha: write a haiku about \"{meta['title']}\" including the word \"{meta['keyword']}\"",
                        "",
                        *haiku_lines,
                    ])
                    await _send_msg(client, FEED_ROOM, relay)
                audit({"type": "promoted", "user": meta["mxid"],
                       "displayname": displayname, "room": room_id,
                       "haiku": text, "title": meta.get("title"),
                       "keyword": meta.get("keyword"),
                       "welcome_signup_code": signup_code})
                print(f"[promoted] {meta['mxid']} ({displayname})", flush=True)
            else:
                audit({"type": "promote_failed", "user": meta["mxid"],
                       "status": st, "body": body[:200]})
                print(f"[promote failed] {meta['mxid']} status={st}", flush=True)
            return meta
        meta["tries_left"] -= 1
        if meta["tries_left"] <= 0:
            await _send_msg(client, room_id,
                "out of tries. closing this room — get a fresh code and try again.")
            await _leave(client, room_id, reason="vetting failed")
            meta["closed"] = True
            meta["closed_reason"] = "tries_exhausted"
            audit({"type": "vetting_failed", "user": meta["mxid"], "room": room_id})
            return meta
        await _send_msg(client, room_id,
            f"not yet — {why}. {meta['tries_left']} tries left.")
    return meta


async def cleanup_stale_vetting(client, vetting_state):
    """Leave vetting rooms older than VETTING_TIMEOUT. Returns True if state changed."""
    now = time.time()
    dirty = False
    for vroom, meta in list(vetting_state.items()):
        if meta.get("promoted") or meta.get("closed"):
            continue
        if now - meta.get("created", 0) > VETTING_TIMEOUT:
            await _leave(client, vroom, reason="vetting timeout")
            meta["closed"] = True
            meta["closed_reason"] = "timeout"
            audit({"type": "vetting_timeout", "user": meta["mxid"], "room": vroom})
            dirty = True
    return dirty


# --- Lobby flow (POST /join/api → fresh public room → haiku → space) ---
#
# Replaces the knock dance for users who get the /join?code=… link. Each
# code-use mints one fresh public room. The bot waits for the user to join
# via the matrix.to URL, posts a wikipedia-fact haiku challenge, and on a
# valid haiku invites them to the space. On promotion the bot leaves the
# room — no admin remains, the room dies naturally.

def _rand_alias_suffix():
    """Lowercase alphanumeric suffix safe for a room alias localpart."""
    raw = secrets.token_urlsafe(8).lower()
    s = "".join(c for c in raw if c.isalnum())
    return (s or secrets.token_hex(5))[:10]


async def _create_lobby_room_raw(code):
    """Create a public room with a random alias using the admin token.

    Returns (room_id, alias_local). Raises on failure.

    Pinned to room_version=11 because continuwuity's default (room_v12)
    produced rooms the local homeserver did not authoritatively own,
    causing a federated user joining the room to leave the bot's local
    membership state desynced (404 on m.room.member queries, 403
    "Event is not authorized" on subsequent sends, M_NOT_FOUND on
    /join). Pinning to v11 keeps the room firmly local-authoritative
    and the federation path well-trodden.
    """
    alias_local = f"{LOBBY_ALIAS_PREFIX}{_rand_alias_suffix()}"
    body = {
        "preset": "public_chat",       # join_rule=public, history=shared
        "visibility": "private",       # don't list in the public room directory
        "room_alias_name": alias_local,
        "name":  "shape-rotator lobby",
        "topic": "haiku airlock — answer the challenge to be invited to the space.",
        "room_version": "11",
        # world_readable history bypasses continuwuity's per-event
        # visibility check, which fails with "shortstatehash not found"
        # after a remote fast_join (the optimisation continuwuity uses
        # when a federated user joins a public room without full state
        # resolution). With shared history (the public_chat default),
        # the bot's messages get stuck — matrix.org asks "can I see
        # this?" and continuwuity can't answer, so the user's Element
        # never renders the challenge. world_readable means "anyone
        # can see history" and the visibility check is a no-op.
        "initial_state": [
            {"type": "m.room.history_visibility",
             "state_key": "",
             "content": {"history_visibility": "world_readable"}},
        ],
    }
    async with aiohttp.ClientSession(
        headers={**LOBBY_AUTH, "Content-Type": "application/json"}
    ) as s:
        url = f"{HS}/_matrix/client/v3/createRoom"
        async with s.post(url, json=body) as r:
            if r.status != 200:
                raise RuntimeError(f"createRoom {r.status}: {(await r.text())[:300]}")
            j = await r.json()
            room_id = j["room_id"]
        # Belt-and-suspenders: explicitly /join the room. createRoom
        # auto-joins the creator, but if anything goes sideways with
        # room state federation, this re-asserts the bot's local
        # member event so subsequent sends are authorized.
        join_url = f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/join"
        async with s.post(join_url, json={}) as r:
            if r.status != 200:
                print(f"[lobby] post-create join warn ({r.status}): "
                      f"{(await r.text())[:200]}", flush=True)
        return room_id, alias_local


async def join_handler(request):
    """POST /join/api {code} → {url, alias, room_id} or {error}."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "bad_json"}, status=400)
    code = (data.get("code") or "").strip()
    if not code:
        return web.json_response({"error": "missing_code"}, status=400)

    codes = _load(CODES_PATH)
    entry = codes.get(code)
    if not entry or entry.get("uses_remaining", 0) <= 0:
        audit({"type": "lobby_rejected", "code": code, "why": "invalid_code"})
        return web.json_response({"error": "invalid_code"}, status=403)

    title, keyword = await _fetch_wiki_challenge()
    try:
        room_id, alias_local = await _create_lobby_room_raw(code)
    except Exception as e:
        audit({"type": "lobby_room_failed", "code": code, "err": str(e)[:300]})
        print(f"[lobby] createRoom failed: {e}", flush=True)
        return web.json_response({"error": "create_failed",
                                  "detail": str(e)[:200]}, status=500)

    entry["uses_remaining"] -= 1
    codes[code] = entry
    _save(CODES_PATH, codes)

    state = _load(LOBBY_PATH)
    state[room_id] = {
        "alias": alias_local, "code": code, "created": time.time(),
        "title": title, "keyword": keyword,
        "challenged": [],   # mxids we've already posted the challenge to
        "tries":     {},    # mxid -> tries_left
        "promoted":  False, # set on first successful promotion
        "closed":    False, # bot has left, no further processing
    }
    _save(LOBBY_PATH, state)

    full_alias = f"#{alias_local}:{SERVER_NAME}"
    matrix_to = f"https://matrix.to/#/{urllib.parse.quote(full_alias)}"
    audit({"type": "lobby_created", "room": room_id, "alias": full_alias,
           "code": code, "title": title, "keyword": keyword})
    print(f"[lobby] {full_alias} ({code}) -> {room_id} ({title!r}/{keyword})",
          flush=True)
    return web.json_response({
        "url":      matrix_to,
        "alias":    full_alias,
        "room_id":  room_id,
        "title":    title,
        "keyword":  keyword,
    })


def iter_lobby_rooms(rooms_data, lobby_state, self_mxid):
    """For each open lobby room we own, yield
    (room_id, meta, list_of_user_join_events, list_of_user_msg_events).

    self_mxid is the mxid of whichever bot is doing the /sync (the lobby bot
    in lobby_sync_loop, the main bot if called from sync_loop). Used to
    filter out the bot's own join + message events.

    We gather joins for users not yet challenged, plus messages from anyone
    who has already been challenged (so we can vet their haikus).
    """
    for room_id, rd in rooms_data.get("join", {}).items():
        meta = lobby_state.get(room_id)
        if not meta or meta.get("promoted") or meta.get("closed"):
            continue
        new_joins = []
        for section in ("state", "timeline"):
            for ev in rd.get(section, {}).get("events", []):
                if ev.get("type") != "m.room.member":
                    continue
                content = ev.get("content") or {}
                if content.get("membership") != "join":
                    continue
                mxid = ev.get("state_key", "")
                if not mxid or mxid == self_mxid:
                    continue
                if mxid in meta.get("challenged", []):
                    continue
                new_joins.append(ev)
        msgs = [ev for ev in rd.get("timeline", {}).get("events", [])
                if ev.get("type") == "m.room.message"
                and ev.get("sender") != self_mxid
                and ev.get("sender") in meta.get("challenged", [])]
        if new_joins or msgs:
            yield room_id, meta, new_joins, msgs


async def process_lobby_room(room_id, meta, new_joins, msgs, lobby_mxid):
    """Handle joins (post challenge) and messages (vet haiku) for one lobby.

    Runs as the dedicated onboarding bot (LOBBY_TOKEN). lobby_mxid is the
    bot's own mxid (filtering out its own join/messages). All Matrix calls
    are raw HTTP via LOBBY_AUTH so they don't touch the main bot's mautrix
    crypto state.
    """
    keyword = meta["keyword"]
    title   = meta["title"]

    # Post the haiku challenge to anyone who joined since last cycle.
    # The pre-send delay gives federation a few seconds to propagate the
    # join + initial state to the user's homeserver before the message is
    # sent (see LOBBY_CHALLENGE_DELAY_SEC docstring near env var).
    for ev in new_joins:
        mxid = ev["state_key"]
        if mxid == lobby_mxid:
            continue
        displayname = (ev.get("content") or {}).get("displayname", "")
        meta.setdefault("challenged", []).append(mxid)
        meta.setdefault("tries", {})[mxid] = LOBBY_MAX_TRIES
        meta.setdefault("displaynames", {})[mxid] = displayname
        if LOBBY_CHALLENGE_DELAY > 0:
            await asyncio.sleep(LOBBY_CHALLENGE_DELAY)
        await _send_msg_raw(room_id,
            f"hi {mxid} — responsiveness check — confirms someone (human or agent) is on the line.\n\n"
            f"write a 3-line haiku about: {title}\n"
            f"include the word \"{keyword}\" somewhere.\n"
            f"reply in this room. {LOBBY_MAX_TRIES} tries.")
        meta.setdefault("challenge_sent_ts", {})[mxid] = time.time()
        meta.setdefault("challenge_resends", {})[mxid] = 0
        audit({"type": "lobby_challenge_sent", "user": mxid, "room": room_id,
               "title": title, "keyword": keyword})
        print(f"[lobby] challenged {mxid} in {room_id} "
              f"({title!r} / {keyword})", flush=True)

    # Vet any new messages from already-challenged users.
    for msg in msgs:
        mxid = msg["sender"]
        if mxid == lobby_mxid:
            continue
        text = (msg.get("content") or {}).get("body", "")
        displayname = meta.get("displaynames", {}).get(mxid, "")
        ok, why = _vet(displayname, text, keyword)
        if ok:
            st, body = await _lobby_invite_to_space(mxid)
            # /invite returning 403 means either "already in the room" or
            # the bot lacks PL. The lobby bot has PL by construction (PL>=50
            # on the space); the only realistic 403 in this flow is
            # "already member." Treat it as success so re-running the lobby
            # works cleanly as a debug self-test.
            already_member = (st == 403)
            if already_member:
                print(f"[lobby] promote 403 — assuming already-member. "
                      f"body={body[:200]}", flush=True)
            if st == 200 or already_member:
                meta["promoted"] = True
                meta["promoted_at"] = time.time()
                meta["promoted_user"] = mxid
                invited_children = await _invite_to_children(mxid)
                signup_code, signup_url = _mint_welcome_signup_code(mxid)
                ack = ("you're already in shape rotator — see you in the space."
                       if already_member else
                       "nice — invited you to shape rotator. see you in the space.")
                if signup_url:
                    ack += (f"\n\nhere's a {LOBBY_WELCOME_CODE_USES}-use signup "
                            f"code for adding accounts/agents to this server: "
                            f"{signup_url}")
                await _send_msg_raw(room_id, ack)
                # FEED_ROOM relay (haiku celebration to #matrix-devops) is
                # the main bot's job, not the lobby bot's — it lives in a
                # different room the lobby bot may not be in. Skipped here;
                # if you want this back, handle it via the main sync_loop.
                audit({"type": "lobby_promoted", "user": mxid, "room": room_id,
                       "haiku": text, "title": title, "keyword": keyword,
                       "already_member": already_member,
                       "invited_children": invited_children,
                       "welcome_signup_code": signup_code})
                print(f"[lobby promoted] {mxid} ({displayname})"
                      f"{' (already in space)' if already_member else ''}"
                      f" children={len(invited_children)}/{len(SPACE_CHILD_IDS)}",
                      flush=True)
                await _lobby_leave_room(room_id, reason="lobby done")
                meta["closed"] = True
                meta["closed_reason"] = ("already_member" if already_member
                                         else "promoted")
            else:
                audit({"type": "lobby_promote_failed", "user": mxid,
                       "status": st, "body": body[:200]})
                print(f"[lobby promote failed] {mxid} status={st}", flush=True)
            return meta
        meta["tries"][mxid] = meta["tries"].get(mxid, LOBBY_MAX_TRIES) - 1
        if meta["tries"][mxid] <= 0:
            await _send_msg_raw(room_id,
                f"{mxid}: out of tries. get a fresh code and try again.")
            audit({"type": "lobby_failed", "user": mxid, "room": room_id})
            try:
                meta["challenged"].remove(mxid)
            except ValueError:
                pass
        else:
            await _send_msg_raw(room_id,
                f"{mxid}: not yet — {why}. {meta['tries'][mxid]} tries left.")
    return meta


async def process_lobby_resends(lobby_state):
    """Re-post the haiku challenge for users who joined but never replied.

    Walks all open lobby rooms (the per-/sync iter_lobby_rooms only yields
    rooms with new events, so we sweep separately). For each challenged user
    who has not attempted a haiku and whose first challenge was sent more
    than LOBBY_RESEND_AFTER seconds ago, post the challenge again. Capped
    at LOBBY_MAX_RESENDS per user.

    Motivation: continuwuity → matrix.org federation occasionally drops the
    initial challenge message when the user fast-joined seconds earlier
    (see LOBBY_CHALLENGE_DELAY_SEC docstring). The resend is the only
    proxy we have for "did the message land," since the sender homeserver
    doesn't surface federation delivery acks.
    """
    now = time.time()
    dirty = False
    for room_id, meta in list(lobby_state.items()):
        if meta.get("promoted") or meta.get("closed"):
            continue
        title = meta.get("title")
        keyword = meta.get("keyword")
        if not title or not keyword:
            continue
        sent_ts = meta.get("challenge_sent_ts", {})
        resends = meta.setdefault("challenge_resends", {})
        for mxid in list(meta.get("challenged", [])):
            if resends.get(mxid, 0) >= LOBBY_MAX_RESENDS:
                continue
            if mxid not in sent_ts:
                # Pre-existing room from before this patch — skip rather
                # than spam the user with a stale challenge.
                continue
            if now - sent_ts[mxid] < LOBBY_RESEND_AFTER:
                continue
            # Has the user attempted a haiku yet? tries[mxid] starts at
            # LOBBY_MAX_TRIES and only decrements on a (failed) reply.
            tries_left = meta.get("tries", {}).get(mxid, LOBBY_MAX_TRIES)
            if tries_left != LOBBY_MAX_TRIES:
                continue
            await _send_msg_raw(room_id,
                f"hi {mxid} — re-sending in case the first message didn't reach you.\n\n"
                f"write a 3-line haiku about: {title}\n"
                f"include the word \"{keyword}\" somewhere.\n"
                f"reply in this room. {LOBBY_MAX_TRIES} tries.")
            sent_ts[mxid] = now
            meta["challenge_sent_ts"] = sent_ts
            resends[mxid] = resends.get(mxid, 0) + 1
            audit({"type": "lobby_challenge_resent", "user": mxid,
                   "room": room_id, "attempt": resends[mxid]})
            print(f"[lobby] resent challenge to {mxid} in {room_id} "
                  f"(attempt {resends[mxid]})", flush=True)
            dirty = True
    return dirty


async def cleanup_stale_lobby(lobby_state):
    """Leave lobby rooms older than LOBBY_TIMEOUT with no promotion."""
    now = time.time()
    dirty = False
    for room_id, meta in list(lobby_state.items()):
        if meta.get("promoted") or meta.get("closed"):
            continue
        if now - meta.get("created", 0) > LOBBY_TIMEOUT:
            await _lobby_leave_room(room_id, reason="lobby timeout")
            meta["closed"] = True
            meta["closed_reason"] = "timeout"
            audit({"type": "lobby_timeout", "room": room_id,
                   "code": meta.get("code")})
            dirty = True
    return dirty


# --- Lobby sync loop (runs as the dedicated onboarding bot) ---

LOBBY_SYNC_STATE = Path(os.environ.get("LOBBY_SYNC_STATE",
                                       "/data/lobby_sync_since.txt"))


async def _lobby_whoami():
    """Return (mxid, device_id) for LOBBY_TOKEN, or (None, None) on failure."""
    async with aiohttp.ClientSession(headers=LOBBY_AUTH) as s:
        async with s.get(f"{HS}/_matrix/client/v3/account/whoami") as r:
            if r.status != 200:
                return None, None
            j = await r.json()
            return j.get("user_id"), j.get("device_id")


async def _lobby_accept_pending_invites(rooms_data):
    """Auto-accept any pending invites for the lobby bot. Lobby rooms are
    created by the bot itself so this is mostly a safety net for ops/admin
    flows that invite the lobby bot somewhere."""
    for room_id in list(rooms_data.get("invite", {}).keys()):
        async with aiohttp.ClientSession(
            headers={**LOBBY_AUTH, "Content-Type": "application/json"}
        ) as s:
            url = f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/join"
            try:
                async with s.post(url, json={}) as r:
                    if r.status == 200:
                        print(f"[lobby] auto-joined {room_id}", flush=True)
                    else:
                        print(f"[lobby] auto-join warn {room_id}: "
                              f"{r.status} {(await r.text())[:200]}", flush=True)
            except Exception as e:
                print(f"[lobby] auto-join failed {room_id}: {e}", flush=True)


async def lobby_sync_loop():
    """Long-poll /sync as the dedicated onboarding bot (LOBBY_TOKEN) and
    drive lobby flow processing. Runs in parallel with the main sync_loop.

    Cleartext-only — no OlmMachine, no crypto store. Lobby rooms are
    public + cleartext by design, so raw HTTP is sufficient.
    """
    lobby_mxid, lobby_device = await _lobby_whoami()
    if not lobby_mxid:
        print("[lobby sync] LOBBY_TOKEN whoami failed; lobby flow disabled",
              flush=True)
        return
    print(f"[lobby sync] running as {lobby_mxid}; device={lobby_device}",
          flush=True)

    since = (LOBBY_SYNC_STATE.read_text().strip()
             if LOBBY_SYNC_STATE.exists() else None)

    while True:
        url = f"{HS}/_matrix/client/v3/sync?timeout=30000"
        if since:
            url += f"&since={urllib.parse.quote(since)}"
        try:
            async with aiohttp.ClientSession(headers=LOBBY_AUTH) as s:
                async with s.get(url) as r:
                    if r.status != 200:
                        print(f"[lobby sync] {r.status} "
                              f"{(await r.text())[:200]}", flush=True)
                        await asyncio.sleep(5)
                        continue
                    data = await r.json()
        except Exception as e:
            print(f"[lobby sync error] {type(e).__name__}: {e}", flush=True)
            await asyncio.sleep(5)
            continue

        next_batch = data.get("next_batch")
        if next_batch:
            since = next_batch
            LOBBY_SYNC_STATE.write_text(since)

        rooms_data = data.get("rooms", {}) or {}
        await _lobby_accept_pending_invites(rooms_data)

        lobby_state = _load(LOBBY_PATH)
        l_dirty = False
        for lroom, meta, new_joins, msgs in iter_lobby_rooms(
                rooms_data, lobby_state, lobby_mxid):
            updated = await process_lobby_room(lroom, meta, new_joins,
                                               msgs, lobby_mxid)
            if updated is not None:
                lobby_state[lroom] = updated
                l_dirty = True
        if await process_lobby_resends(lobby_state):
            l_dirty = True
        if await cleanup_stale_lobby(lobby_state):
            l_dirty = True
        if l_dirty:
            _save(LOBBY_PATH, lobby_state)


# --- Admin commands (!mint / !codes / !revoke) ---
#
# Admin chat surface so adding/listing/revoking codes doesn't require ssh.
# The bot listens in ADMIN_COMMAND_ROOM (defaults to SPACE_ID — the space
# room itself is cleartext, so raw-HTTP /sync can read commands there).
# Tracked in issue #7; this is the v1 cut.

ADMIN_COMMAND_ROOM = os.environ.get("ADMIN_COMMAND_ROOM") or SPACE_ID
ADMIN_PL_THRESHOLD = int(os.environ.get("ADMIN_PL_THRESHOLD", "50"))
# Comma-separated mxids allowed regardless of PL. Useful when you want to
# delegate admin to someone whose PL hasn't been bumped yet.
ADMIN_ALLOWLIST = set(
    m.strip() for m in os.environ.get("ADMIN_ALLOWLIST", "").split(",") if m.strip()
)
# Room to relay successful-vetting messages to. Default = the admin room
# so #matrix-devops gets a "🌸 X joined" + their haiku each time. Set to
# any other cleartext room id to redirect.
FEED_ROOM = os.environ.get("FEED_ROOM") or ADMIN_COMMAND_ROOM

# Where to post "X started lobby" / "lobby failed" operator notifications.
# Defaults to FEED_ROOM (matrix-devops). Set to a private DM room id with
# the bot to keep these out of the public feed.
OPERATOR_NOTIFY_ROOM = os.environ.get("OPERATOR_NOTIFY_ROOM") or FEED_ROOM
# Sidecar bookkeeping for which lobby events have already been announced
# to OPERATOR_NOTIFY_ROOM. Kept separate from LOBBY_PATH so the
# lobby_sync_loop and sync_loop never write the same JSON file.
OPERATOR_ANNOUNCE_PATH = Path(os.environ.get(
    "OPERATOR_ANNOUNCE_PATH", "/data/operator_announce.json"))

# Filled at startup by /whoami so we can skip our own messages in the
# command room (we'd otherwise process replies we just sent).
OUR_MXID = ""


async def _whoami(client):
    resp = await client.api.request("GET", "/_matrix/client/v3/account/whoami")
    return resp["user_id"], resp.get("device_id", "")


async def _get_user_pl(client, room_id, mxid):
    try:
        pl = await client.api.request("GET",
            f"/_matrix/client/v3/rooms/{room_id}/state/m.room.power_levels")
    except Exception:
        return 0
    users = pl.get("users") or {}
    if mxid in users:
        return int(users[mxid])
    return int(pl.get("users_default", 0))


async def _is_admin(client, room_id, sender):
    if sender in ADMIN_ALLOWLIST:
        return True
    return (await _get_user_pl(client, room_id, sender)) >= ADMIN_PL_THRESHOLD


def _new_code():
    return secrets.token_urlsafe(6).rstrip("=").replace("_", "").replace("-", "")[:9] or secrets.token_hex(4)


async def cmd_mint(client, room_id, sender, args):
    """!mint [knock|signup] [-n N] [--uses U] [label]  — generate codes.

    -n N        number of distinct codes (default 1)
    --uses U    uses_remaining per code (default 1)

    Examples:
      !mint                          one knock code, single use
      !mint -n 10                    ten knock codes, single use each (one per person)
      !mint --uses 10                one knock code, usable 10 times (shareable link)
      !mint signup batch-A           one signup code, labelled
      !mint -n 5 signup cohort1      five signup codes labelled "cohort1"
      !mint --uses 20 open-house     one knock code, 20 uses, labelled "open-house"
    """
    def _parse_int(flag, raw):
        try:
            return int(raw), None
        except ValueError:
            return None, f"!mint: {flag} needs a number, got {raw!r}"

    parts = args.split() if args else []
    kind = "knock"
    n = 1
    uses = 1
    label_parts = []
    i = 0
    while i < len(parts):
        p = parts[i]
        if p in ("knock", "signup") and not label_parts:
            kind = p
        elif p in ("-n", "--n") and i + 1 < len(parts):
            n, err = _parse_int("-n", parts[i + 1])
            if err: return err
            i += 1
        elif p.startswith("-n=") or p.startswith("--n="):
            n, err = _parse_int("-n", p.split("=", 1)[1])
            if err: return err
        elif p == "--uses" and i + 1 < len(parts):
            uses, err = _parse_int("--uses", parts[i + 1])
            if err: return err
            i += 1
        elif p.startswith("--uses="):
            uses, err = _parse_int("--uses", p.split("=", 1)[1])
            if err: return err
        else:
            label_parts.append(p)
        i += 1
    if n < 1 or n > 50:
        return f"!mint: -n must be 1..50, got {n}"
    if uses < 1 or uses > 1000:
        return f"!mint: --uses must be 1..1000, got {uses}"
    label = " ".join(label_parts) or f"minted by {sender}"

    path = SIGNUP_PATH if kind == "signup" else CODES_PATH
    codes = _load(path)
    minted = []
    for _ in range(n):
        code = _new_code()
        while code in codes:
            code = _new_code() + secrets.token_hex(2)
        codes[code] = {"uses_remaining": uses, "label": label}
        minted.append(code)
    _save(path, codes)
    audit({"type": "admin_mint", "kind": kind, "n": n, "uses": uses,
           "minted_by": sender, "label": label, "codes": minted})

    base = f"{HS_PUBLIC}/{'signup' if kind == 'signup' else 'join'}?code="
    uses_tag = "" if uses == 1 else f", {uses} uses each" if n > 1 else f", {uses} uses"
    if n == 1:
        return f"minted {kind} code → {base}{minted[0]}\n(label: {label}{uses_tag})"
    lines = [f"minted {n} {kind} codes (label: {label}{uses_tag}):"]
    lines.extend(f"  {base}{c}" for c in minted)
    return "\n".join(lines)


async def cmd_codes(client, room_id, sender, args):
    """!codes — list current valid codes."""
    out = []
    for label_name, p in (("knock", CODES_PATH), ("signup", SIGNUP_PATH)):
        codes = _load(p)
        live = {c: m for c, m in codes.items() if m.get("uses_remaining", 0) > 0}
        if not live:
            continue
        out.append(f"{label_name}:")
        for c, m in sorted(live.items()):
            out.append(f"  {c} (uses={m.get('uses_remaining',0)}, label={m.get('label','')!r})")
    return "\n".join(out) if out else "no live codes."


async def cmd_revoke(client, room_id, sender, args):
    """!revoke <code> — zero out a code's uses_remaining."""
    code = args.strip()
    if not code:
        return "usage: !revoke <code>"
    for p in (CODES_PATH, SIGNUP_PATH):
        codes = _load(p)
        if code in codes:
            codes[code]["uses_remaining"] = 0
            _save(p, codes)
            audit({"type": "admin_revoke", "code": code, "revoked_by": sender,
                   "in": p.name})
            return f"revoked {code} (in {p.name})"
    return f"unknown code: {code}"


COMMANDS = {
    "!mint": cmd_mint,
    "!codes": cmd_codes,
    "!revoke": cmd_revoke,
    "!help": None,  # filled below
}

async def cmd_help(client, room_id, sender, args):
    return ("commands: " +
            ", ".join(sorted(c for c in COMMANDS if c != "!help")) +
            ", !help")
COMMANDS["!help"] = cmd_help


async def process_admin_command(client, room_id, event_id, sender, body):
    if not OUR_MXID:
        # /whoami failed at startup; refuse to process anything to avoid
        # ever responding to our own replies (which would loop).
        return
    if sender == OUR_MXID:
        return
    parts = body.split(maxsplit=1)
    cmd = parts[0]
    args = parts[1] if len(parts) > 1 else ""
    handler = COMMANDS.get(cmd)
    if not handler:
        return
    is_admin = await _is_admin(client, room_id, sender)
    print(f"[admin] dispatch {cmd} from {sender} is_admin={is_admin}", flush=True)
    if not is_admin:
        await _send_msg(client, room_id,
            f"{sender}: refused — need PL >= {ADMIN_PL_THRESHOLD} or be on the allowlist")
        audit({"type": "admin_refused", "cmd": cmd, "sender": sender})
        return
    try:
        result = await handler(client, room_id, sender, args)
    except Exception as e:
        result = f"!{cmd[1:]} failed: {type(e).__name__}: {e}"
        print(f"[admin] {cmd} crashed: {e}", flush=True)
    print(f"[admin] sending reply: {result[:120]!r}", flush=True)
    await _send_msg(client, room_id, result)


async def announce_lobby_events(client):
    """Scan lobby_state and emit operator notifications for new joins
    (challenged users) and lobby failures. Posts to OPERATOR_NOTIFY_ROOM
    via the main bot's E2EE-aware client. Idempotent — uses a sidecar
    JSON file so the lobby loop never has to know about announce flags."""
    if not OPERATOR_NOTIFY_ROOM:
        return
    lobby_state = _load(LOBBY_PATH)
    if not lobby_state:
        return
    # First-run backfill suppression: if the announce file doesn't exist
    # yet, seed it with everything currently in lobby_state marked as
    # already-announced. Otherwise the first cycle after deploy would
    # spam ~20 retroactive "X started" messages for historical lobbies.
    first_run = not OPERATOR_ANNOUNCE_PATH.exists()
    seen = _load(OPERATOR_ANNOUNCE_PATH)
    if first_run:
        for room_id, meta in lobby_state.items():
            seen[room_id] = {
                "started": list(meta.get("challenged", [])),
                "failed":  bool(meta.get("closed")
                                and meta.get("closed_reason") not in (None, "promoted", "already_member")),
            }
        _save(OPERATOR_ANNOUNCE_PATH, seen)
        return
    dirty = False
    for room_id, meta in lobby_state.items():
        rec = seen.setdefault(room_id, {"started": [], "failed": False})
        for mxid in meta.get("challenged", []):
            if mxid in rec["started"]:
                continue
            displayname = (meta.get("displaynames") or {}).get(mxid, "")
            label = f"{displayname} ({mxid})" if displayname else mxid
            await _send_msg(client, OPERATOR_NOTIFY_ROOM,
                f"🚪 {label} started lobby flow (code={meta.get('code', '?')})")
            rec["started"].append(mxid)
            dirty = True
        if (meta.get("closed") and not rec["failed"]
                and meta.get("closed_reason") not in (None, "promoted", "already_member")):
            users = ", ".join(meta.get("challenged", []) or ["(no users joined)"])
            await _send_msg(client, OPERATOR_NOTIFY_ROOM,
                f"⚠️ lobby failed for {users} "
                f"(reason={meta.get('closed_reason')}, code={meta.get('code', '?')})")
            rec["failed"] = True
            dirty = True
    # Garbage-collect bookkeeping for closed lobbies — no further events
    # will fire for them, so the announce record is no longer needed.
    for room_id in list(seen.keys()):
        meta = lobby_state.get(room_id)
        if meta is None or meta.get("closed"):
            seen.pop(room_id, None)
            dirty = True
    if dirty:
        _save(OPERATOR_ANNOUNCE_PATH, seen)


# Helper for OlmMachine. Tracks which rooms we're joined to so the
# crypto state store can answer find_shared_rooms.
class _StateStore:
    def __init__(self, inner):
        self._inner, self._joined = inner, set()
    async def is_encrypted(self, rid):
        return (await self.get_encryption_info(rid)) is not None
    async def get_encryption_info(self, rid):
        if hasattr(self._inner, "get_encryption_info"):
            return await self._inner.get_encryption_info(rid)
        return None
    async def find_shared_rooms(self, uid):
        return list(self._joined)


async def sync_loop():
    """Mautrix-based sync loop with full E2EE support.

    Replaces the previous raw-HTTP /sync. Mautrix decrypts incoming
    encrypted events in place via OlmMachine, and our send_message_event
    calls auto-encrypt outbound to any room with m.room.encryption set.

    Cleartext flows (knock events on the space, vetting rooms) keep
    working unchanged — iter_knock_events and iter_vetting_rooms read
    raw event dicts which mautrix returns in the same shape.

    Admin commands route through an event-handler so the bot processes
    DECRYPTED versions even when ADMIN_COMMAND_ROOM is E2EE.
    """
    global OUR_MXID

    api = _MAU_HTTPAPI(base_url=HS, token=TOKEN)
    state_store = _MAU_MemoryStateStore()
    sync_store_obj = _MAU_MemorySyncStore()

    # Resolve our identity via /whoami before the Client constructor
    # (mautrix needs mxid + device_id baked in).
    try:
        whoami = await api.request("GET", "/_matrix/client/v3/account/whoami")
        OUR_MXID = whoami["user_id"]
        device_id = whoami.get("device_id", "approver")
    except Exception as e:
        print(f"[startup] whoami failed: {e}", flush=True)
        # Fall through with empty mxid; admin commands will refuse.
        OUR_MXID = ""
        device_id = "approver-fallback"

    client = _MAU_Client(mxid=_MAU_UserID(OUR_MXID or "@unknown:localhost"),
                         device_id=device_id, api=api,
                         state_store=state_store, sync_store=sync_store_obj)

    CRYPTO_DB.parent.mkdir(parents=True, exist_ok=True)
    db = _MAU_Database.create(f"sqlite:///{CRYPTO_DB}",
                               upgrade_table=_MAU_PgCryptoStore.upgrade_table)
    await db.start()
    cs = _MAU_PgCryptoStore(account_id=OUR_MXID or "approver",
                             pickle_key=f"{OUR_MXID}:{device_id}", db=db)
    await cs.open()
    ss = _StateStore(state_store)
    olm = _MAU_OlmMachine(client, cs, ss)
    olm.share_keys_min_trust = _MAU_TrustState.UNVERIFIED
    olm.send_keys_min_trust = _MAU_TrustState.UNVERIFIED
    await olm.load()
    client.crypto = olm
    client.crypto_store = cs

    # Queue admin-command events as they arrive (mautrix decrypts before
    # invoking handlers). Drained once per sync cycle below.
    admin_queue = asyncio.Queue()

    async def on_room_message(evt):
        try:
            ev_room = str(evt.room_id)
            ev_sender = str(evt.sender)
            if ev_room != ADMIN_COMMAND_ROOM:
                return
            if ev_sender == OUR_MXID:
                return
            body = (getattr(evt.content, "body", "") or "").strip()
            if not body.startswith("!"):
                return
            await admin_queue.put((str(evt.event_id), ev_sender, body))
        except Exception as e:
            print(f"[admin handler] {type(e).__name__}: {e}", flush=True)

    client.add_event_handler(_MAU_EventType.ROOM_MESSAGE, on_room_message)

    try:
        await client.crypto.share_keys()
    except Exception as e:
        print(f"[startup] share_keys failed (continuing): {e}", flush=True)

    print(f"[startup] running as {OUR_MXID}; device={device_id}; "
          f"admin room={ADMIN_COMMAND_ROOM}; "
          f"allowlist={sorted(ADMIN_ALLOWLIST) or '(empty)'}", flush=True)

    since = SYNC_STATE.read_text().strip() if SYNC_STATE.exists() else None

    while True:
        try:
            data = await client.sync(since=since, timeout=30000)
        except Exception as e:
            print(f"[sync error] {type(e).__name__}: {e}", flush=True)
            await asyncio.sleep(5)
            continue
        if not isinstance(data, dict):
            continue
        next_batch = data.get("next_batch")
        if next_batch:
            since = next_batch
            SYNC_STATE.write_text(since)

        # Update joined-rooms tracking so OlmMachine can answer find_shared_rooms.
        ss._joined.clear()
        ss._joined.update(data.get("rooms", {}).get("join", {}).keys())

        # Decrypt + dispatch event handlers (this populates admin_queue).
        try:
            tasks = client.handle_sync(data)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            print(f"[handle_sync error] {type(e).__name__}: {e}", flush=True)

        # Cleartext flows — knock events on the space + vetting rooms.
        for room_id, user_id, reason in iter_knock_events(data.get("rooms", {})):
            await handle_knock(client, room_id, user_id, reason)

        vetting_state = _load(VETTING_PATH)
        v_dirty = False
        for vroom, meta, join_ev, msgs in iter_vetting_rooms(
                data.get("rooms", {}), vetting_state):
            updated = await process_vetting_room(client, vroom, meta, join_ev, msgs)
            if updated is not None:
                vetting_state[vroom] = updated
                v_dirty = True
        if await cleanup_stale_vetting(client, vetting_state):
            v_dirty = True
        if v_dirty:
            _save(VETTING_PATH, vetting_state)

        # Lobby flow runs in its own /sync loop (lobby_sync_loop) under the
        # dedicated onboarding-bot identity (LOBBY_TOKEN), so it doesn't appear
        # here. See lobby_sync_loop() below.

        # Operator notifications about lobby starts + failures. The lobby
        # bot can't post to OPERATOR_NOTIFY_ROOM (which is typically E2EE
        # and only the main bot has keys for), so the main bot scans
        # lobby_state each cycle and emits the announcements itself.
        try:
            await announce_lobby_events(client)
        except Exception as e:
            print(f"[announce_lobby_events] {type(e).__name__}: {e}", flush=True)

        # Drain queued admin commands (event handler may have populated
        # them with decrypted message events).
        while not admin_queue.empty():
            try:
                ev_id, sender, body = admin_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            await process_admin_command(client, ADMIN_COMMAND_ROOM, ev_id, sender, body)


# --- Signup auth proxy ---

def valid_username(u: str) -> bool:
    return (u.isascii() and 1 <= len(u) <= 32
            and all(c.isalnum() or c in "-_.=" for c in u))

async def _admin_invite(mxid, room_id, reason="signup auto-invite"):
    """Invite `mxid` to `room_id` using the admin (MATRIX_TOKEN) account."""
    async with aiohttp.ClientSession(
        headers={**AUTH, "Content-Type": "application/json"}
    ) as s:
        url = f"{HS}/_matrix/client/v3/rooms/{room_id}/invite"
        async with s.post(url, json={"user_id": mxid, "reason": reason}) as r:
            return r.status, await r.text()

async def _as_user(access_token, method, path, body=None):
    """Make a request as the freshly-registered user."""
    headers = {"Authorization": f"Bearer {access_token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    url = f"{HS}{path}"
    async with aiohttp.ClientSession(headers=headers) as s:
        kwargs = {"json": body} if body is not None else {}
        async with s.request(method, url, **kwargs) as r:
            return r.status, await r.text()

async def signup_handler(request):
    if not REG_TOKEN:
        return web.json_response({"error": "signup_disabled"}, status=503)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "bad_json"}, status=400)

    code        = (data.get("code") or "").strip()
    username    = (data.get("username") or "").strip().lower()
    password    = data.get("password") or ""
    displayname = (data.get("display_name") or "").strip()
    intro_raw   = (data.get("intro") or "").strip()

    if not (code and username and password):
        return web.json_response({"error": "missing_fields"}, status=400)
    if not valid_username(username):
        return web.json_response({"error": "bad_username"}, status=400)
    if len(password) < 12:
        return web.json_response({"error": "password_too_short"}, status=400)

    codes = _load(SIGNUP_PATH)
    entry = codes.get(code)
    if not entry or entry.get("uses_remaining", 0) <= 0:
        audit({"type": "signup_rejected", "username": username, "why": "invalid_code"})
        return web.json_response({"error": "invalid_code"}, status=403)

    # --- Step 1+2: register ---
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{HS}/_matrix/client/v3/register", json={}) as r:
            if r.status == 401:
                session = (await r.json()).get("session")
            else:
                return web.json_response({"error": "register_init_unexpected",
                                          "status": r.status}, status=502)

        body = {
            "auth": {"type": "m.login.registration_token",
                     "token": REG_TOKEN, "session": session},
            "username": username,
            "password": password,
            "initial_device_display_name": "shape-rotator signup",
        }
        async with s.post(f"{HS}/_matrix/client/v3/register", json=body) as r:
            result = await r.json()
            if r.status != 200:
                audit({"type": "signup_failed", "username": username,
                       "status": r.status, "body": str(result)[:300]})
                err = str(result.get("errcode", "register_failed")).lower()
                return web.json_response({"error": err,
                                          "detail": result.get("error")}, status=400)

    entry["uses_remaining"] -= 1
    codes[code] = entry
    _save(SIGNUP_PATH, codes)

    mxid  = result["user_id"]
    token = result["access_token"]
    steps_done = {"register": True}

    # --- Step 3: admin invites the new user to the space ---
    st, _body = await _admin_invite(mxid, SPACE_ID)
    steps_done["space_invited"] = (st == 200)
    if st != 200:
        print(f"[signup] admin invite of {mxid} -> {st}: {_body[:200]}", flush=True)

    # --- Step 4: new user sets display name (if requested) ---
    if displayname:
        import urllib.parse as _up
        st, _body = await _as_user(
            token, "PUT",
            f"/_matrix/client/v3/profile/{_up.quote(mxid)}/displayname",
            {"displayname": displayname[:100]},
        )
        steps_done["displayname_set"] = (st == 200)

    # --- Step 5: new user accepts space invite ---
    st, _body = await _as_user(
        token, "POST", f"/_matrix/client/v3/rooms/{SPACE_ID}/join", {}
    )
    steps_done["space_joined"] = (st == 200)
    if st != 200:
        print(f"[signup] space join by {mxid} -> {st}: {_body[:200]}", flush=True)

    # --- Step 6: new user joins each child room (restricted rule permits) ---
    joined_children = []
    for child in SPACE_CHILD_IDS:
        st, _body = await _as_user(
            token, "POST", f"/_matrix/client/v3/rooms/{child}/join", {}
        )
        if st == 200:
            joined_children.append(child)
        else:
            print(f"[signup] child {child} join by {mxid} -> {st}: {_body[:200]}", flush=True)
    steps_done["children_joined"] = joined_children

    # --- Step 7: create an E2EE DM with the inviter from the new user ---
    # We create the DM encrypted from the start (m.room.encryption in
    # initial_state). We do NOT send a greeting here via raw HTTP —
    # the server would reject plaintext m.room.message in an encrypted
    # room. The intro is left to the bot's matrix-nio startup, which
    # can send encrypted messages properly. We just return dm_room and
    # intro_text so the bot knows where to post and what to say.
    inviter = (entry.get("inviter") or ONBOARDING_INVITER_MXID or "").strip()
    dm_room = None
    intro_text = intro_raw or (
        f"hi — I'm {displayname or mxid}, just signed up on "
        f"{HS_PUBLIC} via a code you issued. Let me know what you need."
    )
    if inviter:
        st, dm_body = await _as_user(
            token, "POST", "/_matrix/client/v3/createRoom",
            {
                "is_direct": True,
                "invite":    [inviter],
                "preset":    "trusted_private_chat",
                "name":      f"{displayname or username} ↔ {inviter}",
                "initial_state": [{
                    "type": "m.room.encryption",
                    "state_key": "",
                    "content": {"algorithm": "m.megolm.v1.aes-sha2"},
                }],
            },
        )
        if st == 200:
            dm_room = json.loads(dm_body).get("room_id")
            steps_done["inviter_dm"] = True
        else:
            print(f"[signup] createRoom (DM to {inviter}) -> {st}: {dm_body[:200]}", flush=True)
            steps_done["inviter_dm"] = False

    audit({"type": "signup_ok", "user": mxid, "code": code,
           "uses_left": entry["uses_remaining"], "steps": steps_done})
    print(f"[signup ok] {mxid} via {code} "
          f"(left={entry['uses_remaining']}, steps={steps_done})", flush=True)

    return web.json_response({
        "user_id":     mxid,
        "access_token": token,
        "device_id":   result["device_id"],
        "homeserver":  HS_PUBLIC,
        "space_id":    SPACE_ID,
        "steps":       steps_done,
        "dm_room":     dm_room,
        "intro_text":  intro_text,   # for the bot to post via nio on startup
    })

# --- Cross-signing bootstrap ---
#
# Generate MSK / SSK / USK for the user, sign SSK and USK with MSK, sign the
# caller's current device with SSK, and upload everything via UIA. After this,
# Element stops showing "encrypted by a device not verified by its owner".
#
# Matrix canonical JSON: keys sorted, no whitespace, no non-ASCII escaping.

def _b64(data: bytes) -> str:
    return base64.b64encode(data).rstrip(b"=").decode()

def _canon(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")

def _raw_pub(privkey: ed25519.Ed25519PrivateKey) -> bytes:
    return privkey.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

def _raw_priv(privkey: ed25519.Ed25519PrivateKey) -> bytes:
    return privkey.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )

def _sign_object(obj: dict, signer: ed25519.Ed25519PrivateKey, user_id: str, key_id: str) -> dict:
    """Sign `obj` per Matrix spec: canonical JSON of obj minus signatures/unsigned,
    attach under signatures[user_id][ed25519:key_id]."""
    to_sign = {k: v for k, v in obj.items() if k not in ("signatures", "unsigned")}
    sig = _b64(signer.sign(_canon(to_sign)))
    sigs = dict(obj.get("signatures", {}))
    user_sigs = dict(sigs.get(user_id, {}))
    user_sigs[f"ed25519:{key_id}"] = sig
    sigs[user_id] = user_sigs
    obj["signatures"] = sigs
    return obj

async def _crosssign(access_token: str, password: str = ""):
    """Bootstrap cross-signing for the user identified by access_token.

    Password is optional — only needed if the homeserver insists on UIA
    m.login.password for /keys/device_signing/upload. Continuwuity (our
    target) currently accepts the upload directly.
    """
    headers = {"Authorization": f"Bearer {access_token}",
               "Content-Type": "application/json"}

    async with aiohttp.ClientSession() as s:
        # Identify the user
        async with s.get(f"{HS}/_matrix/client/v3/account/whoami", headers=headers) as r:
            if r.status != 200:
                raise RuntimeError(f"whoami failed: {r.status}")
            me = await r.json()
        user_id  = me["user_id"]
        device_id = me["device_id"]

        # Generate three ed25519 keypairs
        msk = ed25519.Ed25519PrivateKey.generate()
        ssk = ed25519.Ed25519PrivateKey.generate()
        usk = ed25519.Ed25519PrivateKey.generate()
        msk_pub = _b64(_raw_pub(msk))
        ssk_pub = _b64(_raw_pub(ssk))
        usk_pub = _b64(_raw_pub(usk))

        # Build three signed key objects.
        # MSK self-signs (master signs itself). SSK and USK are signed by MSK.
        master = {
            "user_id": user_id, "usage": ["master"],
            "keys": {f"ed25519:{msk_pub}": msk_pub},
        }
        master = _sign_object(master, msk, user_id, msk_pub)

        self_signing = {
            "user_id": user_id, "usage": ["self_signing"],
            "keys": {f"ed25519:{ssk_pub}": ssk_pub},
        }
        self_signing = _sign_object(self_signing, msk, user_id, msk_pub)

        user_signing = {
            "user_id": user_id, "usage": ["user_signing"],
            "keys": {f"ed25519:{usk_pub}": usk_pub},
        }
        user_signing = _sign_object(user_signing, msk, user_id, msk_pub)

        # Upload the three signing keys. Matrix spec requires UIA here, but
        # continuwuity sometimes skips it — try direct first, fall back to UIA.
        upload_body = {
            "master_key":       master,
            "self_signing_key": self_signing,
            "user_signing_key": user_signing,
        }
        async with s.post(f"{HS}/_matrix/client/v3/keys/device_signing/upload",
                          json=upload_body, headers=headers) as r:
            if r.status == 401:
                if not password:
                    raise RuntimeError("homeserver requires UIA; pass `password` to /crosssign")
                uia = await r.json()
                session = uia.get("session")
                if not session:
                    raise RuntimeError(f"no UIA session: {uia}")
                upload_body["auth"] = {
                    "type": "m.login.password",
                    "identifier": {"type": "m.id.user", "user": user_id},
                    "password": password,
                    "session": session,
                }
                async with s.post(f"{HS}/_matrix/client/v3/keys/device_signing/upload",
                                  json=upload_body, headers=headers) as r2:
                    if r2.status != 200:
                        raise RuntimeError(f"device_signing/upload (UIA retry) failed "
                                           f"{r2.status}: {(await r2.text())[:300]}")
            elif r.status != 200:
                raise RuntimeError(f"device_signing/upload failed {r.status}: "
                                   f"{(await r.text())[:300]}")

        # Try to sign the current device with SSK. If device keys aren't uploaded
        # yet (bot hasn't synced), retry briefly — else succeed partial and let
        # the caller try again once the bot is live.
        device_obj = None
        for attempt in range(4):
            async with s.post(f"{HS}/_matrix/client/v3/keys/query",
                              json={"device_keys": {user_id: [device_id]}},
                              headers=headers) as r:
                if r.status == 200:
                    q = await r.json()
                    device_obj = (q.get("device_keys", {})
                                   .get(user_id, {})
                                   .get(device_id))
                    if device_obj:
                        break
            await asyncio.sleep(1.5)

        device_signed = False
        if device_obj:
            signed_device = _sign_object(device_obj, ssk, user_id, ssk_pub)
            async with s.post(f"{HS}/_matrix/client/v3/keys/signatures/upload",
                              json={user_id: {device_id: signed_device}},
                              headers=headers) as r:
                body = await r.json()
                if r.status == 200 and not body.get("failures"):
                    device_signed = True
                else:
                    print(f"[crosssign] signatures/upload: {r.status} {body}", flush=True)

    return {
        "device_signed": device_signed,
        "user_id": user_id,
        "device_id": device_id,
        "msk_public": msk_pub,
        "ssk_public": ssk_pub,
        "usk_public": usk_pub,
        "private_keys": {
            # Client persists these if it wants to sign future devices or
            # publish its own USK signatures for other users.
            "master":       _b64(_raw_priv(msk)),
            "self_signing": _b64(_raw_priv(ssk)),
            "user_signing": _b64(_raw_priv(usk)),
        },
    }

async def crosssign_handler(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "bad_json"}, status=400)
    access_token = (data.get("access_token") or "").strip()
    password     = data.get("password") or ""
    if not access_token:
        return web.json_response({"error": "missing_fields",
                                  "hint": "need access_token; password optional"}, status=400)
    try:
        result = await _crosssign(access_token, password)
    except Exception as e:
        audit({"type": "crosssign_failed", "error": str(e)})
        print(f"[crosssign] {e}", flush=True)
        return web.json_response({"error": "crosssign_failed",
                                  "detail": str(e)[:500]}, status=400)
    audit({"type": "crosssign_ok", "user": result["user_id"],
           "msk": result["msk_public"]})
    print(f"[crosssign ok] {result['user_id']} msk={result['msk_public'][:20]}...", flush=True)
    return web.json_response(result)


async def run_http():
    app = web.Application()
    app.router.add_post("/signup/api",           signup_handler)
    app.router.add_post("/signup/api/crosssign", crosssign_handler)
    app.router.add_post("/join/api",             join_handler)
    app.router.add_get("/health",                lambda r: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
    await site.start()
    print(f"signup HTTP server listening on :{HTTP_PORT}", flush=True)


# --- Main ---

async def main():
    global SERVER_NAME
    if not SERVER_NAME:
        async with aiohttp.ClientSession(headers=AUTH) as s:
            async with s.get(f"{HS}/_matrix/client/v3/account/whoami") as r:
                me = await r.json()
        SERVER_NAME = me["user_id"].split(":", 1)[1]
    print(f"approver starting. space={SPACE_ID} signup_enabled={bool(REG_TOKEN)} "
          f"server_name={SERVER_NAME!r}", flush=True)
    for p in (CODES_PATH, SIGNUP_PATH, LOG_PATH, VETTING_PATH, LOBBY_PATH):
        p.parent.mkdir(parents=True, exist_ok=True)
    if not CODES_PATH.exists():   _save(CODES_PATH,   {})
    if not SIGNUP_PATH.exists():  _save(SIGNUP_PATH,  {})
    if not VETTING_PATH.exists(): _save(VETTING_PATH, {})
    if not LOBBY_PATH.exists():   _save(LOBBY_PATH,   {})
    merge_seed(CODES_PATH,  "INITIAL_CODES")
    merge_seed(SIGNUP_PATH, "INITIAL_SIGNUP_CODES")

    await run_http()
    # Run main sync_loop (knocks, vetting, admin commands) and lobby_sync_loop
    # (dedicated onboarding-bot identity for the lobby flow) in parallel.
    # If LOBBY_TOKEN == TOKEN (no dedicated bot configured), both loops sync
    # the same user — works but wasteful; configure ONBOARDING_BOT_TOKEN.
    await asyncio.gather(sync_loop(), lobby_sync_loop())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
