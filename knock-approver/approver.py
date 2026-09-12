"""Auto-approve Matrix knocks on the Shape Rotator space, AND proxy signups.

Three responsibilities, all running in one process:

1. **Knock approver** (long-running /sync loop).
   Watches the space for membership=knock events. When the knock reason matches
   an entry in /data/codes.json, POSTs /invite to approve.

2. **Welcome-room door** (HTTP /join/api + sync-loop handler).
   POST /join/api {code} validates the code and maps it to ONE public
   `#welcome-<hash>:server` room (idempotent — a second call for a live
   code returns the same alias, and nothing is decremented yet). The user
   clicks through and joins with Element's plain Join button; the bot sees
   the join in /sync, invites the joiner to the space, and only then
   decrements the code's uses_remaining and posts a confirmation — a failed
   invite consumes nothing, so a rejoin retries. Rooms are tombstoned and
   forgotten on a timer (joined: 30m, unused: 48h).

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
  /data/codes.json          knock codes (also the welcome-room codes)
  /data/signup_codes.json   signup codes
  /data/welcome_rooms.json  live welcome rooms (code -> room mapping)
  /data/welcome_secret      server secret for welcome alias hashes
  /data/log.jsonl           audit log
  /data/sync_since.txt      /sync cursor
"""
import asyncio, base64, hashlib, json, os, secrets, sys, time, urllib.parse
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
    SessionID as _MAU_SessionID,
)
from mautrix.crypto import OlmMachine as _MAU_OlmMachine
from mautrix.crypto.sessions import InboundGroupSession as _MAU_InboundGroupSession
from mautrix.crypto.attachments import encrypt_attachment as _MAU_encrypt_attachment
from mautrix.crypto.store.asyncpg import PgCryptoStore as _MAU_PgCryptoStore
from mautrix.util.async_db import Database as _MAU_Database

HS            = os.environ["HS"].rstrip("/")
# Public-facing URL returned to signup clients (the client needs to point Element
# at the public name, not the internal docker hostname).
HS_PUBLIC     = os.environ.get("HS_PUBLIC", HS).rstrip("/")
# Sealed-env token is a *bootstrap* hint; if it's stale (M_UNKNOWN_TOKEN), the
# self-heal path on startup re-mints it from the persisted password file. So
# this can be empty on first boot and the bot will still come up via password.
TOKEN         = os.environ.get("MATRIX_TOKEN", "")
SPACE_ID      = os.environ["SPACE_ID"]
REG_TOKEN     = os.environ.get("CONDUWUIT_REGISTRATION_TOKEN", "")
CODES_PATH    = Path(os.environ.get("CODES_PATH",        "/data/codes.json"))
SIGNUP_PATH   = Path(os.environ.get("SIGNUP_CODES_PATH", "/data/signup_codes.json"))
LOG_PATH      = Path(os.environ.get("LOG_PATH",          "/data/log.jsonl"))
# Invitation web-of-trust log (issue #58/#62): one JSONL row per bot-issued
# invite into a room, (endorser, invitee, code_or_manual, room_id, ts).
ENDORSEMENTS_PATH = Path(os.environ.get("ENDORSEMENTS_PATH", "/data/endorsements.jsonl"))
SYNC_STATE    = Path(os.environ.get("SYNC_STATE",        "/data/sync_since.txt"))
HTTP_PORT     = int(os.environ.get("HTTP_PORT", "8001"))
# Bot's E2EE crypto store (megolm sessions, identity keys, peer device keys).
# Lives on the same /data volume so it survives container restarts.
CRYPTO_DB     = Path(os.environ.get("CRYPTO_DB", "/data/bot_crypto.db"))
# Durable escrow of inbound megolm sessions, written BEFORE the self-heal
# crypto wipe and re-imported into the freshly-opened store after re-mint
# (issue #60). Plaintext is fine: same /data volume + trust domain as
# bot_crypto.db itself. The file IS the escrow — replaced on each wipe.
ESCROW_PATH   = Path(os.environ.get("ESCROW_PATH", "/data/inbound_sessions.escrow.json"))
# session_id -> earliest-seen origin_server_ts index, per room (issue #77, chip
# 1 of the retention epic #76). Built cleartext off m.room.encrypted events as
# they arrive in /sync (content.session_id + origin_server_ts, no decryption
# needed) and persisted atomically. Answers "how old is the oldest message we
# still hold for this megolm session". Same /data trust domain as bot_crypto.db
# and the escrow (issue #60 precedent).
SESSION_INDEX_PATH = Path(os.environ.get("SESSION_INDEX_PATH",
                                         "/data/session_age_index.json"))

# Set by sync_loop once the persistent Olm store is open.  The inviter-side
# MSC4268 builder deliberately uses the same store as the running bot; a
# second store would not contain the sessions that the bot actually received.
_ROOM_KEY_BUNDLE_STORE = None
# Set by sync_loop alongside _ROOM_KEY_BUNDLE_STORE: the mautrix Client whose
# OlmMachine actually sends the encrypted m.room_key_bundle to-device
# messages (issue #62). MSC4268 requires the bundle sender to be the SAME
# identity that issued the room invite, so this is only ever the main bot
# (TOKEN/AUTH) — every invite path that gets bundle wiring uses that same
# identity. Never wire this up for LOBBY_AUTH invites (see
# _lobby_invite_to_space).
_ROOM_KEY_BUNDLE_CLIENT = None

# Persisted bot passwords for self-mint-on-boot. If the env-provided access
# token is stale (M_UNKNOWN_TOKEN at startup), the bot logs in with the
# password and uses the resulting fresh token instead. The /data volume
# survives container restarts so the bot stays self-healing across env-update
# clobbers, password rotations, and admin reset-password operations.
SR2_PASSWORD_PATH = Path(os.environ.get("SR2_PASSWORD_PATH",
                                         "/data/shape_rotator_2_password"))
ONBOARDING_BOT_PASSWORD_PATH = Path(os.environ.get("ONBOARDING_BOT_PASSWORD_PATH",
                                         "/data/onboarding_bot_password"))

# Persisted minted tokens + device IDs. Set after a successful self-heal
# /login. On next boot the bot prefers these over the (often-stale) env
# token, so a sealed-env clobber doesn't force a re-mint. The cached
# device_id is also passed back to /login on re-mint so Matrix returns a
# fresh token *for the same device* — preserves the bot_crypto.db pickle
# (keyed on mxid:device_id) and avoids the "new device joined, peers see
# Unable to decrypt" cycle.
SR2_TOKEN_PATH = Path(os.environ.get("SR2_TOKEN_PATH",
                                      "/data/shape_rotator_2_token"))
SR2_DEVICE_ID_PATH = Path(os.environ.get("SR2_DEVICE_ID_PATH",
                                          "/data/shape_rotator_2_device_id"))
ONBOARDING_BOT_TOKEN_PATH = Path(os.environ.get("ONBOARDING_BOT_TOKEN_PATH",
                                                "/data/onboarding_bot_token"))
ONBOARDING_BOT_DEVICE_ID_PATH = Path(os.environ.get("ONBOARDING_BOT_DEVICE_ID_PATH",
                                                    "/data/onboarding_bot_device_id"))

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


async def _login_with_password(username, password, device_id=None):
    """POST /login as username/password. If device_id is given, Matrix returns
    a fresh access_token for that *same* device — invalidates the previous
    token but leaves the crypto state for that device intact. Without
    device_id, the server allocates a new device.
    Returns (access_token, device_id, mxid) or (None, None, None) on failure."""
    payload = {
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": username},
        "password": password,
        "initial_device_display_name": f"approver-{username}",
    }
    if device_id:
        payload["device_id"] = device_id
    body = json.dumps(payload).encode()
    async with aiohttp.ClientSession(headers={"Content-Type": "application/json"}) as s:
        async with s.post(f"{HS}/_matrix/client/v3/login", data=body) as r:
            if r.status != 200:
                print(f"[self-heal] /login {username} -> {r.status}: "
                      f"{(await r.text())[:200]}", flush=True)
                return None, None, None
            j = await r.json()
            return j.get("access_token"), j.get("device_id"), j.get("user_id")


async def _whoami_with_token(token):
    """Validate `token` via /whoami. Returns the JSON body on 200, None
    otherwise. Doesn't print — caller decides what to log on failure.
    (Distinct from `_whoami(client)` lower in the file, which is the
    mautrix-client-based /whoami used after the bot is fully initialized.)"""
    if not token:
        return None
    async with aiohttp.ClientSession(
        headers={"Authorization": f"Bearer {token}"}
    ) as s:
        try:
            async with s.get(f"{HS}/_matrix/client/v3/account/whoami") as r:
                if r.status == 200:
                    return await r.json()
        except Exception:
            return None
    return None


def _read_text(path):
    try:
        return path.read_text().strip() or None
    except FileNotFoundError:
        return None


