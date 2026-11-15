#!/usr/bin/env python3
"""bridge/relay.py — bidirectional Matrix<->Telegram relay (issue #52, #49).

Long-running loop that mirrors plain-text messages between the ONE Matrix room
and the ONE Telegram group named in ~/.teleport-travel/test-fixtures.json.
Built on bridge/mx.py (E2EE Matrix) and bridge/tg.py (Telethon) — both are
expected to already be bootstrapped (`mx.py send` once, so `store/.woke`
exists and Element has shared megolm keys to the device).

Mirroring format (relay-bot style, no puppeting):

    **<sender>:** <text>

v1 scope (per #49): plain text only. Media is replaced with a placeholder;
edits, deletes, threads, and reactions are ignored (not relayed).

Loop prevention — each side ignores messages authored by the relay's OWN
identity on that side:

  - Telegram: messages whose sender is this Telethon account (`get_me`), and
  - Matrix:   messages whose sender is the bridge bot mxid (from creds.json).

A message the relay forwards lands on the far side under the relay's own
identity, so the counter-poller sees it as "self" and drops it — no echo.
Sender-id is the SOLE loop guard; content-prefix matching is intentionally
NOT used (it is fragile against users literally named like the relay and
against edited bodies).

Durable cursors (~/.teleport-travel/relay-state.json) — restart-safe, no
dupes, catch-up after downtime:

  - tg_last_id:      highest Telegram message id already processed.
  - mx_seen:         bounded ring of recently-processed Matrix event_ids
                     (dedup safety net across re-syncs / cursor rewinds).

plus the relay's OWN /sync cursor at
``~/.shape-bridge-bot/store/relay_next_batch``: absent on first start ->
advance to "now" without relaying (skip backlog, same as tg_last_id).

The Matrix /sync cursor lives in its OWN file
(``~/.shape-bridge-bot/store/relay_next_batch``), NOT mx.py's
``store/next_batch`` — so running the ``mx.py`` debug CLI between relay
restarts cannot advance the relay's bookmark and drop events. Both share
``crypto.db`` (the OlmMachine identity); only the cursor is split.

HARD RULE (identical to mx.py / tg.py): only the fixture pair from
test-fixtures.json is ever touched. Production pairing is an operator-only
config change — there is no flag to override either target.

Usage:
    python3 bridge/relay.py            # run until SIGTERM / SIGINT / timeout
    python3 bridge/relay.py --once     # one poll pass on each side, then exit
"""
import argparse
import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

# Sibling modules — bridge/ is sys.path[0] when this file is run as a script.
# Also insert it explicitly so the module imports cleanly under `import
# bridge.relay` (tests / packaging) rather than only as a bare __main__.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mx import (
    STORE_DIR as MX_STORE_DIR,
    _shutdown as mx_shutdown,
    load_config as mx_load_config,
    make_client as mx_make_client,
)
from tg import (
    _sender_name as tg_sender_name,
    load_chat_id as tg_load_chat_id,
    make_client as tg_make_client,
)
from mautrix.types import (
    EncryptedEvent,
    Event,
    EventType,
    MessageType,
    TextMessageEventContent,
    UserID,
)

TELEPORT_DIR = Path(os.environ.get("TELEPORT_DIR", Path.home() / ".teleport-travel"))
STATE_PATH = Path(os.environ.get("RELAY_STATE", TELEPORT_DIR / "relay-state.json"))
# The relay's OWN /sync cursor — independent of mx.py's `store/next_batch` so
# that running the debug CLI between daemon restarts can't advance the relay's
# bookmark and silently drop events. See mx.make_client(sync_cursor_path=...).
RELAY_SYNC_CURSOR = Path(
    os.environ.get("MX_RELAY_CURSOR", str(MX_STORE_DIR / "relay_next_batch"))
)

# /sync long-poll timeout (ms). The server holds the connection this long.
MX_SYNC_TIMEOUT = 30_000
# Telegram poll cadence (s). Matrix pushes via long-poll; TG has to be polled.
TG_POLL_INTERVAL = 3.0
# Bounds for one catch-up pass.
TG_POLL_LIMIT = 200
# How many Matrix event_ids to remember for dedup.
MX_SEEN_LIMIT = 500

# v1 media placeholders (no re-upload yet).
_TG_MEDIA_PLACEHOLDER = {
    "photo": "[image posted in Telegram]",
    "document": "[file posted in Telegram]",
    "video": "[video posted in Telegram]",
    "sticker": "[sticker posted in Telegram]",
    "voice": "[voice message posted in Telegram]",
    "audio": "[audio posted in Telegram]",
    "gif": "[gif posted in Telegram]",
}


