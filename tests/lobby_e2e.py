"""End-to-end test of the welcome-room flow + an actual E2EE round-trip.

What it asserts (issue #3):
  1. POST /join/api with a valid code returns the #welcome-… alias.
  2. A fresh user can directly /join the returned room (plain public Join —
     no knock UI, no captcha).
  3. The bot posts the confirmation "invite sent — accept it in Element and
     you're in." and invites the joiner to the space.
  4. The user accepts and auto-joins the E2EE child room (`#bot-noise`).
  5. A SECOND fresh user, via their own code's welcome room, ends up in the
     same E2EE child room.
  6. User #1 sends an encrypted message in #bot-noise; user #2's OlmMachine
     decrypts it. This is the actual E2EE assertion — a megolm round-trip
     between two independently-onboarded users that proves the welcome flow
     doesn't wedge crypto.
  7. An already-space-member re-runs the flow with a fresh code: the invite
     403s as already-member and the bot still confirms (operator self-test
     path).
  8. A hard-failing space invite (PR #91 review finding): a joiner whose
     invite the HS 500s (a federated mxid — the production norm — which
     this federation-disabled stack hard-fails) consumes nothing and arms
     no exactly-once guard, and no confirmation is posted; the retry with
     a joiner the HS accepts then consumes exactly one use and the invite
     actually lands.

Env (all pre-set by run_in_runner.sh):
  DEV_HS              homeserver URL (landing nginx)
  DEV_REG_TOKEN       continuwuity registration token
  DEV_WELCOME_CODE    a code with >= 1 use for user #1
  DEV_WELCOME_CODE_2  a distinct code with >= 1 use for user #2
  DEV_WELCOME_CODE_3  a distinct code with >= 1 use for the redo pass
  SPACE_ID            unsuffixed space room id
  SPACE_CHILD_IDS     comma-separated child room IDs
  DEV_LOBBY_TOKEN     the room bot's own access token. The e2e stack runs
                      the lobby flow on the MATRIX_TOKEN identity (no
                      ONBOARDING_BOT_TOKEN configured), so run_in_runner.sh
                      passes MATRIX_TOKEN here; a dedicated-bot stack must
                      pass that bot's token instead. Used only to make the
                      bot leave a room — the eviction-liveness case.
"""
import asyncio, json, os, secrets, sys, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))

from sas_e2e import make_client, sync_once, register

from mautrix.types import (EventType, MessageType, TextMessageEventContent)

HS                = os.environ.get("DEV_HS", "http://landing:80").rstrip("/")
REG_TOKEN         = os.environ["DEV_REG_TOKEN"]
WELCOME_CODE      = os.environ["DEV_WELCOME_CODE"]
WELCOME_CODE_2    = os.environ["DEV_WELCOME_CODE_2"]
WELCOME_CODE_3    = os.environ["DEV_WELCOME_CODE_3"]
LOBBY_TOKEN       = os.environ["DEV_LOBBY_TOKEN"]
SPACE_ID          = os.environ["SPACE_ID"]
SPACE_CHILD_IDS = [c.strip() for c in os.environ["SPACE_CHILD_IDS"].split(",") if c.strip()]
ENC_ROOM = SPACE_CHILD_IDS[-1] if SPACE_CHILD_IDS else None

CONFIRM = "invite sent — accept it in Element and you're in."
CONFIRM_ALREADY = "you're already in shape rotator — see you in the space."