async def _resolve_credentials(env_token, username, password_path, token_path,
                               device_id_path, label):
    """Resolve a working access_token, in priority order:

      1. env_token (sealed-env hint) — if /whoami says it's still live, use it
      2. cached token persisted at token_path — if /whoami says it's live, use
      3. /login with password + cached device_id (preserves crypto state)
      4. /login with password + no device_id (fresh device — wipes crypto)

    On any successful login (paths 3+4) the new token is persisted to
    token_path and the device_id to device_id_path.

    Returns (token, device_id, mxid, was_reminted, device_changed). The last
    flag is True only if device_id differs from what's currently cached on
    disk — in that case the caller must wipe bot_crypto.db before mautrix
    starts up (the pickle is keyed on (mxid, device_id) and won't load).
    """
    cached_device_id = _read_text(device_id_path)

    # Path 1: env token (sealed-env's bootstrap hint).
    j = await _whoami_with_token(env_token)
    if j:
        device_id = j.get("device_id")
        return (env_token, device_id, j.get("user_id"),
                False, _device_changed(cached_device_id, device_id))
    if env_token:
        print(f"[{label}] env token /whoami failed; trying cached token",
              flush=True)

    # Path 2: cached token from prior self-heal.
    cached_token = _read_text(token_path)
    j = await _whoami_with_token(cached_token)
    if j:
        device_id = j.get("device_id")
        print(f"[{label}] using cached token from {token_path}", flush=True)
        return (cached_token, device_id, j.get("user_id"),
                False, _device_changed(cached_device_id, device_id))
    if cached_token:
        print(f"[{label}] cached token also stale; falling back to password",
              flush=True)

    # Paths 3+4: /login with password.
    pw = _read_text(password_path)
    if not pw:
        print(f"[{label}] no usable password at {password_path}; cannot self-mint",
              flush=True)
        return None, None, None, False, False
    new_token, new_device, mxid = await _login_with_password(
        username, pw, device_id=cached_device_id)
    if not new_token and cached_device_id:
        # Maybe the server forgot the cached device. Retry without device_id
        # to let it allocate a fresh one. The crypto wipe will happen.
        print(f"[{label}] /login with cached device_id={cached_device_id} "
              f"failed; retrying with fresh device", flush=True)
        new_token, new_device, mxid = await _login_with_password(username, pw)
    if not new_token:
        return None, None, None, False, False
    # Persist for next boot.
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(new_token)
    device_id_path.write_text(new_device)
    print(f"[{label}] self-minted fresh token; device_id={new_device} mxid={mxid} "
          f"(cached_device={cached_device_id!r}; reused={new_device == cached_device_id})",
          flush=True)
    return new_token, new_device, mxid, True, _device_changed(cached_device_id, new_device)


def _device_changed(cached, current):
    """True if the cached device_id is non-empty and differs from current.
    No prior cache (fresh /data) is NOT a device-change — there's no crypto
    state to invalidate."""
    return bool(cached) and bool(current) and cached != current


def _wipe_crypto_store():
    """Delete the bot's crypto store + sync-since cursor. Required after a
    fresh /login that allocates a *new* device_id, because the pickle is
    keyed on (mxid, device_id) and mautrix refuses to load a mismatched
    store. With device_id reuse on /login (cf. _login_with_password), this
    is rarely needed — only on a true rotation (cached device deleted on
    server) or first-time bring-up against existing /data."""
    for p in (CRYPTO_DB,
              CRYPTO_DB.with_name(CRYPTO_DB.name + "-shm"),
              CRYPTO_DB.with_name(CRYPTO_DB.name + "-wal"),
              SYNC_STATE):
        try:
            p.unlink()
            print(f"[self-heal] removed {p}", flush=True)
        except FileNotFoundError:
            pass


# --- Inbound megolm session escrow (issue #60) ---
#
# The bot's crypto store is the community's only escrow of historical megolm
# sessions. The self-heal crypto wipe (_wipe_crypto_store) used to destroy
# every inbound session on device re-mint, so any future MSC4268 history
# sharing could only cover messages since the last wipe. These two functions
# dump all inbound group sessions to ESCROW_PATH before the wipe and reload
# them into the fresh store after, preserving historical decryptability.

async def export_inbound_sessions(cs, dest=None):
    """Export every non-withheld inbound megolm group session in `cs` to a
    JSON file at `dest` (default: the module's ESCROW_PATH, resolved at call
    time). Called BEFORE _wipe_crypto_store() on device re-mint, so
    historical messages stay decryptable after the wipe.

    Each entry: room_id / sender_key / signing_key / session_id /
    first_known_index / session_key, where session_key is the base64
    ratchet key produced by InboundGroupSession.export_session(index) —
    the exact primitive from tests/history_e2ee_repro.py check 5 (PR #59).

    The file IS the durable escrow; it is replaced wholesale on each wipe.
    Returns the number of sessions written. Any failure propagates (a
    session we cannot read is a bug, not something to skip)."""
    if dest is None:
        dest = ESCROW_PATH
    rows = await cs.db.fetch(
        "SELECT room_id, session_id FROM crypto_megolm_inbound_session "
        "WHERE account_id=$1 AND withheld_code IS NULL",
        cs.account_id)
    out = []
    for row in rows:
        sess = await cs.get_group_session(row["room_id"], row["session_id"])
        if sess is None:
            raise RuntimeError(
                "inbound megolm session disappeared during escrow export: "
                f"room={row['room_id']} session={row['session_id']}"
            )
        out.append({
            "room_id":          str(sess.room_id),
            "sender_key":       str(sess.sender_key),
            "signing_key":      str(sess.signing_key),
            "session_id":       str(sess.id),
            "first_known_index": int(sess.first_known_index),
            "session_key":      sess.export_session(sess.first_known_index),
        })
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out))
    return len(out)


async def import_inbound_sessions(cs, src=None):
    """Import escrowed inbound megolm sessions into a freshly-opened crypto
    store (called after the new device_id's store is opened, post re-mint).
    `src` defaults to the module's ESCROW_PATH, resolved at call time.

    A session that fails to import RAISES — no silent skips (issue #60: a
    silently-dropped escrow entry is a silent loss of history, which is the
    bug this fixes). If the escrow is corrupt the startup fails loudly so
    the operator sees it and repairs/removes the file, instead of the bot
    looking healthy while having lost its history. Returns the count
    imported; a missing escrow file returns 0 (first boot / nothing to
    restore)."""
    if src is None:
        src = ESCROW_PATH
    if not src.exists():
        return 0
    data = json.loads(src.read_text())
    n = 0
    for entry in data:
        sess = _MAU_InboundGroupSession.import_session(
            entry["session_key"],
            signing_key=entry["signing_key"],
            sender_key=entry["sender_key"],
            room_id=entry["room_id"])
        await cs.put_group_session(
            entry["room_id"], entry["sender_key"],
            _MAU_SessionID(entry["session_id"]), sess)
        n += 1
    return n


async def _export_escrow_for_wipe(mxid, old_device_id):
    """Open the OLD crypto store (keyed on the pre-mint device_id) just long
    enough to dump its inbound sessions to ESCROW_PATH, then close it.
    No-op if the store file or the old device_id is absent (nothing to
    escrow — e.g. the transition case where no device_id was cached and the
    stale pre-PR-#37 pickle was unreadable anyway)."""
    if not old_device_id or not CRYPTO_DB.exists():
        return 0
    db = _MAU_Database.create(f"sqlite:///{CRYPTO_DB}",
                               upgrade_table=_MAU_PgCryptoStore.upgrade_table)
    await db.start()
    cs = _MAU_PgCryptoStore(account_id=mxid or "approver",
                             pickle_key=f"{mxid}:{old_device_id}", db=db)
    await cs.open()
    try:
        return await export_inbound_sessions(cs)
    finally:
        await db.stop()


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

def record_endorsement(endorser, invitee, code_or_manual, room_id):
    """Append one invitation web-of-trust edge to ENDORSEMENTS_PATH (issue
    #58/#62). Same append-only JSONL shape as audit() — no new storage
    layer. Called once per successful bot-issued room invite; endorser is
    the code's minter for code-based joins, or the bot's own mxid for
    bot-autonomous flows (per issue #62)."""
    row = {"endorser": endorser, "invitee": invitee,
           "code_or_manual": code_or_manual, "room_id": room_id,
           "ts": time.time()}
    ENDORSEMENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ENDORSEMENTS_PATH.open("a") as f:
        f.write(json.dumps(row) + "\n")

def _endorser_for_code(code, fallback):
    """Resolve the endorser mxid for a knock/lobby code: the code's minter
    (cmd_mint's minted_by, set on codes minted after issue #62) if known,
    else `fallback` (the bot's own identity — INITIAL_CODES-seeded or
    pre-#62 codes have no minter on file, so the bot is the de facto
    endorser for those bot-autonomous flows)."""
    entry = _load(CODES_PATH).get(code) or {}
    return entry.get("minted_by") or fallback

# --- session_id -> earliest-ts age index (issue #77) ---
# The disk file is the source of truth: record_session loads-mutates-saves it on
# every call, so a process restart between syncs sees the same index by
# construction (no in-memory cache to lose). A missing file is a first-boot {}
# (not an error); a corrupt file RAISES via _load (json.loads) — no silent skip
# of state loss, the #60 convention.

def record_session(room_id, session_id, ts):
    """Record that megolm `session_id` was seen in `room_id` at ms-timestamp
    `ts`, keeping the EARLIEST ts seen (the index answers 'how old is the oldest
    message we still hold for this session'). Persists atomically. Existing
    entries with a smaller ts are left untouched."""
    ts = int(ts)
    index = _load(SESSION_INDEX_PATH)
    room = index.setdefault(room_id, {})
    if session_id not in room or ts < room[session_id]:
        room[session_id] = ts
        _save(SESSION_INDEX_PATH, index)

def session_age_index(room_id):
    """Return {session_id: earliest_ts} for `room_id` (empty dict if the room
    has no indexed sessions). Reads live from the persisted file, so a caller
    always sees state that survives a restart."""
    return dict(_load(SESSION_INDEX_PATH).get(room_id, {}))

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

# After successful VETTING promotion (knock path), mint a multi-use signup
# code so the new member can register accounts/agents on this server
# without having to ping an admin. 0 disables the feature.
LOBBY_WELCOME_CODE_USES = int(os.environ.get("LOBBY_WELCOME_CODE_USES", "10"))
# Optional onboarding doc link, posted alongside the welcome signup code.
LOBBY_WELCOME_DOC_URL = os.environ.get(
    "LOBBY_WELCOME_DOC_URL",
    "https://github.com/amiller/smithers-toys/blob/main/demos/shape-rotator-matrix-welcome.md",
).strip()

