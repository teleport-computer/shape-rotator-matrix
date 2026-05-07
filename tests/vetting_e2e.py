"""End-to-end test of the per-knock vetting flow + an actual E2EE round-trip.

What it asserts:
  1. A fresh user can knock with a valid code and gets invited to a NEW room
     that is NOT the space (the per-knock vetting room).
  2. The bot's welcome message in the vetting room contains a `keyword`.
  3. Replying with a valid 3-line haiku containing that keyword causes the
     approver to invite the user to the space.
  4. The user accepts and auto-joins the E2EE child room (`#bot-noise`).
  5. A SECOND fresh user joins via the same flow and ends up in the same
     E2EE room.
  6. User #1 sends an encrypted message in the room; user #2's OlmMachine
     decrypts it. This is the actual E2EE assertion — a megolm round-trip
     between two independently-onboarded users that proves the flow doesn't
     wedge crypto.

Run inside the test-runner container — `tests/run_e2e.sh` is the entry
point, and that's what CI calls.

Env (all pre-set by run_in_runner.sh):
  DEV_HS              homeserver URL
  DEV_REG_TOKEN       continuwuity registration token
  DEV_KNOCK_CODE      a knock code with >= 2 uses
  SPACE_ID            unsuffixed space room id
  SPACE_CHILD_IDS     comma-separated child room IDs (general, announcements, bot-noise)
"""
import asyncio, json, os, re, secrets, sys, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))

from sas_e2e import make_client, sync_once, register

from mautrix.types import (EventType, MessageType, TextMessageEventContent,
                           UserID)

HS              = os.environ.get("DEV_HS", "http://landing:80").rstrip("/")
REG_TOKEN       = os.environ["DEV_REG_TOKEN"]
KNOCK_CODE      = os.environ["DEV_KNOCK_CODE"]
SPACE_ID        = os.environ["SPACE_ID"]
SPACE_CHILD_IDS = [c.strip() for c in os.environ["SPACE_CHILD_IDS"].split(",") if c.strip()]
# bot-noise is the canonical E2EE child room (third in the bootstrap order).
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


async def onboard_via_vetting(label):
    """Register, set displayname, knock, complete vetting, end up in space."""
    username = f"e2e_{label}_{int(time.time())}_{secrets.token_hex(2)}"
    device   = f"E2E{label.upper()}{secrets.token_hex(2)}"
    mxid, token = register(username, secrets.token_urlsafe(32), device)
    print(f"[{label}] registered {mxid} device={device}", flush=True)

    http("PUT",
         f"/_matrix/client/v3/profile/{urllib.parse.quote(mxid)}/displayname",
         token=token, body={"displayname": f"e2e-{label}"})

    s, _ = http("POST",
                f"/_matrix/client/v3/knock/{urllib.parse.quote(SPACE_ID)}",
                token=token, body={"reason": KNOCK_CODE})
    log(f"[{label}] knock posted", s == 200, f"status={s}")

    space_prefix = SPACE_ID.split(":")[0]
    deadline = time.time() + 60
    vetting_room = None
    while time.time() < deadline:
        _s, sync = http("GET", "/_matrix/client/v3/sync?timeout=0", token=token)
        for rid in sync.get("rooms", {}).get("invite", {}).keys():
            if rid.split(":")[0] != space_prefix:
                vetting_room = rid
                break
        if vetting_room:
            break
        await asyncio.sleep(1)
    log(f"[{label}] vetting room invite arrived", bool(vetting_room),
        f"room={vetting_room}")
    if not vetting_room:
        return None

    s, _ = http("POST",
                f"/_matrix/client/v3/join/{urllib.parse.quote(vetting_room)}",
                token=token, body={})
    log(f"[{label}] joined vetting room", s == 200, f"status={s}")

    # Pull the captcha challenge — bot may need a beat after our join.
    keyword = None
    deadline = time.time() + 30
    while time.time() < deadline:
        _s, sync = http("GET", "/_matrix/client/v3/sync?timeout=0", token=token)
        joined = sync.get("rooms", {}).get("join", {}).get(vetting_room, {})
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
    log(f"[{label}] captcha keyword visible", bool(keyword), f"keyword={keyword!r}")
    if not keyword:
        return None

    haiku = (f"silent {keyword} hum\n"
             f"floating in the morning fog\n"
             f"spring wind blowing through")
    s, _ = http(
        "PUT",
        f"/_matrix/client/v3/rooms/{urllib.parse.quote(vetting_room)}"
        f"/send/m.room.message/e2e-haiku-{label}-{int(time.time())}",
        token=token, body={"msgtype": "m.text", "body": haiku})
    log(f"[{label}] haiku sent", s == 200, f"status={s}")

    # Wait for the actual space invite. Also collect any vetting-room
    # messages so we can assert the bot posts a signup-code URL on success.
    deadline = time.time() + 60
    got_space = False
    signup_url = None
    saw_doc = False
    while time.time() < deadline:
        _s, sync = http("GET", "/_matrix/client/v3/sync?timeout=0", token=token)
        if any(rid.split(":")[0] == space_prefix
               for rid in sync.get("rooms", {}).get("invite", {}).keys()):
            got_space = True
        for ev in (sync.get("rooms", {}).get("join", {})
                   .get(vetting_room, {}).get("timeline", {})
                   .get("events", [])):
            if ev.get("type") != "m.room.message":
                continue
            body = (ev.get("content") or {}).get("body", "")
            m = re.search(r"https?://\S+/signup\?code=\S+", body)
            if m:
                signup_url = m.group(0)
            if "onboarding doc:" in body.lower():
                saw_doc = True
        if got_space and signup_url and saw_doc:
            break
        await asyncio.sleep(1)
    log(f"[{label}] space invite after vetting", got_space)
    log(f"[{label}] welcome signup-code url posted in ack",
        bool(signup_url), f"url={signup_url}")
    log(f"[{label}] onboarding doc url posted in ack", saw_doc)
    if not got_space:
        return None

    s, _ = http("POST",
                f"/_matrix/client/v3/rooms/{urllib.parse.quote(SPACE_ID)}/join",
                token=token, body={})
    log(f"[{label}] accepted space invite", s == 200, f"status={s}")

    # Restricted child rooms — user can self-join now.
    for child in SPACE_CHILD_IDS:
        http("POST",
             f"/_matrix/client/v3/rooms/{urllib.parse.quote(child)}/join",
             token=token, body={})

    return mxid, token, device


