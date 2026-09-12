"""Issue #62 — MSC4268 bundle delivery wired into an ACTUAL approver.py
invite path, not a hand-built bundle / hand-rolled to-device send.

Scenario:
  1. alice creates an E2EE room (history_visibility=shared) and invites the
     approver's bot identity. The bot joins and receives + decrypts an
     encrypted message from alice — this is the pre-invite history.
  2. Drive the REAL approver._invite_to_children() — the exact function
     the vetted-invite path calls after promoting a user — for a fresh invitee. This performs the real invite
     (_admin_invite), the real bundle send (_send_room_key_bundle), and
     the real endorsement write (record_endorsement) — issue #62's wiring,
     unmodified from what ships in approver.py.
  3. The invitee runs the production responder.py to-device handler
     (chip #63): receives the olm-encrypted m.room_key_bundle, downloads +
     decrypts the attachment, imports the sessions — only because the
     bundle's sender matches the room's recorded inviter.
  4. The invitee joins the room and decrypts alice's PRE-INVITE message.
  5. approver.ENDORSEMENTS_PATH (issue #58/#62 web-of-trust JSONL) contains
     the (endorser, invitee, code, room_id) edge.

Run against the dev stack:
  cd dev && docker compose up -d && python3 bootstrap.py   # activates dev-token
  cd .. && python3 tests/history_bundle_invite_e2e.py

Tier 1: this transcript is the evidence (no user-visible surface).
"""
import asyncio, json, os, secrets, sys, time, urllib.parse, urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# --- Import approver (set the env it reads at module load). ---
os.environ.setdefault("HS", os.environ.get("DEV_HS", "http://localhost:46167"))
os.environ.setdefault("SPACE_ID", "!space:localhost")
os.environ.setdefault("SPACE_CHILD_IDS", "")
os.environ.setdefault("ADMIN_COMMAND_ROOM", "!admin:localhost")
os.environ.setdefault("CONDUWUIT_REGISTRATION_TOKEN",
                      os.environ.get("DEV_REG_TOKEN", "dev-token"))
sys.path.insert(0, str(REPO / "knock-approver"))
sys.path.insert(0, str(REPO / "tests"))

import approver  # noqa: E402  (must be after env setup)
from sas_e2e import HS, _post, make_client, register, sync_once  # noqa: E402
from mautrix.types import Event, EventType, MessageType, TextMessageEventContent  # noqa: E402

results = []
def log(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    results.append((name, ok))


def get_json(path, token):
    req = urllib.request.Request(
        f"{HS}{path}", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"{}")


def raw_message(room_id, event_id, token):
    chunk = get_json(
        f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}"
        f"/messages?dir=b&limit=100", token).get("chunk", [])
    return next((e for e in chunk if e.get("event_id") == event_id), None)


