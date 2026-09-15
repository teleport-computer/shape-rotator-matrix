"""Acceptance test for issue #79 — retention filtering of the MSC4268
bundle (epic #76 chip 3).

Self-contained against the dev continuwuity stack. Imports the approver
module and drives its production paths directly:

  - the retention room factory (_create_retention_room, chip 2) makes the
    90d room and its write-once policy record;
  - the production session-age hook (iter_encrypted_events +
    record_session, chip 1) indexes every inbound session as the bot syncs;
  - build_room_key_bundle (the function this issue changes) filters by the
    immutable RETENTION_PATH record, never the mutable m.room.retention
    state (#78);
  - _send_room_key_bundle delivers through the same funnel every invite
    path uses, and the invitee runs the production responder handler.

The age clock: the CS API cannot backdate an event's origin_server_ts, so
"message at T-100d" is produced by seeding the session-age index for that
session via the same production record_session() the sync hook calls.
The index IS the clock chip 3 reads — the crypto store carries no
timestamps (that absence is why chip 1 exists).

Acceptance criteria (from the issue body):
  1. Retention room with a 90d policy; messages at T-100d and T-1d.
  2. New member is invited in and receives the bundle (production funnel).
  3. Recent message decrypts.
  4. Old message does NOT decrypt — SessionNotFound (missing/withheld
     session), not a delivery failure or server error.
  5. Control room with no m.room.retention record: both messages decrypt.
  6. history_bundle_e2e.py / history_bundle_responder_e2e.py keep passing
     (run unmodified by tests/run_in_runner.sh).

Run:
  python3 tests/retention_bundle_e2e.py
(env: DEV_HS, DEV_REG_TOKEN; defaults target the local dev stack.)

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
from mautrix.crypto import attachments  # noqa: E402
from mautrix.errors import SessionNotFound  # noqa: E402
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


def raw_event(room_id, event_id, token):
    chunk = get_json(
        f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}"
        f"/messages?dir=b&limit=100", token).get("chunk", [])
    return next((e for e in chunk if e.get("event_id") == event_id), None)


async def download_bundle_plaintext(bundle, token):
    """Download + decrypt a build_room_key_bundle() result (the attachment
    the invitee's responder decrypts) into the exported JSON dict."""
    uri = bundle["url"].split("://", 1)[1]
    server, media_id = uri.split("/", 1)
    ciphertext = urllib.request.urlopen(urllib.request.Request(
        f"{HS}/_matrix/media/v3/download/{server}/{media_id}",
        headers={"Authorization": f"Bearer {token}"}), timeout=15).read()
    info = bundle["file"]
    return json.loads(attachments.decrypt_attachment(
        ciphertext, info["key"]["k"], info["hashes"]["sha256"], info["iv"]))


async def bot_sync_and_record(bot, bot_ss, first=False, timeout=5000):
    """One bot /sync cycle that ALSO runs the production index hook:
    iterate m.room.encrypted events off the raw sync dict and call
    approver.record_session — the exact code path sync_loop runs."""
    since = None if first else await bot.sync_store.get_next_batch()
    data = await bot.sync(since=since, timeout=timeout, full_state=first)
    if not isinstance(data, dict):
        return None
    nb = data.get("next_batch")
    if nb:
        await bot.sync_store.put_next_batch(nb)
    for rid, sid, its in approver.iter_encrypted_events(data.get("rooms", {})):
        approver.record_session(rid, sid, its)
    bot_ss._joined.clear()
    bot_ss._joined.update(data.get("rooms", {}).get("join", {}).keys())
    tasks = bot.handle_sync(data)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    return data


async def send_encrypted(alice, room_id, body):
    """Send an encrypted message, return its event_id. Callers rotate
    alice's outbound session BEFORE this when a fresh session_id is
    required (mautrix mints a new one after EncryptionError -> re-share)."""
    return str(await alice.send_message_event(
        room_id, EventType.ROOM_MESSAGE,
        TextMessageEventContent(msgtype=MessageType.TEXT, body=body)))


async def main():
    suffix = f"{int(time.time())}_{secrets.token_hex(2)}"
    server_name = HS.split("//", 1)[1].rstrip("/")

    alice_mxid, alice_tok = register(f"ret_alice_{suffix}",
                                     secrets.token_urlsafe(24), f"RAL{secrets.token_hex(2)}")
    bot_mxid, bot_tok = register(f"ret_bot_{suffix}",
                                 secrets.token_urlsafe(24), f"RBOT{secrets.token_hex(2)}")
    bob_mxid, bob_tok = register(f"ret_bob_{suffix}",
                                 secrets.token_urlsafe(24), f"RBOB{secrets.token_hex(2)}")
    bot_device = get_json("/_matrix/client/v3/account/whoami", bot_tok)["device_id"]
    print(f"[retention_bundle_e2e] alice={alice_mxid} bot={bot_mxid} bob={bob_mxid}",
          flush=True)

    # --- bot creates a space (retention rooms are space children) ----------
    s, r = _post(f"{HS}/_matrix/client/v3/createRoom", {
        "name": "retention bundle test space",
        "preset": "public_chat",
        "visibility": "private",
        "creation_content": {"type": "m.space"},
        "power_level_content_override": {"users": {bot_mxid: 100}},
    }, token=bot_tok)
    assert s == 200, f"create space: {s} {r}"
    space_id = r["room_id"]

    # --- wire the approver module to the test bot ---------------------------
    tmp = Path(f"/tmp/retention_bundle_{suffix}")
    tmp.mkdir(parents=True, exist_ok=True)
    os.environ.update(HS=HS, SPACE_ID=space_id, MATRIX_TOKEN=bot_tok)
    approver.HS = HS
    approver.TOKEN = bot_tok
    approver.AUTH = {"Authorization": f"Bearer {bot_tok}"}
    approver.OUR_MXID = bot_mxid
    approver.SERVER_NAME = server_name
    approver.SPACE_ID = space_id
    approver.RETENTION_PATH = tmp / "retention_rooms.json"
    approver.SESSION_INDEX_PATH = tmp / "session_age_index.json"
    approver.LOG_PATH = tmp / "log.jsonl"

    # --- criterion 1: retention room, 90d policy, old + recent messages ----
    bot, bot_cs, bot_ss, bot_db = await make_client(
        bot_mxid, bot_tok, bot_device, tmp / "bot.db")
    await bot.crypto.share_keys()
    await bot_sync_and_record(bot, bot_ss, first=True)

    WINDOW_S = 90 * 86400
    rec = await approver._create_retention_room("retention-e2e", WINDOW_S)
    room_id = rec["room_id"]
    log("1a. retention room created with a 90d policy record",
        rec.get("window_seconds") == WINDOW_S
        and approver._load_retention()[room_id]["max_lifetime_ms"] == WINDOW_S * 1000,
        f"room={room_id} window={rec.get('window_seconds')}s")

    s, _ = _post(f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/invite",
                 {"user_id": alice_mxid}, token=bot_tok)
    assert s == 200, f"invite alice: {s}"
    s, _ = _post(f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/join",
                 {}, token=alice_tok)
    assert s == 200, f"alice join: {s}"

    alice_device = get_json("/_matrix/client/v3/account/whoami", alice_tok)["device_id"]
    alice, alice_cs, alice_ss, alice_db = await make_client(
        alice_mxid, alice_tok, alice_device, tmp / "alice.db")
    await alice.crypto.share_keys()
    await sync_once(alice, alice_ss, first=True)

    old_body = f"message at T-100d {secrets.token_hex(4)}"
    ev_old = await send_encrypted(alice, room_id, old_body)
    await bot_sync_and_record(bot, bot_ss)

    await alice.crypto.crypto_store.remove_outbound_group_sessions([room_id])
    recent_body = f"message at T-1d {secrets.token_hex(4)}"
    ev_recent = await send_encrypted(alice, room_id, recent_body)
    await bot_sync_and_record(bot, bot_ss)

    raw_old = raw_event(room_id, ev_old, bot_tok)
    raw_recent = raw_event(room_id, ev_recent, bot_tok)
    sid_old = raw_old["content"]["session_id"]
    sid_recent = raw_recent["content"]["session_id"]
    log("1b. old + recent messages live in distinct megolm sessions",
        sid_old != sid_recent, f"{sid_old[:8]} vs {sid_recent[:8]}")

    # Backdate the old session's index entry to T-100d via the production
    # write path (record_session keeps the min). The index is chip 3's clock.
    now_ms = int(time.time() * 1000)
    ts_old = now_ms - 100 * 86400 * 1000
    approver.record_session(room_id, sid_old, ts_old)
    idx = approver.session_age_index(room_id)
    cutoff_ms = now_ms - WINDOW_S * 1000
    log("1c. index says old session predates the window, recent does not",
        idx.get(sid_old) == ts_old and idx.get(sid_recent, 0) >= cutoff_ms,
        f"old_delta={(now_ms - idx.get(sid_old, now_ms)) / 86400000:.0f}d "
        f"recent_delta={(now_ms - idx.get(sid_recent, now_ms)) / 86400000:.0f}d")

    # --- the bundle: old session withheld with a retention reason ----------
    approver._ROOM_KEY_BUNDLE_STORE = bot_cs
    approver._ROOM_KEY_BUNDLE_CLIENT = bot

    inbound_before = await inbound_session_ids(bot_cs, room_id)

    bundle = await approver.build_room_key_bundle(room_id)
    exported = await download_bundle_plaintext(bundle, bot_tok)
    keys_ids = {k["session_id"] for k in exported["room_keys"]}
    with_by_sid = {w["session_id"]: w for w in exported["withheld"]}
    log("2a. bundle omits the expired session from room_keys",
        sid_old not in keys_ids and sid_recent in keys_ids,
        f"room_keys={sorted(s[:8] for s in keys_ids)}")
    log("2b. bundle reports the expired session in withheld",
        sid_old in with_by_sid, f"withheld={sorted(s[:8] for s in with_by_sid)}")
    w = with_by_sid.get(sid_old, {})
    log("2c. withheld code is m.unauthorised (bot holds it; policy denies it)",
        w.get("code") == "m.unauthorised", f"code={w.get('code')!r}")
    log("2d. withheld reason is retention-specific + names the window",
        "retention" in (w.get("reason") or "")
        and approver._render_window(WINDOW_S) in (w.get("reason") or ""),
        f"reason={w.get('reason')!r}")
    both = keys_ids & set(with_by_sid)
    log("2e. no session appears in both sections (MSC4268 MUST NOT)",
        not both, f"overlap={sorted(s[:8] for s in both)}")

    # --- escrow/durability untouched: bundle build pruned nothing ----------
    inbound_after = await inbound_session_ids(bot_cs, room_id)
    log("3a. crypto store still holds every inbound session after the build",
        inbound_before == inbound_after,
        f"{len(inbound_after)} session(s)")
    still = await bot.crypto.decrypt_megolm_event(Event.deserialize(raw_old))
    log("3b. bot itself still decrypts the old message after the build",
        still.content.body == old_body, f"body={still.content.body!r}")

    # --- criteria 2-4: new member receives the bundle via the funnel -------
    bob_device = get_json("/_matrix/client/v3/account/whoami", bob_tok)["device_id"]
    os.environ.update(MXID=bob_mxid, TOKEN=bob_tok, DEVICE=bob_device)
    sys.path.insert(0, str(REPO / "landing"))
    from responder import _StateStore as _RStateStore, register_room_key_bundle_handler
    from responder import sync_once as responder_sync_once
    bob, bob_cs, bob_state, bob_db = await make_client(
        bob_mxid, bob_tok, bob_device, tmp / "bob.db")
    await bob.crypto.share_keys()
    bob_ss = _RStateStore(bob.state_store)
    register_room_key_bundle_handler(bob, bob_cs, bob_ss)
    await responder_sync_once(bob, bob_ss, first=True)

    s, _ = _post(f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/invite",
                 {"user_id": bob_mxid}, token=bot_tok)
    assert s == 200, f"invite bob: {s}"
    delivered = await approver._send_room_key_bundle(bob_mxid, room_id)
    log("2f. bundle delivered through the production funnel (_send_room_key_bundle)",
        delivered)

    for _ in range(6):
        await responder_sync_once(bob, bob_ss)
        if room_id in bob_ss._joined:
            break
    log("2g. bob joined the room after the invite", room_id in bob_ss._joined)

    bob_raw_recent = raw_event(room_id, ev_recent, bob_tok)
    bob_recent = await bob.crypto.decrypt_megolm_event(
        Event.deserialize(bob_raw_recent))
    log("3c. RECENT message decrypts for the new member",
        bob_recent.content.body == recent_body, f"body={bob_recent.content.body!r}")

    bob_raw_old = raw_event(room_id, ev_old, bob_tok)
    try:
        await bob.crypto.decrypt_megolm_event(Event.deserialize(bob_raw_old))
        ok, detail = False, "old message DECRYPTED — retention filter failed"
    except SessionNotFound:
        ok, detail = True, "SessionNotFound (missing/withheld session)"
    except Exception as e:
        ok, detail = False, f"wrong failure mode: {type(e).__name__}: {e}"
    log("4. OLD message does not decrypt for the new member (missing session)",
        ok, detail)

    # --- criterion 5: control room with no retention record ----------------
    s, r = _post(f"{HS}/_matrix/client/v3/createRoom", {
        "name": "control room (no retention)",
        "preset": "private_chat",
        "initial_state": [
            {"type": "m.room.history_visibility", "state_key": "",
             "content": {"history_visibility": "shared"}},
            {"type": "m.room.encryption", "state_key": "",
             "content": {"algorithm": "m.megolm.v1.aes-sha2"}},
        ],
    }, token=bot_tok)
    assert s == 200, f"control createRoom: {s} {r}"
    control_id = r["room_id"]
    s, _ = _post(f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(control_id)}/invite",
                 {"user_id": alice_mxid}, token=bot_tok)
    assert s == 200, f"invite alice (control): {s}"
    s, _ = _post(f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(control_id)}/join",
                 {}, token=alice_tok)
    assert s == 200, f"alice join (control): {s}"
    await sync_once(alice, alice_ss)

    c1_body = f"control first {secrets.token_hex(4)}"
    c1_ev = await send_encrypted(alice, control_id, c1_body)
    await bot_sync_and_record(bot, bot_ss)
    await alice.crypto.crypto_store.remove_outbound_group_sessions([control_id])
    c2_body = f"control second {secrets.token_hex(4)}"
    c2_ev = await send_encrypted(alice, control_id, c2_body)
    await bot_sync_and_record(bot, bot_ss)

    log("5a. control room has NO m.room.retention and no policy record",
        "m.room.retention" not in {
            e["type"] for e in get_json(
                f"/_matrix/client/v3/rooms/{urllib.parse.quote(control_id)}/state",
                bot_tok)}
        and control_id not in approver._load_retention())

    control_bundle = await approver.build_room_key_bundle(control_id)
    control_exported = await download_bundle_plaintext(control_bundle, bot_tok)
    c_ids = {k["session_id"] for k in control_exported["room_keys"]}
    log("5b. control bundle keeps every session, withheld list empty",
        len(c_ids) >= 2 and not control_exported["withheld"],
        f"room_keys={len(c_ids)} withheld={len(control_exported['withheld'])}")

    s, _ = _post(f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(control_id)}/invite",
                 {"user_id": bob_mxid}, token=bot_tok)
    assert s == 200, f"invite bob (control): {s}"
    delivered = await approver._send_room_key_bundle(bob_mxid, control_id)
    log("5c. control bundle delivered through the same funnel", delivered)

    for _ in range(6):
        await responder_sync_once(bob, bob_ss)
        if control_id in bob_ss._joined:
            break
    for name, ev, body in (("5d. control: FIRST message decrypts", c1_ev, c1_body),
                           ("5e. control: SECOND message decrypts", c2_ev, c2_body)):
        raw = raw_event(control_id, ev, bob_tok)
        decrypted = await bob.crypto.decrypt_megolm_event(Event.deserialize(raw))
        log(name, decrypted.content.body == body, f"body={decrypted.content.body!r}")

    for db in (alice_db, bot_db, bob_db):
        await db.stop()
    for client in (alice, bot, bob):
        try:
            await client.api.session.close()
        except Exception:
            pass

    failed = [n for n, ok in results if not ok]
    print(f"\n[retention_bundle_e2e] {len(results) - len(failed)}/{len(results)} "
          f"checks passed", flush=True)
    sys.exit(1 if failed else 0)


async def inbound_session_ids(store, room_id):
    rows = await store.db.fetch(
        "SELECT session_id FROM crypto_megolm_inbound_session "
        "WHERE room_id=$1 AND account_id=$2 AND withheld_code IS NULL",
        room_id, store.account_id)
    return {str(row["session_id"]) for row in rows}


asyncio.run(main())