async def main():
    if not SPACE_CHILD_IDS:
        print("no SPACE_CHILD_IDS — cannot run E2EE round-trip portion", file=sys.stderr)
        sys.exit(2)

    # Onboard two independent users via the vetting flow.
    a = await onboard_via_vetting("alice")
    b = await onboard_via_vetting("bob")
    if not a or not b:
        print("onboarding failed; skipping E2EE round-trip")
        sys.exit(1)
    a_mxid, a_token, a_device = a
    b_mxid, b_token, b_device = b

    # Spin up two real mautrix clients with OlmMachine + crypto store.
    a_client, a_cs, a_ss, a_db = await make_client(
        a_mxid, a_token, a_device, db_path=f"/tmp/{secrets.token_hex(4)}_a.db")
    b_client, b_cs, b_ss, b_db = await make_client(
        b_mxid, b_token, b_device, db_path=f"/tmp/{secrets.token_hex(4)}_b.db")
    await a_client.crypto.share_keys()
    await b_client.crypto.share_keys()

    # Initial syncs so each one sees they're in the encrypted child room and
    # the other's device keys land in their crypto store.
    for _ in range(3):
        await sync_once(a_client, a_ss, timeout=2000, first=True)
        await sync_once(b_client, b_ss, timeout=2000, first=True)

    # Confirm the room is actually encrypted from each side.
    a_enc = await a_ss.is_encrypted(ENC_ROOM)
    b_enc = await b_ss.is_encrypted(ENC_ROOM)
    log("E2EE child room reports encrypted (alice side)", bool(a_enc))
    log("E2EE child room reports encrypted (bob side)",   bool(b_enc))

    # Alice sends an encrypted message; Bob decrypts.
    secret = f"vetting-e2e secret {secrets.token_hex(8)}"
    event_id = await a_client.send_message_event(
        ENC_ROOM, EventType.ROOM_MESSAGE,
        TextMessageEventContent(msgtype=MessageType.TEXT, body=secret))
    log("alice sent encrypted message", bool(event_id),
        f"event_id={event_id}")

    decrypted_body = None
    deadline = time.time() + 60
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

    failed = [name for name, ok in results if not ok]
    print(f"\n=== {len(results) - len(failed)}/{len(results)} pass ===")
    if failed:
        print("FAILED: " + ", ".join(failed), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