def log(side: str, msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {side}: {msg}", flush=True)


class State:
    """Durable relay cursors. Persisted atomically after every relay so a kill
    between messages cannot double-relay."""

    def __init__(self, tg_last_id: int, mx_seen):
        self.tg_last_id = tg_last_id
        self.mx_seen = list(mx_seen)

    @classmethod
    def load(cls) -> "State":
        if STATE_PATH.exists():
            try:
                data = json.loads(STATE_PATH.read_text())
            except (json.JSONDecodeError, TypeError) as e:
                raise SystemExit(f"relay state {STATE_PATH} corrupt: {e}")
            try:
                return cls(
                    tg_last_id=int(data.get("tg_last_id", 0)),
                    mx_seen=list(data.get("mx_seen", [])),
                )
            except (TypeError, ValueError) as e:
                raise SystemExit(f"relay state {STATE_PATH} corrupt: {e}")
        return cls(tg_last_id=0, mx_seen=[])

    def save(self) -> None:
        """Atomic write (tmp + rename) — never a half-written file on crash."""
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "tg_last_id": self.tg_last_id,
                "mx_seen": self.mx_seen[-MX_SEEN_LIMIT:],
            }
        )
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(payload)
        os.replace(tmp, STATE_PATH)

    def mark_mx(self, event_id: str) -> bool:
        """Record that we've processed `event_id`. Returns True if it was
        already recorded (i.e. a re-sync we should skip). Does NOT persist —
        the caller saves once after a relay / end of pass."""
        if event_id in self.mx_seen:
            return True
        self.mx_seen.append(event_id)
        if len(self.mx_seen) > MX_SEEN_LIMIT:
            del self.mx_seen[: len(self.mx_seen) - MX_SEEN_LIMIT]
        return False


def _tg_text(msg) -> str | None:
    """Body to relay for a Telegram message, or None to skip.

    Plain text passes through; media becomes a placeholder; empty service
    messages are skipped."""
    if msg.text:
        return msg.text
    if msg.media:
        for attr, label in _TG_MEDIA_PLACEHOLDER.items():
            if getattr(msg.media, attr, None) is not None:
                return label
        return "[non-text message in Telegram]"
    return None


def _mx_body(evt) -> str | None:
    """Body to relay for a decrypted Matrix event, or None to skip.

    text/notice pass through; emote is prefixed with '* '; media becomes a
    placeholder; edits (m.replace) and anything that isn't m.room.message are
    skipped per #49 v1 scope."""
    content = getattr(evt, "content", None)
    if content is None:
        return None
    rel = getattr(content, "relates_to", None)
    if rel is not None and getattr(rel, "rel_type", None) == "m.replace":
        return None  # edit — skipped in v1
    msgtype = getattr(content, "msgtype", None)
    body = getattr(content, "body", None)
    if msgtype in (MessageType.TEXT, MessageType.NOTICE):
        return body or ""
    if msgtype == MessageType.EMOTE:
        return f"* {body}*" if body else None
    if msgtype in (MessageType.IMAGE, MessageType.FILE, MessageType.AUDIO, MessageType.VIDEO):
        return f"[{getattr(msgtype, 'value', msgtype)} posted in Matrix]"
    return None


_mx_name_cache: dict[str, str] = {}


async def _mx_displayname(mx_client, user_id: str) -> str:
    """Human-readable sender for the relay prefix. Displayname if fetchable,
    else the mxid localpart — never raises (attribution is cosmetic)."""
    if user_id in _mx_name_cache:
        return _mx_name_cache[user_id]
    name = user_id.split(":")[0].lstrip("@") or user_id
    try:
        prof = await mx_client.get_displayname(UserID(user_id))
        dn = getattr(prof, "displayname", None)
        if dn:
            name = str(dn)
    except Exception:
        pass
    _mx_name_cache[user_id] = name
    return name


def _try_deserialize(raw):
    try:
        return Event.deserialize(raw)
    except Exception:
        return None


async def mx_send_relay(mx_client, room: str, sender: str, text: str) -> None:
    """Encrypt + send one relayed line into the Matrix room."""
    body = f"**{sender}:** {text}"
    await mx_client.send_message_event(
        room,
        EventType.ROOM_MESSAGE,
        TextMessageEventContent(msgtype=MessageType.TEXT, body=body),
    )
    log("mx", f"-> {body!r}")