async def main():
    suffix = f"{int(time.time())}_{secrets.token_hex(2)}"

    alice_mxid, alice_tok = register(f"inv_alice_{suffix}",
                                     secrets.token_urlsafe(24), f"AL{secrets.token_hex(2)}")
    bot_mxid, bot_tok = register(f"inv_bot_{suffix}",
                                 secrets.token_urlsafe(24), f"BOT{secrets.token_hex(2)}")
    bot_device = get_json("/_matrix/client/v3/account/whoami", bot_tok)["device_id"]
    print(f"[invite-e2e] alice={alice_mxid} bot={bot_mxid}", flush=True)

    # --- 1. alice creates an E2EE room (shared history) and invites the bot. ---
    status, room = _post(f"{HS}/_matrix/client/v3/createRoom", {
        "preset": "private_chat",
        "invite": [bot_mxid],
        "initial_state": [
            {"type": "m.room.history_visibility", "state_key": "",
             "content": {"history_visibility": "shared"}},
            {"type": "m.room.encryption", "state_key": "",
             "content": {"algorithm": "m.megolm.v1.aes-sha2"}},
        ],
    }, token=alice_tok)
    assert status == 200, (status, room)
    room_id = room["room_id"]
    status, body = _post(
        f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/join",
        {}, token=bot_tok)
    assert status == 200, (status, body)
    print(f"[invite-e2e] room={room_id}", flush=True)

    alice_device = get_json("/_matrix/client/v3/account/whoami", alice_tok)["device_id"]
    alice, alice_cs, alice_ss, alice_db = await make_client(
        alice_mxid, alice_tok, alice_device, f"/tmp/invite_e2e_{suffix}_alice.db")
    bot, bot_cs, bot_ss, bot_db = await make_client(
        bot_mxid, bot_tok, bot_device, f"/tmp/invite_e2e_{suffix}_bot.db")
    await alice.crypto.share_keys()
    await bot.crypto.share_keys()
    await sync_once(alice, alice_ss, first=True)
    await sync_once(bot, bot_ss, first=True)

    # --- 2. alice sends the PRE-INVITE encrypted message. ---
    secret = f"pre-invite history {secrets.token_hex(4)}"
    event_id = str(await alice.send_message_event(
        room_id, EventType.ROOM_MESSAGE,
        TextMessageEventContent(msgtype=MessageType.TEXT, body=secret)))
    for _ in range(4):
        await sync_once(bot, bot_ss)
    bot_raw = raw_message(room_id, event_id, bot_tok)
    bot_decrypted = await bot.crypto.decrypt_megolm_event(Event.deserialize(bot_raw))
    log("bot decrypts alice's pre-invite message (inbound session present)",
        bot_decrypted.content.body == secret)

    # --- 3. wire approver's module globals to this bot/room, then drive the
    #        REAL invite-path function (issue #62's actual wiring). ---
    tmp = Path(os.environ.get("ESCROW_TMP", "/tmp")) / f"invite_e2e_{suffix}"
    tmp.mkdir(parents=True, exist_ok=True)
    approver.HS = HS
    approver.TOKEN = bot_tok
    approver.AUTH = {"Authorization": f"Bearer {bot_tok}"}
    approver.OUR_MXID = bot_mxid
    approver.SPACE_CHILD_IDS = [room_id]
    approver.CODES_PATH = tmp / "codes.json"
    approver.ENDORSEMENTS_PATH = tmp / "endorsements.jsonl"
    approver._save(approver.CODES_PATH, {
        "testcode": {"uses_remaining": 5, "label": "invite-e2e",
                     "minted_by": alice_mxid},
    })
    approver._ROOM_KEY_BUNDLE_STORE = bot_cs
    approver._ROOM_KEY_BUNDLE_CLIENT = bot

    bob_mxid, bob_tok = register(f"inv_bob_{suffix}",
                                 secrets.token_urlsafe(24), f"BB{secrets.token_hex(2)}")
    bob_device = get_json("/_matrix/client/v3/account/whoami", bob_tok)["device_id"]

    # Bob comes online (crypto client, uploaded device keys) BEFORE the
    # invite — exactly like a real responder.py client would be. Import
    # responder.py's inviter-tracking + bundle handler (chip #63), the
    # production invitee-side code this test proves against.
    os.environ.update(HS=HS, MXID=bob_mxid, TOKEN=bob_tok, DEVICE=bob_device)
    sys.path.insert(0, str(REPO / "landing"))
    from responder import _StateStore as _RStateStore, register_room_key_bundle_handler
    from responder import sync_once as responder_sync_once

    bob, bob_cs, bob_state, bob_db = await make_client(
        bob_mxid, bob_tok, bob_device, f"/tmp/invite_e2e_{suffix}_bob.db")
    await bob.crypto.share_keys()
    bob_ss = _RStateStore(bob.state_store)
    register_room_key_bundle_handler(bob, bob_cs, bob_ss)
    await responder_sync_once(bob, bob_ss, first=True)

    endorser = approver._endorser_for_code("testcode", bot_mxid)
    log("endorser resolved from the code's minted_by", endorser == alice_mxid, endorser)

    invited = await approver._invite_to_children(
        bob_mxid, endorser=endorser, code_or_manual="testcode")
    log("approver._invite_to_children invited the child room",
        invited == [room_id], str(invited))

    # --- 4. bob's responder-style client receives + imports the bundle
    #        (chip #63), joins the room, decrypts alice's PRE-INVITE msg. ---
    for _ in range(6):
        await responder_sync_once(bob, bob_ss)
        if room_id in bob_ss._joined:
            break
    log("bob auto-joined the room after the invite", room_id in bob_ss._joined)

    bob_raw = raw_message(room_id, event_id, bob_tok)
    log("bob's client can fetch the pre-invite event", bob_raw is not None)
    bob_decrypted = await bob.crypto.decrypt_megolm_event(Event.deserialize(bob_raw))
    log("bob decrypts alice's PRE-INVITE message via the imported bundle",
        bob_decrypted.content.body == secret,
        f"body={bob_decrypted.content.body!r}")

    # --- 5. endorsement JSONL has the edge (issue #58/#62). ---
    rows = [json.loads(l) for l in approver.ENDORSEMENTS_PATH.read_text().splitlines()
            if l.strip()]
    edge = next((r for r in rows
                if r["invitee"] == bob_mxid and r["room_id"] == room_id), None)
    log("endorsement JSONL contains the (endorser, invitee, code, room_id) edge",
        edge is not None, str(edge))
    if edge:
        log("endorsement edge names alice as endorser (code minted_by) and the right code",
            edge.get("endorser") == alice_mxid and edge.get("code_or_manual") == "testcode",
            str(edge))

    for db in (alice_db, bot_db, bob_db):
        await db.stop()
    for client in (alice, bot, bob):
        try:
            await client.api.session.close()
        except Exception:
            pass

    failed = [n for n, ok in results if not ok]
    print(f"\n[invite-e2e] {len(results) - len(failed)}/{len(results)} checks passed",
          flush=True)
    sys.exit(1 if failed else 0)


asyncio.run(main())
