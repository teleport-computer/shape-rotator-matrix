"""Regression test for announce_lobby_events flood.

The bug: process_operator_announce / announce_lobby_events used to GC its
bookkeeping for any closed lobby. Then on the next cycle setdefault re-
added each closed room with empty rec, fired 🚪 / ⚠️ for every challenged
user / bad-reason close, GC'd them again, repeat forever. In prod (May 9)
this hit ~38 sends every 8s into matrix-devops.

This test reproduces the prod state (lobby.json with closed rooms,
operator_announce.json == {}) and asserts no sends are made.

Standalone — `python3 tests/announce_unit.py`. Doesn't need the docker
compose harness used by the *_e2e.py tests, because the bug is pure
application logic.
"""
import asyncio, json, os, sys, tempfile
from pathlib import Path
from unittest.mock import MagicMock

TMP = tempfile.mkdtemp()
os.environ.update({
    "HS": "http://localhost",
    "SPACE_ID": "!s:t",
    "SPACE_CHILD_IDS": "",
    "REG_TOKEN": "x",
    "LOBBY_PATH": f"{TMP}/lobby.json",
    "OPERATOR_ANNOUNCE_PATH": f"{TMP}/op.json",
    "OPERATOR_NOTIFY_ROOM": "!notify:t",
    "ADMIN_COMMAND_ROOM": "!admin:t",
})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "knock-approver"))
import approver


def _install_send_recorder():
    sends = []
    async def fake_send(client, room, text):
        sends.append((room, text))
    approver._send_msg = fake_send
    return sends


def test_no_flood_on_historical_closed_rooms():
    sends = _install_send_recorder()
    # 26 closed-with-bad-reason rooms (each would fire ⚠️) and 12 of them
    # also have a challenged user (each would also fire 🚪), plus 6
    # promoted/already-member rooms (no fire either way).
    lobby = {}
    for i in range(26):
        lobby[f"!bad{i}:t"] = {
            "code": f"c{i}",
            "challenged": [f"@u{i}:t"] if i < 12 else [],
            "displaynames": {f"@u{i}:t": f"u{i}"} if i < 12 else {},
            "closed": True,
            "closed_reason": "tries_exhausted",
        }
    for i in range(6):
        lobby[f"!ok{i}:t"] = {
            "code": f"c{26+i}",
            "challenged": [f"@p{i}:t"],
            "displaynames": {f"@p{i}:t": f"p{i}"},
            "closed": True,
            "closed_reason": "promoted",
        }
    approver._save(approver.LOBBY_PATH, lobby)
    approver._save(approver.OPERATOR_ANNOUNCE_PATH, {})

    client = MagicMock()
    asyncio.run(approver.announce_lobby_events(client))
    asyncio.run(approver.announce_lobby_events(client))
    asyncio.run(approver.announce_lobby_events(client))

    assert sends == [], f"expected no sends after fix; got {len(sends)}: {sends[:3]}"

    seen = json.loads(approver.OPERATOR_ANNOUNCE_PATH.read_text())
    assert len(seen) == 32, f"expected 32 records, got {len(seen)}"


def test_open_room_still_announced():
    sends = _install_send_recorder()
    lobby = {
        "!open:t": {
            "code": "abc", "challenged": ["@u:t"],
            "displaynames": {"@u:t": "u"}, "closed": False,
        },
    }
    approver._save(approver.LOBBY_PATH, lobby)
    approver._save(approver.OPERATOR_ANNOUNCE_PATH, {"!seen-prev:t": {"started": [], "failed": False}})

    client = MagicMock()
    asyncio.run(approver.announce_lobby_events(client))

    fires = [t for _r, t in sends if "started lobby flow" in t]
    assert len(fires) == 1, f"expected 1 🚪, got {sends}"


def test_seen_persists_across_cycles_for_open_then_closed():
    sends = _install_send_recorder()
    lobby = {
        "!l:t": {
            "code": "abc", "challenged": ["@u:t"],
            "displaynames": {"@u:t": "u"}, "closed": False,
        },
    }
    approver._save(approver.LOBBY_PATH, lobby)
    approver._save(approver.OPERATOR_ANNOUNCE_PATH, {})

    client = MagicMock()
    asyncio.run(approver.announce_lobby_events(client))  # fires 🚪
    assert len(sends) == 1

    # Lobby fails — close it with a bad reason
    lobby["!l:t"]["closed"] = True
    lobby["!l:t"]["closed_reason"] = "tries_exhausted"
    approver._save(approver.LOBBY_PATH, lobby)

    asyncio.run(approver.announce_lobby_events(client))  # should fire ⚠️ once
    asyncio.run(approver.announce_lobby_events(client))  # MUST NOT re-fire

    assert len(sends) == 2, f"expected exactly 2 sends total (🚪 + ⚠️), got {len(sends)}: {sends}"


if __name__ == "__main__":
    test_no_flood_on_historical_closed_rooms()
    print("ok: no_flood_on_historical_closed_rooms")
    test_open_room_still_announced()
    print("ok: open_room_still_announced")
    test_seen_persists_across_cycles_for_open_then_closed()
    print("ok: seen_persists_across_cycles_for_open_then_closed")
    print("all tests passed")