results = []
def log(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    results.append((name, ok))


def http(method, path, token=None, body=None, timeout=15):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{HS}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:    return e.code, json.loads(e.read())
        except: return e.code, {}


async def _wait_for_message(token, room_id, needle, timeout=30):
    """Long-poll the room timeline until a message containing `needle`
    arrives. Returns the message body or None."""
    since = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        url = "/_matrix/client/v3/sync?timeout=10000"
        if since:
            url += f"&since={urllib.parse.quote(since)}"
        _s, sync = http("GET", url, token=token, timeout=15)
        since = sync.get("next_batch") or since
        joined = sync.get("rooms", {}).get("join", {}).get(room_id, {})
        for ev in joined.get("timeline", {}).get("events", []):
            if ev.get("type") != "m.room.message":
                continue
            body = (ev.get("content") or {}).get("body", "")
            if needle in body:
                return body
        await asyncio.sleep(1)
    return None


def _wait_for_invite(token, predicate, timeout=15):
    """Poll /sync until an invited room matches predicate(rid). Returns rid or None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        _s, sync = http("GET", "/_matrix/client/v3/sync?timeout=0", token=token)
        for rid in sync.get("rooms", {}).get("invite", {}).keys():
            if predicate(rid):
                return rid
        time.sleep(1)
    return None


async def onboard_via_welcome(label, code):
    """Register, mint the code's welcome room via /join/api, join it with a
    plain public join, receive the space invite + confirmation, land in the
    space."""
    username = f"e2e_welcome_{label}_{int(time.time())}_{secrets.token_hex(2)}"
    device   = f"E2EW{label.upper()}{secrets.token_hex(2)}"
    mxid, token = register(username, secrets.token_urlsafe(32), device)
    print(f"[{label}] registered {mxid} device={device}", flush=True)

    # /join/api is unauthenticated — anyone holding the code can mint the room.
    s, j = http("POST", "/join/api", body={"code": code})
    log(f"[{label}] /join/api returned 200",
        s == 200 and j.get("room_alias", "").startswith("#welcome-"),
        f"status={s} body={j}")
    if s != 200:
        return None
    alias = j["room_alias"]

    _s, dirr = http("GET", f"/_matrix/client/v3/directory/room/"
                    f"{urllib.parse.quote(alias)}")
    room_id = dirr.get("room_id")
    log(f"[{label}] welcome alias resolves", bool(room_id), f"dir={dirr}")
    if not room_id:
        return None

    # Public room → user joins directly via the alias (the one UI step).
    s, _ = http("POST",
                f"/_matrix/client/v3/join/{urllib.parse.quote(alias)}",
                token=token, body={})
    log(f"[{label}] joined welcome room via alias", s == 200,
        f"status={s} alias={alias}")
    if s != 200:
        return None

    space_prefix = SPACE_ID.split(":")[0]
    invited = _wait_for_invite(
        token, lambda rid: rid.split(":")[0] == space_prefix, timeout=15)
    log(f"[{label}] space invite after welcome join (within 15s)", bool(invited))

    confirm = await _wait_for_message(token, room_id, "Element and you're in")
    log(f"[{label}] confirmation message in welcome room",
        bool(confirm and CONFIRM in confirm), f"msg={confirm!r}")
    if not invited:
        return None

    s, _ = http("POST",
                f"/_matrix/client/v3/rooms/{urllib.parse.quote(SPACE_ID)}/join",
                token=token, body={})
    log(f"[{label}] accepted space invite", s == 200, f"status={s}")

    for child in SPACE_CHILD_IDS:
        http("POST",
             f"/_matrix/client/v3/rooms/{urllib.parse.quote(child)}/join",
             token=token, body={})

    return mxid, token, device


async def main():
    if not SPACE_CHILD_IDS:
        print("no SPACE_CHILD_IDS — cannot run E2EE round-trip portion", file=sys.stderr)
        sys.exit(2)

    # Reject path: bogus code → /join/api returns 403, no room minted.
    s, j = http("POST", "/join/api", body={"code": "definitely-not-a-code"})
    log("/join/api rejects bogus code", s == 403 and j.get("error") == "invalid_code",
        f"status={s} body={j}")

    a = await onboard_via_welcome("alice", WELCOME_CODE)
    b = await onboard_via_welcome("bob", WELCOME_CODE_2)
    if not a or not b:
        print("onboarding failed; skipping E2EE round-trip")
        sys.exit(1)
    a_mxid, a_token, a_device = a
    b_mxid, b_token, b_device = b

    a_client, a_cs, a_ss, a_db = await make_client(
        a_mxid, a_token, a_device, db_path=f"/tmp/{secrets.token_hex(4)}_wa.db")
    b_client, b_cs, b_ss, b_db = await make_client(
        b_mxid, b_token, b_device, db_path=f"/tmp/{secrets.token_hex(4)}_wb.db")
    await a_client.crypto.share_keys()
    await b_client.crypto.share_keys()

    for _ in range(3):
        await sync_once(a_client, a_ss, timeout=2000, first=True)
        await sync_once(b_client, b_ss, timeout=2000, first=True)

    a_enc = await a_ss.is_encrypted(ENC_ROOM)
    b_enc = await b_ss.is_encrypted(ENC_ROOM)
    log("E2EE child room reports encrypted (alice side)", bool(a_enc))
    log("E2EE child room reports encrypted (bob side)",   bool(b_enc))

    secret = f"welcome-e2e secret {secrets.token_hex(8)}"
    event_id = await a_client.send_message_event(
        ENC_ROOM, EventType.ROOM_MESSAGE,
        TextMessageEventContent(msgtype=MessageType.TEXT, body=secret))
    log("alice sent encrypted message", bool(event_id), f"event_id={event_id}")

    decrypted_body = None
    deadline = time.time() + 30
    received = asyncio.Event()

    async def on_msg(evt):
        nonlocal decrypted_body
        if evt.room_id != ENC_ROOM or evt.sender == b_mxid:
            return
        body = getattr(evt.content, "body", "") or ""
        if body == secret:
            decrypted_body = body
            received.set()

    b_client.add_event_handler(EventType.ROOM_MESSAGE, on_msg)
    while time.time() < deadline and not received.is_set():
        await sync_once(b_client, b_ss, timeout=2000)
    log("bob decrypted alice's message via OlmMachine",
        decrypted_body == secret, f"got={decrypted_body!r}")

    await a_db.stop()
    await b_db.stop()

    # Already-member pass: alice (now in the space) re-runs the flow with a
    # FRESH code. The space invite 403s as already-member; the bot must still
    # treat it as success and confirm — this is the operator self-test path.
    s, j = http("POST", "/join/api", body={"code": WELCOME_CODE_3})
    log("[alice-redo] /join/api returned 200 for existing member",
        s == 200 and j.get("room_alias", "").startswith("#welcome-"),
        f"status={s} body={j}")
    if s == 200:
        alias = j["room_alias"]
        s2, _ = http("POST",
                     f"/_matrix/client/v3/join/{urllib.parse.quote(alias)}",
                     token=a_token, body={})
        log("[alice-redo] joined fresh welcome room as existing member",
            s2 == 200, f"status={s2}")

        _s, dirr = http("GET", f"/_matrix/client/v3/directory/room/"
                        f"{urllib.parse.quote(alias)}")
        redo_room = dirr.get("room_id")
        ack = await _wait_for_message(
            a_token, redo_room, "already in shape rotator", timeout=30)
        log("[alice-redo] got 'already in space' ack from bot",
            bool(ack and CONFIRM_ALREADY in ack), f"ack={ack!r}")

    # PR #91 review finding: the liveness check used to read only
    # m.room.tombstone, so a room the bot had LEFT — no tombstone — read
    # as alive and /join/api kept returning the stale, unusable alias.
    # Make the room bot leave a freshly minted room, then a POST must
    # remint: same deterministic alias, NEW room behind it.
    s, j = http("POST", "/join/api", body={"code": WELCOME_CODE_2})
    log("[eviction] minted a room for a live code", s == 200,
        f"status={s} body={j}")
    if s == 200:
        alias = j["room_alias"]
        _s, dirr = http("GET", f"/_matrix/client/v3/directory/room/"
                        f"{urllib.parse.quote(alias)}")
        old_room = dirr.get("room_id")
        # world_readable: a non-member can read state. Prove the room
        # carries no tombstone, so the remint below can only be driven by
        # the membership check.
        s2, _ = http("GET", f"/_matrix/client/v3/rooms/"
                     f"{urllib.parse.quote(old_room)}/state/m.room.tombstone",
                     token=a_token)
        log("[eviction] old room carries no tombstone", s2 == 404,
            f"status={s2}")
        s3, _ = http("POST", f"/_matrix/client/v3/rooms/"
                     f"{urllib.parse.quote(old_room)}/leave",
                     token=LOBBY_TOKEN, body={})
        log("[eviction] room bot left the room (no tombstone)", s3 == 200,
            f"status={s3}")
        s4, _j = http("POST", "/join/api", body={"code": WELCOME_CODE_2})
        _s, dirr2 = http("GET", f"/_matrix/client/v3/directory/room/"
                         f"{urllib.parse.quote(alias)}")
        log("[eviction] POST remints instead of returning the stale alias",
            s4 == 200 and dirr2.get("room_id")
            and dirr2["room_id"] != old_room,
            f"status={s4} old={old_room} new={dirr2.get('room_id')}")

    # PR #91 review finding #2: the use used to be decremented and
    # joined_by recorded BEFORE the space invite was attempted, so any
    # non-200/403 invite status permanently burned the code and armed the
    # exactly-once guard — the joiner never got the invite and no rejoin
    # could retry. Drive process_welcome_join over the real HS: the failed
    # invite is a joiner with a federated mxid (the production norm for
    # this flow), which this federation-disabled stack's HS answers with a
    # real 500 M_UNKNOWN — the same non-200/403 class as the transient
    # invite failures the review names. The retry then uses a local joiner
    # the HS accepts. Same import-in-process shape as retention_room_e2e.py;
    # own state files so the running approver's /data state is untouched
    # (its loop has no mapping for this room and ignores the join).
    import tempfile as _tempfile
    _tmp = _tempfile.mkdtemp()
    _saved_env = dict(os.environ)
    _s, _who = http("GET", "/_matrix/client/v3/account/whoami", token=LOBBY_TOKEN)
    _lobby_mxid = _who["user_id"]
    _server_name = _lobby_mxid.split(":", 1)[1]
    os.environ.update({
        "CODES_PATH": f"{_tmp}/codes.json",
        "WELCOME_PATH": f"{_tmp}/welcome_rooms.json",
        "WELCOME_SECRET_PATH": f"{_tmp}/welcome_secret",
        "LOG_PATH": f"{_tmp}/log.jsonl",
        "ENDORSEMENTS_PATH": f"{_tmp}/endorsements.jsonl",
        "LOBBY_SYNC_STATE": f"{_tmp}/lobby_sync.txt",
    })
    sys.path.insert(0, str(REPO / "knock-approver"))
    import approver as _approver
    os.environ.clear()
    os.environ.update(_saved_env)
    _approver.SERVER_NAME = _server_name

    _code = "e2e-invitefail-" + secrets.token_hex(4)
    _approver._save(_approver.CODES_PATH, {_code: {"uses_remaining": 3}})
    _alias_local = _approver._welcome_alias_local(_code)
    _room = await _approver._create_welcome_room(_alias_local)
    _full_alias = f"#{_alias_local}:{_server_name}"
    _approver._save(_approver.WELCOME_PATH, {_code: {
        "room_id": _room, "room_alias": _full_alias, "created_at": time.time()}})

    _if_mxid, _if_token = register(
        f"e2e_invitefail_{int(time.time())}_{secrets.token_hex(2)}",
        secrets.token_urlsafe(32), f"EIF{secrets.token_hex(2)}")
    s, _ = http("POST", f"/_matrix/client/v3/join/{urllib.parse.quote(_full_alias)}",
                token=_if_token, body={})
    log("[invite-fail] user joins the room", s == 200, f"status={s}")
    if s == 200:
        # Phase A: the joiner's space invite 500s — nothing may be consumed.
        _welcome_all = _approver._load(_approver.WELCOME_PATH)
        _meta = _welcome_all[_code]
        await _approver.process_welcome_join(
            _code, _meta, "@e2e-invitefail-remote:unreachable.invalid",
            _lobby_mxid, _welcome_all)

        _uses = _approver._load(_approver.CODES_PATH)[_code]["uses_remaining"]
        log("[invite-fail] 500'd invite consumed no use", _uses == 3,
            f"uses_remaining={_uses}")
        log("[invite-fail] exactly-once guard not armed",
            "joined_by" not in _meta, f"meta={_meta}")
        _rows = [json.loads(l)
                 for l in _approver.LOG_PATH.read_text().splitlines()]
        log("[invite-fail] failure audited",
            any(r["type"] == "welcome_invite_failed" and r["status"] == 500
                for r in _rows), f"rows={[r['type'] for r in _rows]}")
        _no_ack = await _wait_for_message(
            _if_token, _room, "you're in", timeout=5)
        log("[invite-fail] no confirmation posted", _no_ack is None,
            f"saw={_no_ack!r}")

        # Phase B: the rejoin retry, a joiner the HS accepts — exactly one
        # use goes, the invite + confirmation actually land.
        _welcome_all2 = _approver._load(_approver.WELCOME_PATH)
        _meta2 = _welcome_all2[_code]
        await _approver.process_welcome_join(
            _code, _meta2, _if_mxid, _lobby_mxid, _welcome_all2)
        _uses2 = _approver._load(_approver.CODES_PATH)[_code]["uses_remaining"]
        log("[invite-fail] retry consumed exactly one use", _uses2 == 2,
            f"uses_remaining={_uses2}")
        log("[invite-fail] retry recorded the joiner",
            _meta2.get("joined_by") == _if_mxid, f"meta={_meta2}")
        _ack = await _wait_for_message(
            _if_token, _room, "Element and you're in", timeout=15)
        log("[invite-fail] retry posted the confirmation", bool(_ack),
            f"ack={_ack!r}")
        _invited = _wait_for_invite(
            _if_token, lambda rid: rid.split(":")[0] == SPACE_ID.split(":")[0],
            timeout=15)
        log("[invite-fail] retry delivered the space invite", bool(_invited),
            f"room={_invited}")

    failed = [name for name, ok in results if not ok]
    print(f"\n=== {len(results) - len(failed)}/{len(results)} pass ===")
    if failed:
        print("FAILED: " + ", ".join(failed), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
