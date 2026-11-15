"""Issue #93 — live cursor-retry proof against the local dev continuwuity.

Drives the REAL mx.load_config + mx.make_client + mx.sync_once (the exact
code relay.py's _mx_sync wraps) with real megolm traffic, and induces the
issue's hypothesized handler failure — a crypto.db write error — by holding
an exclusive sqlite lock on the store (the handler's write fails with
"database is locked" after mautrix's 5s busy timeout) between two encrypted
messages:

  success   — handlers succeed; sync_once persists next_batch
  failure   — the handler's db write raises; sync_once RAISES and the cursor
              file still holds the previous batch (pre-fix code advanced the
              cursor here and dropped the room key forever)
  recovery  — lock released, batch retried. Olm to-device messages are
              at-most-once per process (the first decrypt ratchets the olm
              session even when persisting it failed), so the in-process
              retry raises again and the real-world recovery is a process
              restart — modeled here by rebuilding the client from the same
              store + held cursor, after which the key share IS stored and
              the previously-lost message decrypts; the cursor advances

The /sync cursor lives in its OWN dir (like relay.py's RELAY_SYNC_CURSOR)
so the induced store failure cannot also break the cursor write.

Run: cd dev && docker compose up -d (register once via the first-boot
token so the configured dev-token works), then (runner image, --network
host): DEV_HS=http://localhost:<port> python3 tests/sync_cursor_live.py
"""
import asyncio
import json
import os
import secrets
import sqlite3
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))

work = Path(tempfile.mkdtemp(prefix="sync-live-"))
creds_path = work / "creds.json"
fixtures_path = work / "fixtures.json"
store_dir = work / "store"
cursor_path = work / "cursor" / "next_batch"

os.environ["MX_CREDS"] = str(creds_path)
os.environ["MX_STORE"] = str(store_dir)
os.environ["MX_FIXTURES"] = str(fixtures_path)

sys.path.insert(0, str(REPO / "bridge"))
import mx  # noqa: E402  (env above must be set before import: paths are module-level)

from sas_e2e import (  # noqa: E402
    HS,
    _post,
    make_client as e2e_make_client,
    register,
    sync_once as e2e_sync_once,
)
from mautrix.types import (  # noqa: E402
    Event,
    EventType,
    MessageType,
    TextMessageEventContent,
)

results = []