async def tg_send_relay(tg_client, tg_chat_id: int, sender: str, text: str) -> None:
    """Send one relayed line into the Telegram group."""
    body = f"**{sender}:** {text}"
    await tg_client.send_message(tg_chat_id, body)
    log("tg", f"-> {body!r}")


async def _mx_sync(mx_client, room: str, *, full_state: bool = False, timeout: int = MX_SYNC_TIMEOUT):
    """One /sync cycle.

    Refreshes the joined-room set the crypto state store needs, lets
    OlmMachine process to-device events (megolm key shares) by gathering
    handle_sync's tasks, and persists next_batch only after they all
    succeed — a failing handler raises and the batch is retried from the
    old cursor (#93). Returns the bridge room's raw timeline events
    (oldest-first) for the caller to relay."""
    since = None if full_state else await mx_client.sync_store.get_next_batch()
    data = await mx_client.sync(since=since, timeout=timeout, full_state=full_state)
    if not isinstance(data, dict):
        return []
    mx_client._mx_state._joined.clear()
    mx_client._mx_state._joined.update(data.get("rooms", {}).get("join", {}).keys())
    tasks = mx_client.handle_sync(data)
    if tasks:
        await asyncio.gather(*tasks)
    nb = data.get("next_batch")
    if nb:
        await mx_client.sync_store.put_next_batch(nb)
    return (
        data.get("rooms", {})
        .get("join", {})
        .get(room, {})
        .get("timeline", {})
        .get("events", [])
    )


async def mx_relay_once(mx_client, room, bot_mxid, tg_client, tg_chat_id, state, *, timeout=MX_SYNC_TIMEOUT):
    """Sync once and relay every new non-self Matrix message in the bridge
    room to Telegram. State is saved after each relay so a kill never
    double-delivers."""
    timeline = await _mx_sync(mx_client, room, timeout=timeout)
    for raw in timeline:
        evt = _try_deserialize(raw)
        if evt is None:
            continue
        event_id = getattr(evt, "event_id", None)
        if not event_id:
            continue
        sender = str(getattr(evt, "sender", ""))
        # Loop prevention: drop our own bridge-bot posts (mark seen so a
        # re-sync doesn't re-evaluate them).
        if sender == bot_mxid:
            state.mark_mx(event_id)
            log("mx", f"skip own echo {event_id}")
            continue
        if evt.type == EventType.ROOM_ENCRYPTED and isinstance(evt, EncryptedEvent):
            try:
                evt = await mx_client.crypto.decrypt_megolm_event(evt)
            except Exception as exc:
                # Mark seen so we don't retry forever; log honestly, never mask.
                state.mark_mx(event_id)
                state.save()
                log("mx", f"undecryptable {event_id}: {type(exc).__name__}: {exc}")
                continue
        if evt.type != EventType.ROOM_MESSAGE:
            state.mark_mx(event_id)
            continue
        if event_id in state.mx_seen:
            continue  # already relayed (dedup)
        body = _mx_body(evt)
        if body is None:
            continue  # edit / reaction / redaction / unsupported — v1 skips
        name = await _mx_displayname(mx_client, sender)
        try:
            await tg_send_relay(tg_client, tg_chat_id, name, body)
        except Exception as exc:
            log("tg", f"send failed for {event_id}: {type(exc).__name__}: {exc}")
            # Do not mark the event until delivery succeeds. The next pass
            # must retry a failed send, including after a process restart.
            continue
        state.mark_mx(event_id)
        state.save()
    state.save()


async def tg_poll_once(tg_client, tg_chat_id, tg_me_id, mx_client, room, state):
    """Poll once and relay every new non-self Telegram message in the group to
    Matrix. Advances the cursor past every processed id (relayed or skipped)
    so a stuck message can't block the queue."""
    new = []
    async for msg in tg_client.iter_messages(tg_chat_id, limit=TG_POLL_LIMIT):
        if msg.id <= state.tg_last_id:
            break
        new.append(msg)
    if not new:
        return
    # First-ever start: set the cursor to the newest id and skip the backlog
    # (never dump history into the other side on a fresh deploy).
    if state.tg_last_id == 0:
        state.tg_last_id = new[0].id
        state.save()
        log("tg", f"first start: cursor set to {new[0].id}, backlog skipped")
        return
    for msg in reversed(new):  # oldest -> newest
        # Loop prevention: drop our own posts (the Telethon account's).
        if msg.sender_id == tg_me_id:
            log("tg", f"skip own echo id={msg.id}")
            state.tg_last_id = max(state.tg_last_id, msg.id)
            continue
        text = _tg_text(msg)
        if text is None:
            state.tg_last_id = max(state.tg_last_id, msg.id)
            continue
        sender = tg_sender_name(await msg.get_sender(), msg.sender_id)
        try:
            await mx_send_relay(mx_client, room, sender, text)
        except Exception as exc:
            log("mx", f"send failed for tg id={msg.id}: {type(exc).__name__}: {exc}")
            continue  # leave cursor behind so the next pass retries
        state.tg_last_id = max(state.tg_last_id, msg.id)
        state.save()
    state.save()


