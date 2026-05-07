"""End-to-end test of the per-knock lobby flow + an actual E2EE round-trip.

What it asserts:
  1. POST /join/api with a valid code returns a fresh public lobby room URL.
  2. A fresh user can directly /join the returned room (no knock UI involved).
  3. The bot's welcome message in that lobby contains a `keyword`.
  4. Replying with a valid 3-line haiku containing that keyword causes the
     approver to invite the user to the space.
  5. The user accepts and auto-joins the E2EE child room (`#bot-noise`).
  6. A SECOND fresh user, going through their own /join/api → lobby flow,
     ends up in the same E2EE child room.
  7. User #1 sends an encrypted message in #bot-noise; user #2's OlmMachine
     decrypts it. This is the actual E2EE assertion — a megolm round-trip
     between two independently-onboarded users that proves the lobby flow
     doesn't wedge crypto.

Env (all pre-set by run_in_runner.sh):
  DEV_HS              homeserver URL (landing nginx)
  DEV_REG_TOKEN       continuwuity registration token
  DEV_KNOCK_CODE      a code with >= 2 uses (lobby reuses the same codes table)
  SPACE_ID            unsuffixed space room id
  SPACE_CHILD_IDS     comma-separated child room IDs
"""
import asyncio, json, os, re, secrets, sys, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))

from sas_e2e import make_client, sync_once, register

from mautrix.types import (EventType, MessageType, TextMessageEventContent)

HS              = os.environ.get("DEV_HS", "http://landing:80").rstrip("/")
REG_TOKEN       = os.environ["DEV_REG_TOKEN"]
KNOCK_CODE      = os.environ["DEV_KNOCK_CODE"]
SPACE_ID        = os.environ["SPACE_ID"]
SPACE_CHILD_IDS = [c.strip() for c in os.environ["SPACE_CHILD_IDS"].split(",") if c.strip()]
ENC_ROOM = SPACE_CHILD_IDS[-1] if SPACE_CHILD_IDS else None

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
        try:    return e.code, json.loads(e.read() or b"{}")
        except: return e.code, {}