# Welcome-room flow — POST /join/api maps each invite code to one public
# room. State: WELCOME_PATH is keyed by code ->
#   {room_id, room_alias, created_at, joined_by?, joined_at?}
# (issue #3). Codes are consumed on the first user JOIN, not at POST time.
WELCOME_PATH       = Path(os.environ.get("WELCOME_PATH",        "/data/welcome_rooms.json"))
WELCOME_SECRET_PATH = Path(os.environ.get("WELCOME_SECRET_PATH", "/data/welcome_secret"))
# Legacy pre-#3 haiku-lobby state, left on /data by older deploys. Read once
# at startup by migrate_legacy_lobbies, then removed.
LOBBY_PATH         = Path(os.environ.get("LOBBY_PATH",        "/data/lobby.json"))
# A joined room is reaped this long after the join; an unused one this long
# after minting. Kicked, tombstoned, and removed from the mapping either way.
WELCOME_JOINED_TTL = int(os.environ.get("WELCOME_JOINED_TTL_SEC", "1800"))
WELCOME_ROOM_TTL   = int(os.environ.get("WELCOME_ROOM_TTL_SEC",   "172800"))

# Retention rooms (issue #78 / epic #76). The bot's in-force retention
# policy per room — write-once at creation; this is the source of truth
# chip 3 (#79) reads to withhold keys, NOT the room's m.room.retention
# state (which is mutable on the wire and therefore not authoritative).
RETENTION_PATH     = Path(os.environ.get("RETENTION_PATH",     "/data/retention_rooms.json"))
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


async def _lobby_invite_to_space(mxid, endorser=None, code_or_manual="manual"):
    """Invite mxid to the space using the lobby/onboarding bot. Returns
    (status, body[:300]). Caller distinguishes 200 (invited) from 403
    (already a member — treat as success).

    Deliberately does NOT send an MSC4268 bundle, even though the space
    itself is unencrypted today: MSC4268 requires the bundle sender to be
    the SAME identity that issued the m.room.member invite, and this
    invite goes out as the dedicated lobby/onboarding bot (LOBBY_AUTH), not
    the main bot whose OlmMachine _send_room_key_bundle uses. The actual
    E2EE child-room invites that follow (_invite_to_children) go out via
    the main bot's identity and do get bundles. The endorsement edge IS
    recorded here — it's about the invite itself, not bundle delivery."""
    async with aiohttp.ClientSession(
        headers={**LOBBY_AUTH, "Content-Type": "application/json"}
    ) as s:
        url = f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(SPACE_ID)}/invite"
        async with s.post(url, json={"user_id": mxid,
                                     "reason": "vetted via lobby airlock"}) as r:
            status, body = r.status, (await r.text())[:300]
    if status == 200:
        record_endorsement(endorser or OUR_MXID, mxid, code_or_manual, SPACE_ID)
    return status, body


async def _invite_to_children(mxid, endorser=None, code_or_manual="manual"):
    """Invite mxid to each SPACE_CHILD_IDS room using the main bot (TOKEN),
    which has admin PL across the children. Lobby users are typically
    remote (matrix.org), so we can't join on their behalf the way the
    signup flow does — we invite, and they accept.

    Returns list of child IDs successfully invited (status 200) or already
    a member (status 403). Per-child errors are logged but don't fail the
    overall promotion. For each FRESH invite (200), also olm-sends an
    MSC4268 m.room_key_bundle to mxid (issue #62) and records the
    endorsement edge — skipped on 403 since the user already has whatever
    history access they're going to get."""
    invited = []
    for child in SPACE_CHILD_IDS:
        st, body = await _admin_invite(mxid, child, reason="vetted via lobby airlock")
        if st == 200:
            invited.append(child)
            await _send_room_key_bundle(mxid, child)
            record_endorsement(endorser or OUR_MXID, mxid, code_or_manual, child)
        elif st == 403:
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


async def _promote(client, mxid, endorser=None, code_or_manual="manual"):
    """Invite mxid to the space as `client`.

    Note: bundle-sending uses the global _ROOM_KEY_BUNDLE_CLIENT, not the
    `client` param above — callers must only ever pass the main bot's
    client here (the same identity that issued the invite, per MSC4268)."""
    try:
        await client.api.request("POST",
            f"/_matrix/client/v3/rooms/{SPACE_ID}/invite",
            content={"user_id": mxid, "reason": "vetted via airlock"})
        # Same identity as _ROOM_KEY_BUNDLE_CLIENT (main bot / TOKEN), so
        # this is a correct bundle-send target if SPACE_ID is ever made
        # E2EE — currently a no-op via _room_history_shareable.
        await _send_room_key_bundle(mxid, SPACE_ID)
        record_endorsement(endorser or OUR_MXID, mxid, code_or_manual, SPACE_ID)
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


def iter_encrypted_events(rooms_data):
    """Yield (room_id, session_id, origin_server_ts) for every megolm
    m.room.encrypted timeline event in a /sync batch, in arrival order.

    session_id is a cleartext field on m.room.encrypted and origin_server_ts is
    the event's top-level timestamp, so the session-age index (issue #77) can be
    built WITHOUT decrypting anything. Events lacking session_id/ts (a non-megolm
    or malformed shape) are skipped — a non-event, not a state read failure."""
    for room_id, rd in rooms_data.get("join", {}).items():
        for ev in rd.get("timeline", {}).get("events", []):
            if ev.get("type") != "m.room.encrypted":
                continue
            c = ev.get("content") or {}
            sid = c.get("session_id")
            ts = ev.get("origin_server_ts")
            if sid is None or ts is None:
                continue
            yield room_id, sid, int(ts)


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
            code_or_manual = meta.get("code") or "manual"
            endorser = _endorser_for_code(meta.get("code"), OUR_MXID)
            st, body = await _promote(client, meta["mxid"],
                                      endorser=endorser, code_or_manual=code_or_manual)
            if st == 200:
                meta["promoted"] = True
                meta["promoted_at"] = time.time()
                meta["displayname"] = displayname
                invited_children = await _invite_to_children(
                    meta["mxid"], endorser=endorser, code_or_manual=code_or_manual)
                meta["invited_children"] = invited_children
                signup_code, signup_url = _mint_welcome_signup_code(meta["mxid"])
                ack = "nice — invited you to shape rotator. you can leave this room."
                if LOBBY_WELCOME_DOC_URL:
                    ack += f"\n\nonboarding doc: {LOBBY_WELCOME_DOC_URL}"
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


# --- Welcome-room flow (POST /join/api → one public room per code → join → space) ---
#
# Replaces the knock dance (and the older haiku lobby) for users who get the
# /join?code=… link. Each code maps to ONE public room, minted idempotently:
# the code is baked into the room's identity instead of being pasted into
# Element's knock-reason box. The user's only UI step is a plain "Join"
# button. The bot sees the join in /sync, invites the joiner to the space,
# and only once that invite went out consumes one use of the code and
# posts a confirmation — a failed invite consumes nothing, so a rejoin
# retries. Rooms are tombstoned and forgotten on a timer (issue #3).

def _welcome_secret():
    """Server-side secret mixed into welcome-room alias hashes so the alias
    can't be derived offline from a guessed code. Generated once, persisted
    on the /data volume."""
    try:
        return WELCOME_SECRET_PATH.read_text().strip()
    except FileNotFoundError:
        WELCOME_SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_hex(32)
        WELCOME_SECRET_PATH.write_text(secret)
        return secret


def _welcome_alias_local(code):
    """Alias localpart for a code: welcome-<sha256(code+secret)[:8]>.
    Not derivable without the server secret, so an un-joined (un-consumed)
    welcome room can't be found by enumerating codes."""
    digest = hashlib.sha256(
        (code + ":" + _welcome_secret()).encode()).hexdigest()
    return f"welcome-{digest[:8]}"


async def _create_welcome_room(alias_local):
    """Create the public welcome room for a code, using the onboarding-bot
    token. Returns room_id. Raises on failure.

    Pinned to room_version=11 because continuwuity's default (room_v12)
    produced rooms the local homeserver did not authoritatively own,
    causing a federated user joining the room to leave the bot's local
    membership state desynced (404 on m.room.member queries, 403
    "Event is not authorized" on subsequent sends, M_NOT_FOUND on
    /join). Pinning to v11 keeps the room firmly local-authoritative
    and the federation path well-trodden.
    """
    body = {
        "preset": "public_chat",       # join_rule=public — plain Join button
        "visibility": "private",       # don't list in the public room directory
        "room_alias_name": alias_local,
        "name":  "Shape Rotator welcome",
        "topic": "join this room, the bot will invite you to the space",
        "room_version": "11",
        # world_readable history bypasses continuwuity's per-event
        # visibility check, which fails with "shortstatehash not found"
        # after a remote fast_join (the optimisation continuwuity uses
        # when a federated user joins a public room without full state
        # resolution). With shared history (the public_chat default),
        # the bot's messages get stuck — matrix.org asks "can I see
        # this?" and continuwuity can't answer, so the user's Element
        # never renders the confirmation. world_readable means "anyone
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
            room_id = (await r.json())["room_id"]
        # Belt-and-suspenders: explicitly /join the room. createRoom
        # auto-joins the creator, but if anything goes sideways with
        # room state federation, this re-asserts the bot's local
        # member event so subsequent sends are authorized. A non-200
        # means the bot is NOT joined: the room cannot serve the
        # welcome flow (no confirmation post, no join observed), so
        # raise rather than persist a dead mapping — the stranded
        # alias is freed by the M_ROOM_IN_USE retry on the next
        # request.
        join_url = f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/join"
        async with s.post(join_url, json={}) as r:
            if r.status != 200:
                raise RuntimeError(
                    f"post-create join {room_id} -> {r.status}: "
                    f"{(await r.text())[:200]}")
        return room_id


async def _welcome_room_alive(room_id):
    """True if the mapped welcome room can still serve a join: no
    m.room.tombstone state and the onboarding bot still joined. The
    tombstone read alone cannot prove membership — an evicted or departed
    bot can still read world_readable state (404 "no tombstone" looked
    alive) — so liveness also asks /joined_rooms, which answers for this
    token. 200 = tombstoned; 403 = state unreadable (dead); any other
    status raises."""
    async with aiohttp.ClientSession(headers=LOBBY_AUTH) as s:
        url = (f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}"
               f"/state/m.room.tombstone")
        async with s.get(url) as r:
            if r.status in (200, 403):
                return False
            if r.status != 404:
                raise RuntimeError(
                    f"tombstone check {room_id} -> {r.status}: "
                    f"{(await r.text())[:200]}")
        async with s.get(f"{HS}/_matrix/client/v3/joined_rooms") as r:
            if r.status != 200:
                raise RuntimeError(
                    f"joined_rooms -> {r.status}: {(await r.text())[:200]}")
            return room_id in (await r.json())["joined_rooms"]


