"""Issue #93 — sync-handler exceptions must surface, and next_batch must be
persisted only after every handle_sync task succeeds.

Regression for the bug that left Matrix->Telegram relaying dead for a month:
the sync-handler gathers ran with return_exceptions=True and never inspected
the results, AND next_batch was persisted BEFORE the handlers ran — so a
failing Olm/room-key handler was silently discarded and its batch (with the
to-device room-key shares it carried) was never retried.

Exercises the REAL production functions (mx.sync_once and relay._mx_sync)
via duck-typed stub clients — no network, no crypto, no continuwuity — plus
the make_client wiring (fail-fast dispatch, wait_sync handlers) the
propagation depends on.

Standalone — `python3 tests/sync_cursor_unit.py` in the runner image
(mautrix for the mx import, telethon for the relay import).
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "bridge"))

# Hermetic store for the make_client wiring check (mx reads MX_STORE at
# import time).
os.environ.setdefault("MX_STORE", tempfile.mkdtemp(prefix="sync-cursor-unit-"))

import mx  # noqa: E402
import relay  # noqa: E402

ROOM = "!bridge:test"


def _batch(next_batch, event_id=None):
    events = [{"event_id": event_id}] if event_id else []
    return {
        "next_batch": next_batch,
        "rooms": {"join": {ROOM: {"timeline": {"events": events}}}},
    }


class _StubSyncStore:
    """Records put_next_batch calls; get_next_batch returns the live value."""

    def __init__(self, nb):
        self.nb = nb
        self.puts = []

    async def get_next_batch(self):
        return self.nb

    async def put_next_batch(self, nb):
        self.puts.append(nb)
        self.nb = nb


class _StubCryptoState:
    def __init__(self):
        self._joined = set()


class _StubClient:
    """Duck-types the mautrix Client surface sync_once/_mx_sync touch.

    batches: sync() payloads returned in call order. fail_on: index of the
    sync() call whose handle_sync tasks raise (the failing batch's handler);
    every other cycle's handlers succeed. Each cycle returns TWO tasks, so
    the gather sees the real shape: one fine handler, one failing."""

    def __init__(self, batches, fail_on=None):
        self._batches = list(batches)
        self.fail_on = fail_on
        self.sync_calls = []  # (since, timeout, full_state) per call
        self.sync_store = _StubSyncStore("CURSOR_OLD")
        self._mx_state = _StubCryptoState()

    async def sync(self, since=None, timeout=None, full_state=False):
        self.sync_calls.append((since, timeout, full_state))
        return self._batches.pop(0)

    def handle_sync(self, data):
        cycle = len(self.sync_calls) - 1

        async def _fine():
            return "handled"

        async def _failing():
            if cycle == self.fail_on:
                raise RuntimeError("simulated room-key handler failure")

        return [asyncio.ensure_future(_fine()), asyncio.ensure_future(_failing())]


async def _expect_failure(coro, what):
    try:
        await coro
    except RuntimeError as exc:
        assert "simulated room-key handler failure" in str(exc), exc
    else:
        raise AssertionError(f"{what} swallowed the handler failure")


async def test_mx_failure_raises_and_cursor_held():
    c = _StubClient([_batch("NB_1", "$e1")], fail_on=0)
    await _expect_failure(mx.sync_once(c, timeout=0), "mx.sync_once")
    assert c.sync_store.puts == [], f"next_batch persisted on failure: {c.sync_store.puts}"
    assert c.sync_store.nb == "CURSOR_OLD", c.sync_store.nb
    assert c._mx_state._joined == {ROOM}, c._mx_state._joined
    print("ok: mx.sync_once raises on handler failure; cursor held, not persisted")


async def test_mx_retry_same_batch_then_advance():
    # The retried sync re-serves the same batch from CURSOR_OLD.
    c = _StubClient([_batch("NB_1", "$e1"), _batch("NB_1", "$e1")], fail_on=0)
    await _expect_failure(mx.sync_once(c, timeout=0), "mx.sync_once")
    await mx.sync_once(c, timeout=0)
    assert c.sync_calls[1][0] == "CURSOR_OLD", f"retry used {c.sync_calls[1][0]!r}, not the old cursor"
    assert c.sync_store.puts == ["NB_1"], c.sync_store.puts
    print("ok: mx.sync_once retries the failed batch from the old cursor, then advances")


async def test_relay_failure_raises_and_cursor_held():
    c = _StubClient([_batch("NB_1", "$e1")], fail_on=0)
    await _expect_failure(relay._mx_sync(c, ROOM, timeout=0), "relay._mx_sync")
    assert c.sync_store.puts == [], f"next_batch persisted on failure: {c.sync_store.puts}"
    assert c.sync_store.nb == "CURSOR_OLD", c.sync_store.nb
    print("ok: relay._mx_sync raises on handler failure; cursor held, not persisted")


async def test_relay_retry_returns_timeline_and_advances():
    c = _StubClient([_batch("NB_1", "$e1"), _batch("NB_1", "$e1")], fail_on=0)
    await _expect_failure(relay._mx_sync(c, ROOM, timeout=0), "relay._mx_sync")
    timeline = await relay._mx_sync(c, ROOM, timeout=0)
    assert c.sync_calls[1][0] == "CURSOR_OLD", f"retry used {c.sync_calls[1][0]!r}, not the old cursor"
    assert c.sync_store.puts == ["NB_1"], c.sync_store.puts
    assert [e["event_id"] for e in timeline] == ["$e1"], timeline
    print("ok: relay._mx_sync retries the failed batch, returns its timeline, advances once")


async def test_make_client_wires_failfast_dispatch():
    """The propagation above only works because make_client builds a
    _FailFastClient (re-raises in _catch_errors) and re-registers every
    handler wait_sync=True — mautrix's defaults swallow + detach (#93)."""
    creds = {
        "user_id": "@unit:example.org",
        "access_token": "x",
        "device_id": "UNITDEV",
        "homeserver": "http://127.0.0.1:1",
    }
    client = await mx.make_client(
        creds, sync_cursor_path=Path(tempfile.mkdtemp()) / "next_batch")
    try:
        assert isinstance(client, mx._FailFastClient), type(client)
        not_waited = [
            (etype, getattr(h, "__name__", h))
            for etype, handlers in client.event_handlers.items()
            for h, props in handlers.items()
            if not props.wait_sync
        ]
        assert not not_waited, f"handlers not awaited by sync: {not_waited}"
    finally:
        await mx._shutdown(client)
    print("ok: make_client dispatches fail-fast (all handlers wait_sync=True)")


async def main():
    await test_mx_failure_raises_and_cursor_held()
    await test_mx_retry_same_batch_then_advance()
    await test_relay_failure_raises_and_cursor_held()
    await test_relay_retry_returns_timeline_and_advances()
    await test_make_client_wires_failfast_dispatch()
    print("all tests passed")


if __name__ == "__main__":
    asyncio.run(main())