async def onboard_via_lobby(label):
    """Register, set displayname, mint a lobby room via /join/api, join it,
    answer the haiku, end up in the space."""
    username = f"e2e_lobby_{label}_{int(time.time())}_{secrets.token_hex(2)}"
    device   = f"E2EL{label.upper()}{secrets.token_hex(2)}"
    mxid, token = register(username, secrets.token_urlsafe(32), device)
    print(f"[{label}] registered {mxid} device={device}", flush=True)

    http("PUT",
         f"/_matrix/client/v3/profile/{urllib.parse.quote(mxid)}/displayname",
         token=token, body={"displayname": f"e2e-lobby-{label}"})

    # /join/api is unauthenticated — anyone holding the code can mint a lobby.
    s, j = http("POST", "/join/api", body={"code": KNOCK_CODE})
    log(f"[{label}] /join/api returned 200", s == 200, f"status={s} body={j}")
    if s != 200:
        return None
    lobby_room = j.get("room_id")
    log(f"[{label}] /join/api returned room_id+url",
        bool(lobby_room and j.get("url") and j.get("alias")),
        f"room={lobby_room} alias={j.get('alias')}")
    if not lobby_room:
        return None

    # Public room → user joins directly via the alias.
    alias = j["alias"]
    s, _ = http("POST",
                f"/_matrix/client/v3/join/{urllib.parse.quote(alias)}",
                token=token, body={})
    log(f"[{label}] joined lobby room via alias", s == 200,
        f"status={s} alias={alias}")
    if s != 200:
        return None

    # Pull the captcha challenge — bot needs to /sync, see our join, and post.
    # Long-poll with a since token so we wait for the new message rather than
    # busy-polling the same initial-sync slice.
    keyword = None
    since = None
    deadline = time.time() + 45
    while time.time() < deadline:
        url = "/_matrix/client/v3/sync?timeout=10000"
        if since:
            url += f"&since={urllib.parse.quote(since)}"
        _s, sync = http("GET", url, token=token, timeout=15)
        since = sync.get("next_batch") or since
        joined = sync.get("rooms", {}).get("join", {}).get(lobby_room, {})
        for ev in joined.get("timeline", {}).get("events", []):
            if ev.get("type") != "m.room.message":
                continue
            body = (ev.get("content") or {}).get("body", "")
            m = re.search(r'include the word "([^"]+)"', body)
            if m:
                keyword = m.group(1)
                break
        if keyword:
            break
        await asyncio.sleep(1)
    log(f"[{label}] captcha keyword visible in lobby", bool(keyword),
        f"keyword={keyword!r}")
    if not keyword:
        return None

    haiku = (f"silent {keyword} hum\n"
             f"floating in the morning fog\n"
             f"spring wind blowing through")
    s, _ = http(
        "PUT",
        f"/_matrix/client/v3/rooms/{urllib.parse.quote(lobby_room)}"
        f"/send/m.room.message/e2e-lobby-haiku-{label}-{int(time.time())}",
        token=token, body={"msgtype": "m.text", "body": haiku})
    log(f"[{label}] haiku sent", s == 200, f"status={s}")

    # Wait for the actual space invite. Also collect any new lobby-room
    # messages so we can assert the bot posts a signup-code URL on success.
    space_prefix = SPACE_ID.split(":")[0]
    deadline = time.time() + 30
    got_space = False
    signup_url = None
    while time.time() < deadline:
        _s, sync = http("GET", "/_matrix/client/v3/sync?timeout=0", token=token)
        if any(rid.split(":")[0] == space_prefix
               for rid in sync.get("rooms", {}).get("invite", {}).keys()):
            got_space = True
        for ev in (sync.get("rooms", {}).get("join", {})
                   .get(lobby_room, {}).get("timeline", {})
                   .get("events", [])):
            if ev.get("type") != "m.room.message":
                continue
            body = (ev.get("content") or {}).get("body", "")
            m = re.search(r"https?://\S+/signup\?code=\S+", body)
            if m:
                signup_url = m.group(0)
        if got_space and signup_url:
            break
        await asyncio.sleep(1)
    log(f"[{label}] space invite after lobby", got_space)
    log(f"[{label}] welcome signup-code url posted in ack",
        bool(signup_url), f"url={signup_url}")
    if not got_space:
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

    a = await onboard_via_lobby("alice")
    b = await onboard_via_lobby("bob")
    if not a or not b:
        print("onboarding failed; skipping E2EE round-trip")
        sys.exit(1)
    a_mxid, a_token, a_device = a
    b_mxid, b_token, b_device = b

    a_client, a_cs, a_ss, a_db = await make_client(
        a_mxid, a_token, a_device, db_path=f"/tmp/{secrets.token_hex(4)}_la.db")
    b_client, b_cs, b_ss, b_db = await make_client(
        b_mxid, b_token, b_device, db_path=f"/tmp/{secrets.token_hex(4)}_lb.db")
    await a_client.crypto.share_keys()
    await b_client.crypto.share_keys()

    for _ in range(3):
        await sync_once(a_client, a_ss, timeout=2000, first=True)
        await sync_once(b_client, b_ss, timeout=2000, first=True)

    a_enc = await a_ss.is_encrypted(ENC_ROOM)
    b_enc = await b_ss.is_encrypted(ENC_ROOM)
    log("E2EE child room reports encrypted (alice side)", bool(a_enc))
    log("E2EE child room reports encrypted (bob side)",   bool(b_enc))

    secret = f"lobby-e2e secret {secrets.token_hex(8)}"
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

    # Second-pass debug case: alice (now an existing space member) re-runs
    # the lobby flow. The bot should still post the haiku, accept the answer,
    # and acknowledge with the "already in space" success ack — proving the
    # invite-403-because-already-member path is treated as success so an
    # operator can self-test the whole flow without spinning up new accounts.
    s, j = http("POST", "/join/api", body={"code": KNOCK_CODE})
    log("[alice-redo] /join/api returned 200 for existing member",
        s == 200, f"status={s}")
    if s == 200:
        redo_room = j["room_id"]
        s2, _ = http("POST",
                     f"/_matrix/client/v3/join/{urllib.parse.quote(j['alias'])}",
                     token=a_token, body={})
        log("[alice-redo] joined fresh lobby as existing member",
            s2 == 200, f"status={s2}")

        keyword = None
        since = None
        deadline = time.time() + 45
        while time.time() < deadline:
            url = "/_matrix/client/v3/sync?timeout=10000"
            if since:
                url += f"&since={urllib.parse.quote(since)}"
            _s, sync = http("GET", url, token=a_token, timeout=15)
            since = sync.get("next_batch") or since
            joined = sync.get("rooms", {}).get("join", {}).get(redo_room, {})
            for ev in joined.get("timeline", {}).get("events", []):
                if ev.get("type") != "m.room.message":
                    continue
                body_text = (ev.get("content") or {}).get("body", "")
                m = re.search(r'include the word "([^"]+)"', body_text)
                if m:
                    keyword = m.group(1)
                    break
            if keyword:
                break
            await asyncio.sleep(1)
        log("[alice-redo] captcha keyword visible", bool(keyword),
            f"keyword={keyword!r}")

        if keyword:
            haiku = (f"silent {keyword} hum\nfloating in the morning fog\n"
                     f"spring wind blowing through")
            http("PUT",
                 f"/_matrix/client/v3/rooms/{urllib.parse.quote(redo_room)}"
                 f"/send/m.room.message/e2e-redo-{int(time.time())}",
                 token=a_token, body={"msgtype": "m.text", "body": haiku})

            # Wait for the bot's "already in space" success ack.
            ack = None
            deadline = time.time() + 30
            while time.time() < deadline:
                url = "/_matrix/client/v3/sync?timeout=10000"
                if since:
                    url += f"&since={urllib.parse.quote(since)}"
                _s, sync = http("GET", url, token=a_token, timeout=15)
                since = sync.get("next_batch") or since
                joined = sync.get("rooms", {}).get("join", {}).get(redo_room, {})
                for ev in joined.get("timeline", {}).get("events", []):
                    if ev.get("type") != "m.room.message":
                        continue
                    b = (ev.get("content") or {}).get("body", "")
                    if "already in shape rotator" in b.lower():
                        ack = b
                        break
                if ack:
                    break
                await asyncio.sleep(1)
            log("[alice-redo] got 'already in space' ack from bot",
                bool(ack), f"ack={ack!r}")

    failed = [name for name, ok in results if not ok]
    print(f"\n=== {len(results) - len(failed)}/{len(results)} pass ===")
    if failed:
        print("FAILED: " + ", ".join(failed), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