async def _delete_room_alias(full_alias):
    """Delete a room alias. 404 (already gone) is fine; anything else
    raises. The welcome- alias namespace is ours — the localpart needs the
    server secret to compute — so freeing a stale alias from a reaped or
    orphaned room is always safe."""
    async with aiohttp.ClientSession(headers=LOBBY_AUTH) as s:
        url = (f"{HS}/_matrix/client/v3/directory/room/"
               f"{urllib.parse.quote(full_alias)}")
        async with s.delete(url) as r:
            if r.status not in (200, 404):
                raise RuntimeError(
                    f"delete alias {full_alias} -> {r.status}: "
                    f"{(await r.text())[:200]}")


async def join_handler(request):
    """POST /join/api {code} → {room_alias} or {error}.

    Idempotent per code: a live welcome room is reused, and no use is
    consumed here — that happens when a user actually joins the room, so a
    clicked-but-abandoned link never burns the code."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "bad_json"}, status=400)
    code = (data.get("code") or "").strip()

    codes = _load(CODES_PATH)
    entry = codes.get(code)
    if not entry:
        audit({"type": "welcome_rejected", "code": code, "why": "invalid_code"})
        return web.json_response({"error": "invalid_code"}, status=403)
    if entry.get("uses_remaining", 0) <= 0:
        audit({"type": "welcome_rejected", "code": code, "why": "code_exhausted"})
        return web.json_response({"error": "code_exhausted"}, status=403)

    welcome = _load(WELCOME_PATH)
    meta = welcome.get(code)
    if meta:
        try:
            alive = await _welcome_room_alive(meta["room_id"])
        except Exception as e:
            print(f"[welcome] liveness check failed for {code}: {e}", flush=True)
            return web.json_response(
                {"error": "liveness_check_failed",
                 "detail": str(e)[:200]}, status=500)
        if alive:
            return web.json_response({"room_alias": meta["room_alias"]})
        # Dead room (tombstoned/evicted but not yet swept): mint a fresh one.

    alias_local = _welcome_alias_local(code)
    full_alias = f"#{alias_local}:{SERVER_NAME}"
    try:
        try:
            room_id = await _create_welcome_room(alias_local)
        except RuntimeError as e:
            if "M_ROOM_IN_USE" not in str(e):
                raise
            # Stale alias from this code's reaped or orphaned room (reaping
            # deletes it, but a failed mint — crash, or a welcome message
            # that could not be sent — can strand one). Free it and mint
            # exactly once more.
            print(f"[welcome] alias {full_alias} taken; freeing and "
                  f"retrying: {str(e)[:120]}", flush=True)
            await _delete_room_alias(full_alias)
            room_id = await _create_welcome_room(alias_local)

        # Pinned welcome message (issue #3 flow step 3): unencrypted — the
        # room is public by design — and visible pre-join thanks to
        # world_readable. Sent BEFORE the mapping is saved: a welcome room
        # without its message is not the room the spec hands out, so a
        # send/pin failure must leave nothing persisted — the stranded
        # alias is freed by the M_ROOM_IN_USE retry on the next request,
        # which re-mints with the message.
        msg = ("welcome — sit tight, you're about to be invited to the "
               "main space. join this room to get the invite.")
        event_id = await _send_msg_raw(room_id, msg)
        async with aiohttp.ClientSession(
            headers={**LOBBY_AUTH, "Content-Type": "application/json"}
        ) as s:
            pin_url = (f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}"
                       f"/state/m.room.pinned_events")
            async with s.put(pin_url, json={"pinned": [event_id]}) as r:
                if r.status != 200:
                    raise RuntimeError(
                        f"pin welcome message {r.status}: {(await r.text())[:200]}")
    except Exception as e:
        audit({"type": "welcome_room_failed", "code": code, "err": str(e)[:300]})
        print(f"[welcome] room mint failed: {e}", flush=True)
        return web.json_response({"error": "create_failed",
                                  "detail": str(e)[:200]}, status=500)

    welcome[code] = {
        "room_id": room_id,
        "room_alias": full_alias,
        "created_at": time.time(),
    }
    _save(WELCOME_PATH, welcome)

    audit({"type": "welcome_created", "room": room_id, "alias": full_alias,
           "code": code})
    print(f"[welcome] {full_alias} ({code}) -> {room_id}", flush=True)
    return web.json_response({"room_alias": full_alias})


def iter_welcome_joins(rooms_data, welcome_state, self_mxid):
    """Yield (code, meta, joiner_mxid) for the first not-yet-consumed user
    join in each mapped welcome room present in this /sync batch.

    self_mxid is the onboarding bot's own mxid (its creator-join doesn't
    count). Rooms whose meta already carries joined_by are skipped — that
    is the exactly-once guard for replayed/duplicate join events."""
    by_room = {meta["room_id"]: (code, meta)
               for code, meta in welcome_state.items()}
    for room_id, rd in rooms_data.get("join", {}).items():
        pair = by_room.get(room_id)
        if not pair:
            continue
        code, meta = pair
        if meta.get("joined_by"):
            continue
        joiner = None
        for section in ("state", "timeline"):
            for ev in rd.get(section, {}).get("events", []):
                if ev.get("type") != "m.room.member":
                    continue
                if (ev.get("content") or {}).get("membership") != "join":
                    continue
                mxid = ev.get("state_key", "")
                if mxid and mxid != self_mxid:
                    joiner = mxid
                    break
            if joiner:
                break
        if joiner:
            yield code, meta, joiner


async def process_welcome_join(code, meta, joiner, lobby_mxid, welcome):
    """Consume one code use and promote the first joiner of its welcome room:
    invite the joiner to the space, then — only once the invite went out
    (200) or they were already in (403) — decrement uses_remaining and set
    joined_by/joined_at (the exactly-once guard for replayed/duplicate
    joins), persisting both files before the confirmation post. Any other
    invite status consumes nothing and leaves the guard unset, so a later
    join event (the joiner rejoining) retries instead of finding the code
    burned with no invite."""
    endorser = _endorser_for_code(code, lobby_mxid)
    st, body = await _lobby_invite_to_space(
        joiner, endorser=endorser, code_or_manual=code)
    # /invite returning 403 means "already in the space" (the lobby bot has
    # PL>=50 by construction) — treat as success so an operator can self-test
    # the flow with their own already-membered account.
    if st != 200 and st != 403:
        audit({"type": "welcome_invite_failed", "user": joiner,
               "code": code, "room": meta["room_id"], "status": st,
               "body": body[:200]})
        print(f"[welcome] invite of {joiner} failed status={st}: "
              f"{body[:200]}", flush=True)
        return

    codes = _load(CODES_PATH)
    entry = codes.get(code) or {}
    entry["uses_remaining"] = max(0, entry.get("uses_remaining", 0) - 1)
    codes[code] = entry

    meta["joined_by"] = joiner
    meta["joined_at"] = time.time()
    # The confirmation post is the one fallible call left in this path and
    # raises on failure, so both files must already be on disk by then: a
    # failure after only the decrement was saved left the guard unset, and
    # the replayed join re-invited and decremented again. The guard is saved
    # first so a crash between the two writes strands a use unconsumed,
    # never consumed twice.
    _save(WELCOME_PATH, welcome)
    _save(CODES_PATH, codes)
    audit({"type": "welcome_joined", "user": joiner, "code": code,
           "room": meta["room_id"], "uses_left": entry["uses_remaining"],
           "already_member": st == 403})

    ack = ("invite sent — accept it in Element and you're in."
           if st == 200 else
           "you're already in shape rotator — see you in the space.")
    await _send_msg_raw(meta["room_id"], ack)
    print(f"[welcome] {joiner} joined via {code} "
          f"(uses_left={entry['uses_remaining']})", flush=True)


async def _tombstone_welcome_room(room_id, lobby_mxid, room_alias=None):
    """Close a welcome room: kick every member but the bot, tombstone it,
    free its alias, then leave. Raises on failure so cleanup keeps the
    mapping and retries; every step is idempotent against current state, so
    a retry after a partial failure converges."""
    async with aiohttp.ClientSession(
        headers={**LOBBY_AUTH, "Content-Type": "application/json"}
    ) as s:
        members_url = (f"{HS}/_matrix/client/v3/rooms/"
                       f"{urllib.parse.quote(room_id)}/members?membership=join")
        async with s.get(members_url) as r:
            if r.status != 200:
                raise RuntimeError(
                    f"members {room_id} -> {r.status}: {(await r.text())[:200]}")
            chunk = (await r.json()).get("chunk", [])
        for ev in chunk:
            mxid = ev.get("state_key", "")
            if not mxid or mxid == lobby_mxid:
                continue
            kick_url = (f"{HS}/_matrix/client/v3/rooms/"
                        f"{urllib.parse.quote(room_id)}/kick")
            async with s.post(kick_url, json={
                    "user_id": mxid, "reason": "welcome room closed"}) as r:
                if r.status != 200:
                    raise RuntimeError(
                        f"kick {mxid} from {room_id} -> {r.status}: "
                        f"{(await r.text())[:200]}")
        tombstone_url = (f"{HS}/_matrix/client/v3/rooms/"
                         f"{urllib.parse.quote(room_id)}/state/m.room.tombstone")
        async with s.put(tombstone_url, json={
                "replacement_room": SPACE_ID,
                "body": "this welcome room is closed"}) as r:
            if r.status != 200:
                raise RuntimeError(
                    f"tombstone {room_id} -> {r.status}: {(await r.text())[:200]}")
    if room_alias:
        await _delete_room_alias(room_alias)
    await _lobby_leave_room(room_id, reason="welcome room closed")


async def cleanup_welcome_rooms(welcome_state, lobby_mxid):
    """Reap expired welcome rooms: joined ones WELCOME_JOINED_TTL after the
    join, unused ones WELCOME_ROOM_TTL after minting. Kicks the members,
    tombstones the room, and deletes the mapping. A room whose cleanup fails
    keeps its mapping and is retried next cycle. Returns True if state changed."""
    now = time.time()
    dirty = False
    for code, meta in list(welcome_state.items()):
        joined_at = meta.get("joined_at")
        if joined_at is not None:
            expired = now - joined_at > WELCOME_JOINED_TTL
        else:
            expired = now - meta.get("created_at", 0) > WELCOME_ROOM_TTL
        if not expired:
            continue
        try:
            await _tombstone_welcome_room(
                meta["room_id"], lobby_mxid, meta.get("room_alias"))
        except Exception as e:
            audit({"type": "welcome_cleanup_failed", "code": code,
                   "room": meta["room_id"], "err": str(e)[:300]})
            print(f"[welcome] cleanup of {meta['room_id']} failed "
                  f"(will retry): {e}", flush=True)
            continue
        del welcome_state[code]
        audit({"type": "welcome_reaped", "code": code,
               "room": meta["room_id"],
               "joined_by": meta.get("joined_by")})
        print(f"[welcome] reaped {meta['room_alias']}", flush=True)
        dirty = True
    return dirty


async def migrate_legacy_lobbies():
    """One-shot: leave every room tracked by the pre-#3 haiku-lobby state
    file, then remove the file. Users mid-haiku are cut loose — the flow is
    replaced wholesale — and this keeps no orphaned tracked rooms behind."""
    if not LOBBY_PATH.exists():
        return
    legacy = _load(LOBBY_PATH)
    for room_id in legacy:
        await _lobby_leave_room(room_id, reason="lobby flow replaced by "
                                                "welcome rooms — get a fresh link")
    LOBBY_PATH.unlink()
    print(f"[welcome] migrated {len(legacy)} legacy lobby room(s): "
          f"left + removed {LOBBY_PATH}", flush=True)


# --- Welcome-room sync loop (runs as the dedicated onboarding bot) ---

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
    drive welcome-room processing. Runs in parallel with the main sync_loop.

    Cleartext-only — no OlmMachine, no crypto store. Welcome rooms are
    public + cleartext by design, so raw HTTP is sufficient.
    """
    lobby_mxid, lobby_device = await _lobby_whoami()
    if not lobby_mxid:
        print("[welcome sync] LOBBY_TOKEN whoami failed; welcome flow disabled",
              flush=True)
        return
    print(f"[welcome sync] running as {lobby_mxid}; device={lobby_device}",
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

        welcome = _load(WELCOME_PATH)
        w_dirty = False
        for code, meta, joiner in iter_welcome_joins(
                rooms_data, welcome, lobby_mxid):
            await process_welcome_join(
                code, meta, joiner, lobby_mxid, welcome)
            w_dirty = True
        if await cleanup_welcome_rooms(welcome, lobby_mxid):
            w_dirty = True
        if w_dirty:
            _save(WELCOME_PATH, welcome)


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

# Where to post "X joined via code" operator notifications.
# Defaults to FEED_ROOM (matrix-devops). Set to a private DM room id with
# the bot to keep these out of the public feed.
OPERATOR_NOTIFY_ROOM = os.environ.get("OPERATOR_NOTIFY_ROOM") or FEED_ROOM
# Sidecar bookkeeping for which welcome-room joins have already been
# announced to OPERATOR_NOTIFY_ROOM. Kept separate from WELCOME_PATH so
# the welcome loop and sync_loop never write the same JSON file.
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


def _admin_event_trust_state(event):
    """Return the crypto trust state attached by mautrix to a decrypted event.

    A Matrix event's sender MXID and room power level are not enough for an
    admin command: a compromised homeserver can forge both.  Only decrypted
    Megolm events carry mautrix's device-chain result, so cleartext commands
    deliberately resolve to UNVERIFIED.
    """
    metadata = event.get("mautrix", {}) or {}
    if not metadata.get("was_encrypted"):
        return _MAU_TrustState.UNVERIFIED
    trust = metadata.get("trust_state", _MAU_TrustState.UNVERIFIED)
    try:
        return trust if isinstance(trust, _MAU_TrustState) else _MAU_TrustState(trust)
    except (TypeError, ValueError):
        return _MAU_TrustState.UNVERIFIED


def _is_cross_signed_admin_event(event):
    """Require a stable, cross-signed sender device for admin commands.

    CROSS_SIGNED_TOFU means the device is signed by the user's self-signing
    key, which is signed by the user's master key, and that master key has not
    changed since the bot first observed it.  A rotation therefore fails
    closed until the operator explicitly re-verifies/reprovisions the bot's
    crypto store.  This is the strongest trust state mautrix-python 0.19 can
    resolve without its unfinished local-user-trust implementation.
    """
    return _admin_event_trust_state(event) >= _MAU_TrustState.CROSS_SIGNED_TOFU


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
        codes[code] = {"uses_remaining": uses, "label": label, "minted_by": sender}
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


# --- Retention room factory (issue #78 / epic #76 chip 2) ---
#
# A retention room is bot-created: the bot is sole PL100, the room is E2EE,
# m.room.retention carries the policy window, join_rule is restricted to the
# space + the room is a space child, and a plain-language policy message is
# pinned. The policy is IMMUTABLE at creation: it is recorded once in the
# RETENTION_PATH store and never overwritten. Chip 3 (#79) reads THAT store
# (not the room's m.room.retention state) to decide which keys to withhold,
# which is exactly why a later state-event change has no effect on the
# enforced policy.

def _parse_duration(s):
    """Parse '7d' / '12h' / '30d' / '2w' / '90m' / '3600s' into seconds.

    A bare integer is treated as seconds. Raises ValueError on malformed
    input. Used by !retention-room.
    """
    s = (s or "").strip().lower()
    if not s:
        raise ValueError("empty duration")
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    if s[-1] in units:
        n, unit = s[:-1], s[-1]
    else:
        n, unit = s, "s"
    try:
        amount = int(n)
    except ValueError:
        raise ValueError(f"bad duration {s!r}: expected a number")
    if amount <= 0:
        raise ValueError(f"duration must be positive, got {amount}")
    return amount * units[unit]


def _render_window(seconds):
    """Render a window (seconds) as a short human label (2w/7d/12h/90m/3600s)."""
    seconds = int(seconds)
    for secs, suffix in ((604800, "w"), (86400, "d"), (3600, "h"), (60, "m")):
        if seconds % secs == 0:
            return f"{seconds // secs}{suffix}"
    return f"{seconds}s"


RETENTION_POLICY_TEMPLATE = """\
📜 Retention policy — immutable at room creation

Room: {name}
Policy window: {window} ({window_seconds}s)

What this DOES: the bot (sole PL100) withholds megolm room keys older than the
policy window from members who receive history through it. New joiners cannot
decrypt messages older than the window via the bot's key custody.

What this DOES NOT do: delete anything from the server. Events and media stay
stored. Members who were present when a message was sent keep their own keys
and local plaintext. This is retention as access control, not deletion.

This policy was set when the room was created and is the one the bot enforces
permanently; it cannot be changed — not by the bot, not by any admin. A later
m.room.retention state change in this room is ignored and logged. To change
retention, create a new room.
"""


def _retention_policy_text(name, window_seconds):
    return RETENTION_POLICY_TEMPLATE.format(
        name=name, window=_render_window(window_seconds),
        window_seconds=int(window_seconds))


def _load_retention():
    return _load(RETENTION_PATH)


def _save_retention(state):
    _save(RETENTION_PATH, state)


def _record_retention_policy(room_id, record):
    """Write-once: record the in-force retention policy for a room.

    Immutability is the whole point (epic #76). If a record already exists
    for this room it is NEVER overwritten — we raise rather than silently
    weakening the guarantee. Chip 3 (#79) reads this record, not the room's
    m.room.retention state.
    """
    state = _load_retention()
    if room_id in state:
        raise RuntimeError(
            f"retention policy for {room_id} already recorded; refusing to "
            f"overwrite (immutable-at-creation)")
    state[room_id] = record
    _save_retention(state)


async def _create_retention_room(name, window_seconds, creator=None):
    """Create a bot-owned retention room and record its immutable policy.

    Returns a dict: {room_id, name, window_seconds, max_lifetime_ms,
    pinned_event_id, policy_text, space_id, created_by}.

    The bot is creator + sole PL100; the room is E2EE; m.room.retention carries
    the window (ms, per spec); join_rule is restricted to SPACE_ID so existing
    space members auto-join; the room is linked as a space child; a plain-
    language policy message is pinned. The policy is recorded write-once in
    RETENTION_PATH — the in-force policy chip 3 (#79) reads, NOT the room state.
    """
    if not OUR_MXID:
        raise RuntimeError("cannot create retention room: OUR_MXID unknown "
                           "(sync_loop must run /whoami first)")
    if not SPACE_ID:
        raise RuntimeError("cannot create retention room: SPACE_ID not set")

    via_server = SERVER_NAME or OUR_MXID.split(":", 1)[1]
    max_lifetime_ms = int(window_seconds) * 1000
    policy_text = _retention_policy_text(name, window_seconds)
    human = _render_window(window_seconds)

    # public_chat preset ⇒ history_visibility=shared, which the bot's key
    # custody requires to cover the room (epic #76). join_rule is overridden
    # to restricted via initial_state. visibility=private keeps it out of the
    # room directory. room_version 11: continuwuity's default (v12) produced
    # rooms the local HS did not authoritatively own — see _create_lobby_room_raw.
    body = {
        "name": name,
        "topic": (f"retention room — bot sole admin; policy window {human} "
                  f"(immutable at creation)"),
        "preset": "public_chat",
        "visibility": "private",
        "room_version": "11",
        # Bot is the ONLY user at PL100; nobody else is ≥50 (users_default 0).
        # state_default stays at the preset default (50) so only the bot can
        # set state in this room.
        "power_level_content_override": {
            "users": {OUR_MXID: 100},
            "users_default": 0,
        },
        "initial_state": [
            {"type": "m.room.history_visibility", "state_key": "",
             "content": {"history_visibility": "shared"}},
            {"type": "m.room.encryption", "state_key": "",
             "content": {"algorithm": "m.megolm.v1.aes-sha2"}},
            # m.room.retention is an unknown state type to continuwuity; the
            # server accepts and stores it (epic #76). max_lifetime is in
            # MILLISECONDS per the Matrix room retention spec.
            {"type": "m.room.retention", "state_key": "",
             "content": {"max_lifetime": max_lifetime_ms}},
            {"type": "m.room.join_rules", "state_key": "",
             "content": {
                 "join_rule": "restricted",
                 "allow": [{"type": "m.room_membership", "room_id": SPACE_ID}],
             }},
        ],
    }

    async with aiohttp.ClientSession(
        headers={**AUTH, "Content-Type": "application/json"}
    ) as s:
        # 1. createRoom — bot becomes creator + sole PL100 + auto-joined.
        async with s.post(f"{HS}/_matrix/client/v3/createRoom", json=body) as r:
            if r.status != 200:
                raise RuntimeError(
                    f"createRoom retention {r.status}: {(await r.text())[:300]}")
            room_id = (await r.json())["room_id"]

        # 2. Link as a space child so the room appears in the space.
        child_url = (f"{HS}/_matrix/client/v3/rooms/"
                     f"{urllib.parse.quote(SPACE_ID)}/state/m.space.child/"
                     f"{urllib.parse.quote(room_id)}")
        async with s.put(child_url, json={"via": [via_server]}) as r:
            if r.status != 200:
                raise RuntimeError(
                    f"m.space.child {r.status}: {(await r.text())[:300]}")

        # 3. Send the plain-language policy message as a plaintext NOTICE.
        # This is a deliberate transparency statement that every member and
        # observer must be able to read without megolm keys; the retention
        # guarantee itself is enforced by key custody (chip 3), independent
        # of this message's transport.
        txn = f"sr-retention-{secrets.token_hex(8)}"
        msg_url = (f"{HS}/_matrix/client/v3/rooms/"
                   f"{urllib.parse.quote(room_id)}/send/m.room.message/{txn}")
        async with s.put(msg_url, json={
                "msgtype": "m.text", "body": policy_text}) as r:
            if r.status != 200:
                raise RuntimeError(
                    f"policy message {r.status}: {(await r.text())[:300]}")
            pinned_event_id = (await r.json())["event_id"]

        # 4. Pin the policy message.
        pin_url = (f"{HS}/_matrix/client/v3/rooms/"
                   f"{urllib.parse.quote(room_id)}/state/m.room.pinned_events")
        async with s.put(pin_url, json={"pinned": [pinned_event_id]}) as r:
            if r.status != 200:
                raise RuntimeError(
                    f"m.room.pinned_events {r.status}: {(await r.text())[:300]}")

    record = {
        "name": name,
        "window_seconds": int(window_seconds),
        "max_lifetime_ms": max_lifetime_ms,
        "created_at": time.time(),
        "created_by": creator or OUR_MXID,
        "policy_text": policy_text,
        "pinned_event_id": pinned_event_id,
        "space_id": SPACE_ID,
    }
    _record_retention_policy(room_id, record)
    audit({"type": "retention_room_created", "room": room_id, "name": name,
           "window_seconds": int(window_seconds), "pinned": pinned_event_id,
           "created_by": record["created_by"]})
    return {**record, "room_id": room_id}


async def _detect_retention_tamper(client, rooms_data):
    """Scan one /sync batch for m.room.retention state changes in known
    retention rooms. For each change whose value differs from the immutable
    in-force policy, audit + post a notice to OPERATOR_NOTIFY_ROOM.

    The in-force policy store is intentionally NOT touched: a state-event
    change on the wire does not change what the bot enforces (chip 3 reads
    the store). This is the 'log and post a notice if one is attempted'
    work item of issue #78.
    """
    state = _load_retention()
    if not state:
        return
    join = (rooms_data or {}).get("join", {}) or {}
    for room_id, record in list(state.items()):
        room = join.get(room_id)
        if not room:
            continue
        events = list((room.get("state") or {}).get("events") or [])
        events += list((room.get("timeline") or {}).get("events") or [])
        recorded_ms = record.get("max_lifetime_ms")
        for ev in events:
            if ev.get("type") != "m.room.retention" or ev.get("state_key") != "":
                continue
            attempted_ms = (ev.get("content") or {}).get("max_lifetime")
            # Matching the in-force value = the bot's own create-time set or a
            # no-op re-send; not a tamper. Only a DIFFERING value is flagged.
            if attempted_ms == recorded_ms:
                continue
            sender = ev.get("sender")
            audit({"type": "retention_tamper", "room": room_id, "by": sender,
                   "attempted_ms": attempted_ms, "in_force_ms": recorded_ms})
            print(f"[retention] tamper in {room_id}: sender={sender} set "
                  f"max_lifetime={attempted_ms} (in-force={recorded_ms}); "
                  f"policy unchanged", flush=True)
            if OPERATOR_NOTIFY_ROOM:
                try:
                    await _send_msg(client, OPERATOR_NOTIFY_ROOM,
                        f"⚠️ retention tamper in {room_id}: {sender} set "
                        f"m.room.retention max_lifetime={attempted_ms} ms; "
                        f"in-force policy is still {recorded_ms} ms "
                        f"({record.get('window_seconds')}s) — ignored.")
                except Exception as e:
                    print(f"[retention] notice send failed: {e}", flush=True)


async def cmd_retention_room(client, room_id, sender, args):
    """!retention-room <name> <duration> — create a bot-owned retention room.

    The bot becomes sole PL100; the room is E2EE with an m.room.retention
    policy window; it is restricted-joined to the space and linked as a space
    child. The policy is immutable at creation (epic #76). <duration> is like
    7d, 12h, 30d, 2w, 3600s, 90m.

    Examples:
      !retention-room weekly-standup 7d
      !retention-room ephemeral 24h
    """
    parts = args.split()
    if len(parts) != 2:
        return ("usage: !retention-room <name> <duration>   "
                "(e.g. !retention-room ops-chat 7d)")
    name, dur = parts
    if not name or len(name) > 64:
        return f"!retention-room: name must be 1..64 chars, got {name!r}"
    try:
        window_seconds = _parse_duration(dur)
    except ValueError as e:
        return f"!retention-room: {e}"
    try:
        rec = await _create_retention_room(
            name, window_seconds, creator=sender)
    except Exception as e:
        audit({"type": "retention_room_failed", "name": name, "window": dur,
               "err": str(e)[:300], "created_by": sender})
        return f"!retention-room failed: {type(e).__name__}: {e}"
    return (f"created retention room {rec['room_id']}\n"
            f"  name:   {name}\n"
            f"  window: {_render_window(window_seconds)} ({window_seconds}s)\n"
            f"  policy: immutable at creation; bot sole PL100; "
            f"space-child of {SPACE_ID}")


def _parse_admin_target(args, command):
    parts = args.split(maxsplit=1)
    if not parts or not parts[0].startswith("@") or ":" not in parts[0]:
        return None, f"usage: {command} <@user:server> [reason]"
    return parts[0], (parts[1].strip() if len(parts) > 1 else "admin command")


async def _membership_action(client, action, user_id, reason="admin command"):
    try:
        await client.api.request(
            "POST",
            f"/_matrix/client/v3/rooms/{urllib.parse.quote(SPACE_ID, safe='')}/{action}",
            content={"user_id": user_id, "reason": reason},
        )
    except Exception as e:
        status = getattr(e, "http_status", None)
        detail = str(e)[:200]
        return False, f"{status}: {detail}" if status else detail
    return True, "ok"


async def cmd_kick(client, room_id, sender, args):
    user_id, reason = _parse_admin_target(args, "!kick")
    if not user_id:
        return reason
    if user_id == OUR_MXID:
        return "refused: I will not kick myself"
    ok, detail = await _membership_action(client, "kick", user_id, reason)
    audit({"type": "admin_kick", "target": user_id, "reason": reason,
           "requested_by": sender, "ok": ok, "detail": detail})
    return f"kicked {user_id} from the space" if ok else f"!kick failed: {detail}"


async def cmd_ban(client, room_id, sender, args):
    user_id, reason = _parse_admin_target(args, "!ban")
    if not user_id:
        return reason
    if user_id == OUR_MXID:
        return "refused: I will not ban myself"
    ok, detail = await _membership_action(client, "ban", user_id, reason)
    audit({"type": "admin_ban", "target": user_id, "reason": reason,
           "requested_by": sender, "ok": ok, "detail": detail})
    return f"banned {user_id} from the space" if ok else f"!ban failed: {detail}"


async def cmd_unban(client, room_id, sender, args):
    user_id, reason = _parse_admin_target(args, "!unban")
    if not user_id:
        return reason
    ok, detail = await _membership_action(client, "unban", user_id, reason)
    audit({"type": "admin_unban", "target": user_id, "reason": reason,
           "requested_by": sender, "ok": ok, "detail": detail})
    return f"unbanned {user_id} from the space" if ok else f"!unban failed: {detail}"


def _stats_last_24h():
    cutoff = time.time() - 24 * 60 * 60
    counts = {"knocks": 0, "promoted": 0, "rejected": 0}
    keywords = {}
    try:
        lines = LOG_PATH.read_text().splitlines()
    except FileNotFoundError:
        lines = []
    for line in lines:
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        try:
            event_ts = float(event.get("ts", 0))
        except (TypeError, ValueError):
            continue
        if event_ts < cutoff:
            continue
        kind = event.get("type")
        if kind in ("vetting_room_created", "knock_rejected", "welcome_created"):
            counts["knocks"] += 1
        if kind in ("promoted", "welcome_joined"):
            counts["promoted"] += 1
        if kind in ("knock_rejected", "vetting_failed", "vetting_timeout",
                    "welcome_rejected", "welcome_invite_failed"):
            counts["rejected"] += 1
        keyword = event.get("keyword")
        if keyword and kind == "vetting_room_created":
            keywords[keyword] = keywords.get(keyword, 0) + 1
    pending = 0
    pending += sum(1 for meta in _load(VETTING_PATH).values()
                   if not meta.get("closed") and not meta.get("promoted"))
    pending += sum(1 for meta in _load(WELCOME_PATH).values()
                   if not meta.get("joined_by"))
    top = ", ".join(f"{word} ({n})" for word, n in
                     sorted(keywords.items(), key=lambda item: (-item[1], item[0]))[:5])
    return (f"last 24h: knocks={counts['knocks']}, promoted={counts['promoted']}, "
            f"rejected={counts['rejected']}, pending={pending}\n"
            f"top captcha keywords: {top or 'none'}")


async def cmd_stats(client, room_id, sender, args):
    return _stats_last_24h()


COMMANDS = {
    "!mint": cmd_mint,
    "!codes": cmd_codes,
    "!revoke": cmd_revoke,
    "!retention-room": cmd_retention_room,
    "!kick": cmd_kick,
    "!ban": cmd_ban,
    "!unban": cmd_unban,
    "!stats": cmd_stats,
    "!help": None,  # filled below
}

async def cmd_help(client, room_id, sender, args):
    return ("commands: " +
            ", ".join(sorted(c for c in COMMANDS if c != "!help")) +
            ", !help")
COMMANDS["!help"] = cmd_help


async def process_admin_command(client, room_id, event_id, sender, body,
                                cross_signed=False):
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
    if not cross_signed:
        print(f"[admin] refused {cmd} from {sender}: unverified device", flush=True)
        await _send_msg(client, room_id,
            f"{sender}: refused — admin commands require a cross-signing-verified device")
        audit({"type": "admin_unverified", "cmd": cmd, "sender": sender})
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


async def announce_welcome_events(client):
    """Scan welcome-room state and emit an operator notification when a
    code's room is actually used (a join → space invite). Posts to
    OPERATOR_NOTIFY_ROOM via the main bot's E2EE-aware client. Idempotent —
    uses a sidecar JSON file so the welcome loop never has to know about
    announce flags."""
    if not OPERATOR_NOTIFY_ROOM:
        return
    welcome = _load(WELCOME_PATH)
    if not welcome:
        # GC bookkeeping for codes whose rooms disappeared (reaped).
        if OPERATOR_ANNOUNCE_PATH.exists():
            seen = _load(OPERATOR_ANNOUNCE_PATH)
            stale = [k for k in seen if k not in welcome]
            for k in stale:
                seen.pop(k, None)
            if stale:
                _save(OPERATOR_ANNOUNCE_PATH, seen)
        return
    # First-run backfill suppression: if the announce file doesn't exist
    # yet, seed it with everything currently in the welcome store marked as
    # already-announced. Otherwise the first cycle after deploy would spam
    # retroactive messages for historical rooms.
    first_run = not OPERATOR_ANNOUNCE_PATH.exists()
    seen = _load(OPERATOR_ANNOUNCE_PATH)
    if first_run:
        for code, meta in welcome.items():
            seen[code] = {"joined": bool(meta.get("joined_by"))}
        _save(OPERATOR_ANNOUNCE_PATH, seen)
        return
    dirty = False
    for code, meta in welcome.items():
        joiner = meta.get("joined_by")
        if not joiner:
            continue
        rec = seen.setdefault(code, {"joined": False})
        if rec["joined"]:
            continue
        await _send_msg(client, OPERATOR_NOTIFY_ROOM,
            f"🚪 {joiner} joined via code {code} — invited to the space")
        rec["joined"] = True
        dirty = True
    # Drop bookkeeping ONLY for codes whose room disappeared from the
    # welcome store (reaped). A code re-minting a fresh room later gets a
    # fresh key and a fresh announcement for its new join — correct.
    for code in list(seen.keys()):
        if code not in welcome:
            seen.pop(code, None)
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


async def build_room_key_bundle(room_id) -> dict:
    """Export and upload this bot's MSC4268 room-key bundle for ``room_id``.

    Shape Rotator rooms have always used ``history_visibility: shared``.  We
    therefore include every inbound Megolm session, including sessions whose
    original ``m.room_key`` predates mautrix's ``shared_history`` support.
    The returned mapping is the attachment portion ready to put in the
    ``file`` field of an ``m.room_key_bundle`` to-device message.
    """
    if _ROOM_KEY_BUNDLE_STORE is None:
        raise RuntimeError("MSC4268 crypto store is not initialized")

    rows = await _ROOM_KEY_BUNDLE_STORE.db.fetch(
        """
        SELECT session_id, sender_key, signing_key, withheld_code
        FROM crypto_megolm_inbound_session
        WHERE room_id=$1 AND account_id=$2
        ORDER BY session_id
        """,
        room_id,
        _ROOM_KEY_BUNDLE_STORE.account_id,
    )
    room_keys, withheld = [], []
    for row in rows:
        session_id = str(row["session_id"])
        sender_key = str(row["sender_key"])
        if row["withheld_code"] is not None:
            withheld.append({
                "algorithm": "m.megolm.v1.aes-sha2",
                "code": str(row["withheld_code"]),
                "reason": "The session is withheld by the crypto store",
                "room_id": str(room_id),
                "sender_key": sender_key,
                "session_id": session_id,
            })
            continue

        session = await _ROOM_KEY_BUNDLE_STORE.get_group_session(
            room_id, session_id
        )
        if session is None:
            raise RuntimeError(f"inbound session disappeared: {room_id}/{session_id}")
        room_keys.append({
            "algorithm": "m.megolm.v1.aes-sha2",
            "room_id": str(room_id),
            "sender_claimed_keys": {"ed25519": str(session.signing_key)},
            "sender_key": sender_key,
            "session_id": session_id,
            "session_key": session.export_session(session.first_known_index),
        })

    plaintext = json.dumps(
        {"room_keys": room_keys, "withheld": withheld},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    ciphertext, encrypted_file = _MAU_encrypt_attachment(plaintext)
    async with aiohttp.ClientSession(
        headers={"Authorization": f"Bearer {TOKEN}"}
    ) as http:
        # The media-repo upload endpoint consumes the encrypted bytes as the
        # request body.  Multipart would store the MIME envelope itself and
        # make the EncryptedFile SHA-256 verification fail on download.
        async with http.post(
            f"{HS}/_matrix/media/v3/upload?filename=room-key-bundle.json",
            data=ciphertext,
            headers={"Content-Type": "application/octet-stream"},
        ) as response:
            if response.status not in (200, 201):
                raise RuntimeError(
                    f"media upload failed: HTTP {response.status}: "
                    f"{(await response.text())[:500]}"
                )
            uploaded = await response.json()

    content_uri = uploaded.get("content_uri")
    if not content_uri:
        raise RuntimeError(f"media upload omitted content_uri: {uploaded}")
    file_info = encrypted_file.serialize()
    file_info["url"] = content_uri
    return {"url": content_uri, "file": file_info}


async def _room_history_shareable(room_id):
    """True if room_id has m.room.encryption set AND history_visibility is
    shared or world_readable. Invites that don't land in such a room (e.g.
    the space itself, which is unencrypted) get no MSC4268 bundle — there's
    nothing useful to export, or the room already discloses history on join."""
    async with aiohttp.ClientSession(headers=AUTH) as s:
        try:
            async with s.get(
                f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}"
                f"/state/m.room.encryption") as r:
                if r.status != 200:
                    return False
        except Exception:
            return False
        try:
            async with s.get(
                f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}"
                f"/state/m.room.history_visibility") as r:
                if r.status != 200:
                    return False
                hv = (await r.json()).get("history_visibility")
        except Exception:
            return False
    return hv in ("shared", "world_readable")