def log(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    results.append(ok)


async def decrypt_has(bot, room, needle) -> bool:
    """True if some encrypted room event decrypts to a body containing needle."""
    data = await bot.api.request(
        "GET",
        f"/_matrix/client/v3/rooms/{urllib.parse.quote(room)}/messages",
        query_params={"dir": "b", "limit": "50"},
    )
    for raw in reversed(data.get("chunk", [])):
        if raw.get("type") != "m.room.encrypted":
            continue
        try:
            evt = await bot.crypto.decrypt_megolm_event(Event.deserialize(raw))
        except Exception:
            continue
        if needle in (getattr(evt.content, "body", "") or ""):
            return True
    return False


async def main():
    suffix = f"{int(time.time())}_{secrets.token_hex(2)}"
    bot_dev, alice_dev = f"BOT{secrets.token_hex(2)}", f"ALICE{secrets.token_hex(2)}"
    bot_mxid, bot_tok = register(f"sc_bot_{suffix}", secrets.token_urlsafe(32), bot_dev)
    alice_mxid, alice_tok = register(
        f"sc_alice_{suffix}", secrets.token_urlsafe(32), alice_dev)
    creds_path.write_text(json.dumps({
        "user_id": bot_mxid, "access_token": bot_tok,
        "device_id": bot_dev, "homeserver": HS}))
    print(f"[live] bot={bot_mxid} alice={alice_mxid}", flush=True)

    alice, _alice_cs, alice_ss, alice_db = await e2e_make_client(
        alice_mxid, alice_tok, alice_dev, work / "alice.db")
    await alice.crypto.share_keys()
    await e2e_sync_once(alice, alice_ss, first=True)

    s, r = _post(f"{HS}/_matrix/client/v3/createRoom", {
        "name": "sync cursor live",
        "preset": "private_chat",
        "initial_state": [
            {"type": "m.room.history_visibility", "state_key": "",
             "content": {"history_visibility": "shared"}},
            {"type": "m.room.encryption", "state_key": "",
             "content": {"algorithm": "m.megolm.v1.aes-sha2"}},
        ],
    }, token=alice_tok)
    assert s == 200, f"createRoom: {s} {r}"
    room = r["room_id"]
    fixtures_path.write_text(json.dumps({"matrix_room_id": room}))
    print(f"[live] room={room}", flush=True)

    s, _ = _post(f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room)}/invite",
                 {"user_id": bot_mxid}, token=alice_tok)
    assert s == 200, f"invite: {s}"
    s, _ = _post(f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room)}/join",
                 {}, token=bot_tok)
    assert s == 200, f"join: {s}"
    await e2e_sync_once(alice, alice_ss)  # alice learns the bot's device

    # The bot via the production config + constructor path (relay-style cursor).
    creds, _ = mx.load_config()
    bot = await mx.make_client(creds, sync_cursor_path=cursor_path)
    await bot.crypto.share_keys()
    await mx.sync_once(bot, full_state=True, timeout=5000)

    # --- success path: M1's key share handled; cursor persisted. ---
    m1 = f"live-m1 {secrets.token_hex(4)}"
    await alice.send_message_event(
        room, EventType.ROOM_MESSAGE,
        TextMessageEventContent(msgtype=MessageType.TEXT, body=m1))
    await mx.sync_once(bot, timeout=5000)
    cur1 = cursor_path.read_text()
    log("success: sync_once persisted next_batch", bool(cur1), f"cursor={cur1!r}")
    log("success: M1 decrypts (handler stored the room key)",
        await decrypt_has(bot, room, m1))

    # --- failure path: rotate to a NEW megolm session, break db writes, send M2. ---
    await alice.crypto.crypto_store.remove_outbound_group_sessions([room])
    m2 = f"live-m2 {secrets.token_hex(4)}"
    await alice.send_message_event(
        room, EventType.ROOM_MESSAGE,
        TextMessageEventContent(msgtype=MessageType.TEXT, body=m2))
    # Induce the store-write failure: a second connection holds the write
    # lock, so the handler's session write fails after the 5s busy timeout.
    lock = sqlite3.connect(store_dir / "crypto.db")
    lock.execute("BEGIN EXCLUSIVE")
    raised = None
    try:
        await mx.sync_once(bot, timeout=5000)
    except Exception as exc:
        raised = exc
        print(f"[live] sync_once raised {type(exc).__name__}: {exc}", flush=True)
    log("failure: handler exception surfaced (raised)", raised is not None,
        f"{type(raised).__name__}" if raised else "no exception")
    cur_fail = cursor_path.read_text()
    log("failure: cursor held at previous batch", cur_fail == cur1,
        f"cursor={cur_fail!r} prev={cur1!r}")
    log("failure: M2 not decryptable yet (key lost with the failed batch)",
        not await decrypt_has(bot, room, m2))

    # --- recovery: same batch retried from the held cursor. ---
    lock.rollback()
    lock.close()
    try:
        await mx.sync_once(bot, timeout=5000)
        print("[live] in-process retry succeeded", flush=True)
    except Exception as exc:
        # Expected for olm-class failures: the ratchet already consumed the
        # message. Restart (rebuild from the same store + held cursor) is the
        # recovery path a relay restart takes.
        print(f"[live] in-process retry raises {type(exc).__name__} "
              f"(olm ratchet consumed the message) — restarting the client", flush=True)
        await mx._shutdown(bot)
        bot = await mx.make_client(creds, sync_cursor_path=cursor_path)
    await mx.sync_once(bot, timeout=5000)
    log("recovery: cursor advanced after the retry",
        cursor_path.read_text() not in ("", cur1),
        f"cursor={cursor_path.read_text()!r}")
    log("recovery: retried batch stored the room key — M2 decrypts",
        await decrypt_has(bot, room, m2))

    await alice_db.stop()
    await alice.api.session.close()
    await mx._shutdown(bot)

    failed = results.count(False)
    print(f"\n[live] {len(results) - failed}/{len(results)} checks passed", flush=True)
    sys.exit(1 if failed else 0)


asyncio.run(main())