async def run(args) -> int:
    creds, room = mx_load_config()
    tg_chat_id = tg_load_chat_id()
    bot_mxid = creds["user_id"]
    state = State.load()

    mx_client = await mx_make_client(creds, sync_cursor_path=RELAY_SYNC_CURSOR)
    tg_client = tg_make_client()

    await tg_client.connect()
    try:
        from telethon.errors import AuthKeyError

        try:
            authorized = await tg_client.is_user_authorized()
        except AuthKeyError as e:
            raise SystemExit(f"tg session auth key unusable ({e}); re-login is an operator action")
        if not authorized:
            raise SystemExit(
                "tg session not authorized; re-login in place on the relay host "
                "(single-host rule)"
            )
        me = await tg_client.get_me()
    except BaseException:
        await tg_client.disconnect()
        raise
    tg_me_id = me.id

    # mx.py already bootstrapped the device once (cross-signed, wake posted).
    # share_keys is idempotent — a no-op once device keys are on the server.
    await mx_client.crypto.share_keys()

    log(
        "relay",
        f"start room={room} tg_chat={tg_chat_id} bot={bot_mxid} tg_me={tg_me_id}",
    )

    # First-ever Matrix start: the relay keeps its OWN /sync cursor
    # (RELAY_SYNC_CURSOR), separate from mx.py's. If that cursor file doesn't
    # exist yet, advance it to "now" without relaying — mirroring the
    # Telegram first-start skip. Tied to the cursor file (not a flag) so it
    # self-heals if the file is deleted.
    if await mx_client.sync_store.get_next_batch() is None:
        log("mx", "first start: advancing relay sync cursor to now (backlog skipped)")
        await _mx_sync(mx_client, room, timeout=0)

    if args.once:
        # One pass each side with a short MX window, then a clean exit.
        try:
            await tg_poll_once(tg_client, tg_chat_id, tg_me_id, mx_client, room, state)
            await mx_relay_once(
                mx_client, room, bot_mxid, tg_client, tg_chat_id, state, timeout=2000
            )
        finally:
            state.save()
            await tg_client.disconnect()
            await mx_shutdown(mx_client)
            log("relay", "--once complete")
        return 0

    stop = asyncio.Event()

    def _stop(*_):
        if not stop.is_set():
            log("relay", "stop signal received")
        stop.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            # e.g. Windows — fall back to KeyboardInterrupt handling.
            pass

    async def tg_loop():
        while not stop.is_set():
            try:
                await tg_poll_once(
                    tg_client, tg_chat_id, tg_me_id, mx_client, room, state
                )
            except Exception as exc:
                log("tg", f"poll error: {type(exc).__name__}: {exc}")
            try:
                await asyncio.wait_for(stop.wait(), timeout=TG_POLL_INTERVAL)
            except asyncio.TimeoutError:
                pass

    async def mx_loop():
        while not stop.is_set():
            try:
                await mx_relay_once(
                    mx_client, room, bot_mxid, tg_client, tg_chat_id, state,
                    timeout=MX_SYNC_TIMEOUT,
                )
            except Exception as exc:
                log("mx", f"sync error: {type(exc).__name__}: {exc}")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass

    tasks = [asyncio.create_task(tg_loop()), asyncio.create_task(mx_loop())]
    try:
        await stop.wait()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        state.save()
        await tg_client.disconnect()
        await mx_shutdown(mx_client)
        log("relay", "stopped cleanly")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="relay.py",
        description="Bidirectional Matrix<->Telegram relay (test-fixture pair only).",
    )
    ap.add_argument(
        "--once",
        action="store_true",
        help="run one poll pass on each side and exit (no long-poll loop)",
    )
    args = ap.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except SystemExit:
        raise
    except Exception as exc:
        print(f"relay.py: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