async def _send_room_key_bundle(mxid, room_id):
    """Build this bot's MSC4268 bundle for room_id and olm-send it as an
    encrypted m.room_key_bundle to-device message to every device of mxid
    (issue #62). The sender is always _ROOM_KEY_BUNDLE_CLIENT's identity —
    the same one that must have issued the invite, per MSC4268.

    Best-effort: returns False and only logs on any failure (crypto client
    not up yet, room not a shareable E2EE room, build/upload error, device
    fetch/send error). A bundle-delivery problem must never fail the invite
    that already succeeded."""
    if _ROOM_KEY_BUNDLE_CLIENT is None or _ROOM_KEY_BUNDLE_STORE is None:
        print(f"[room_key_bundle] crypto client not ready; skipping {room_id} -> {mxid}",
              flush=True)
        return False
    if not await _room_history_shareable(room_id):
        return False
    try:
        bundle = await build_room_key_bundle(room_id)
    except Exception as e:
        print(f"[room_key_bundle] build failed room={room_id} mxid={mxid}: {e}",
              flush=True)
        return False

    content = {"room_id": room_id, "file": bundle["file"]}
    event_type = _MAU_EventType.find("m.room_key_bundle", _MAU_EventType.Class.TO_DEVICE)
    try:
        devices = await _ROOM_KEY_BUNDLE_CLIENT.crypto._fetch_keys(
            [_MAU_UserID(mxid)], include_untracked=True)
    except Exception as e:
        print(f"[room_key_bundle] device fetch failed mxid={mxid}: {e}", flush=True)
        return False

    sent = 0
    for device in devices.get(_MAU_UserID(mxid), {}).values():
        try:
            await _ROOM_KEY_BUNDLE_CLIENT.crypto.send_encrypted_to_device(
                device, event_type, content)
            sent += 1
        except Exception as e:
            print(f"[room_key_bundle] send failed mxid={mxid} "
                  f"device={device.device_id} room={room_id}: {e}", flush=True)
    if sent:
        print(f"[room_key_bundle] sent to {sent} device(s) of {mxid} for room={room_id}",
              flush=True)
    else:
        print(f"[room_key_bundle] no devices reachable for {mxid} in room={room_id}",
              flush=True)
    return sent > 0


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
    global _ROOM_KEY_BUNDLE_STORE, _ROOM_KEY_BUNDLE_CLIENT
    _ROOM_KEY_BUNDLE_STORE = cs
    _ROOM_KEY_BUNDLE_CLIENT = client
    ss = _StateStore(state_store)
    olm = _MAU_OlmMachine(client, cs, ss)
    olm.share_keys_min_trust = _MAU_TrustState.UNVERIFIED
    olm.send_keys_min_trust = _MAU_TrustState.UNVERIFIED
    await olm.load()
    client.crypto = olm
    client.crypto_store = cs

    # Restore escrowed inbound megolm sessions into the fresh store (issue
    # #60). On the first boot after this change there is no escrow file and
    # this is a 0-session no-op. After a re-mint that wiped the store, this
    # is what keeps pre-wipe history decryptable. A corrupt escrow raises
    # (no silent skip) — the startup fails loudly so the operator repairs
    # or removes the file rather than the bot silently losing history.
    _n = await import_inbound_sessions(cs)
    if _n:
        print(f"[startup] restored {_n} escrowed inbound megolm sessions "
              f"from {ESCROW_PATH}", flush=True)

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
            await admin_queue.put((str(evt.event_id), ev_sender, body,
                                   _is_cross_signed_admin_event(evt)))
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

        # session_id -> earliest-ts age index (retention epic #76, chip 1 / #77).
        # Built cleartext off the m.room.encrypted events the bot just received;
        # record_session keeps the min ts and persists atomically. Wrapped like
        # announce_welcome_events above so a transient index-write fault logs
        # loudly without killing the sync heartbeat (the next event re-records).
        for rid, sid, its in iter_encrypted_events(data.get("rooms", {})):
            try:
                record_session(rid, sid, its)
            except Exception as e:
                print(f"[session_index error] {type(e).__name__}: {e}",
                      flush=True)

        # Welcome flow runs in its own /sync loop (lobby_sync_loop) under the
        # dedicated onboarding-bot identity (LOBBY_TOKEN), so it doesn't appear
        # here. See lobby_sync_loop() below.

        # Operator notifications about welcome-room joins. The onboarding
        # bot can't post to OPERATOR_NOTIFY_ROOM (which is typically E2EE
        # and only the main bot has keys for), so the main bot scans
        # welcome state each cycle and emits the announcements itself.
        try:
            await announce_welcome_events(client)
        except Exception as e:
            print(f"[announce_welcome_events] {type(e).__name__}: {e}", flush=True)

        # Retention tamper detection (issue #78): if a known retention room
        # shows an m.room.retention state change this cycle, log + post a
        # notice. The in-force policy (write-once store) is NEVER updated.
        try:
            await _detect_retention_tamper(client, data.get("rooms", {}))
        except Exception as e:
            print(f"[retention] tamper scan error: {type(e).__name__}: {e}", flush=True)

        # Drain queued admin commands (event handler may have populated
        # them with decrypted message events).
        while not admin_queue.empty():
            try:
                ev_id, sender, body, cross_signed = admin_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            await process_admin_command(client, ADMIN_COMMAND_ROOM, ev_id, sender,
                                        body, cross_signed)


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
    if st == 200:
        # Same identity as _ROOM_KEY_BUNDLE_CLIENT (main bot / TOKEN) issued
        # this invite, so bundle-sending is identity-correct (issue #62).
        # No-op today via _room_history_shareable since the space isn't E2EE.
        await _send_room_key_bundle(mxid, SPACE_ID)
        record_endorsement(entry.get("inviter") or entry.get("minted_by") or OUR_MXID,
                           mxid, code, SPACE_ID)
    else:
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
    global SERVER_NAME, TOKEN, AUTH, LOBBY_TOKEN, LOBBY_AUTH

    # Self-heal @shape-rotator-2 (main bot). Tries env token, then cached
    # /data token, then password /login (with cached device_id so the
    # crypto store stays valid across re-mints).
    sr2_cached_device_before = _read_text(SR2_DEVICE_ID_PATH)
    sr2_crypto_existed_before = CRYPTO_DB.exists()
    sr2_token, _sr2_device, sr2_mxid, sr2_reminted, sr2_device_changed = \
        await _resolve_credentials(
            TOKEN, "shape-rotator-2", SR2_PASSWORD_PATH,
            SR2_TOKEN_PATH, SR2_DEVICE_ID_PATH, "shape-rotator-2")
    if not sr2_token:
        raise SystemExit(
            "fatal: cannot authenticate as @shape-rotator-2. "
            "Either MATRIX_TOKEN is unset/invalid AND no password is "
            f"persisted at {SR2_PASSWORD_PATH}.")
    # Always wire the resolved token in — env_token-success path returns
    # reminted=False but still gives back the working token, which may
    # differ from the (possibly-stale) os.environ["MATRIX_TOKEN"] global.
    TOKEN = sr2_token
    AUTH = {"Authorization": f"Bearer {TOKEN}"}
    # Wipe crypto if the device_id rotated (caught by _device_changed) OR
    # if we just minted fresh against an existing crypto.db that has no
    # cached device_id alongside it (transition case: pre-PR-#37 deploy
    # left a stale pickle for an unknown device).
    if sr2_device_changed or (
            sr2_reminted and not sr2_cached_device_before and sr2_crypto_existed_before):
        # Escrow inbound megolm sessions BEFORE the wipe so historical
        # messages stay decryptable after re-mint (issue #60). Only the
        # device-rotated path has a known old pickle key to read from; the
        # transition case (no cached device_id) cannot be escrowed and those
        # stale pickles were unreadable anyway. Escrow failure propagates so
        # the store is never wiped after an unsuccessful backup.
        if sr2_cached_device_before:
            _n = await _export_escrow_for_wipe(sr2_mxid, sr2_cached_device_before)
            print(f"[self-heal] escrowed {_n} inbound megolm sessions to "
                  f"{ESCROW_PATH} before crypto wipe", flush=True)
        _wipe_crypto_store()

    # Self-heal @onboarding-bot (lobby flow). Same shape; no crypto wipe
    # because the lobby sync loop is cleartext.
    if LOBBY_TOKEN != sr2_token:  # only when a dedicated lobby bot is configured
        lobby_token, _, _, _lobby_reminted, _ = await _resolve_credentials(
            LOBBY_TOKEN, "onboarding-bot", ONBOARDING_BOT_PASSWORD_PATH,
            ONBOARDING_BOT_TOKEN_PATH, ONBOARDING_BOT_DEVICE_ID_PATH,
            "onboarding-bot")
        if lobby_token:
            LOBBY_TOKEN = lobby_token
            LOBBY_AUTH = {"Authorization": f"Bearer {LOBBY_TOKEN}"}
        else:
            print("[self-heal] onboarding-bot auth failed; lobby flow disabled",
                  flush=True)

    if not SERVER_NAME:
        SERVER_NAME = sr2_mxid.split(":", 1)[1]
    print(f"approver starting. space={SPACE_ID} signup_enabled={bool(REG_TOKEN)} "
          f"server_name={SERVER_NAME!r}", flush=True)
    for p in (CODES_PATH, SIGNUP_PATH, LOG_PATH, VETTING_PATH, WELCOME_PATH):
        p.parent.mkdir(parents=True, exist_ok=True)
    if not CODES_PATH.exists():   _save(CODES_PATH,   {})
    if not SIGNUP_PATH.exists():  _save(SIGNUP_PATH,  {})
    if not VETTING_PATH.exists(): _save(VETTING_PATH, {})
    if not WELCOME_PATH.exists(): _save(WELCOME_PATH, {})
    merge_seed(CODES_PATH,  "INITIAL_CODES")
    merge_seed(SIGNUP_PATH, "INITIAL_SIGNUP_CODES")

    # Close out the pre-#3 haiku-lobby rooms before the welcome loop starts,
    # so nothing tracked by the old state file is left dangling.
    await migrate_legacy_lobbies()

    await run_http()
    # Run main sync_loop (knocks, vetting, admin commands) and lobby_sync_loop
    # (dedicated onboarding-bot identity for the welcome-room flow) in parallel.
    # If LOBBY_TOKEN == TOKEN (no dedicated bot configured), both loops sync
    # the same user — works but wasteful; configure ONBOARDING_BOT_TOKEN.
    await asyncio.gather(sync_loop(), lobby_sync_loop())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
