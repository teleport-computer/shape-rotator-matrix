"""Regression test for the announce_welcome_events flood.

The ancestor bug (May 9, prod): process_operator_announce /
announce_lobby_events used to GC its bookkeeping for any closed room. Then on
the next cycle setdefault re-added each room with an empty record, fired 🚪 /
⚠️ for every user, GC'd them again, repeat forever — ~38 sends every 8s into
matrix-devops. announce_welcome_events (issue #3 rewrite) keeps the same
sidecar-seen discipline over the welcome-room store, so the regression test
carries over: records must fire exactly once and reaped rooms must not
re-fire anything.

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
    "WELCOME_PATH": f"{TMP}/welcome_rooms.json",
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


def test_no_flood_on_repeated_cycles():
    """A store of joined rooms + an empty seen file: every join fires 🚪
    exactly once, and no later cycle re-fires anything."""
    sends = _install_send_recorder()
    welcome = {}
    for i in range(26):
        welcome[f"code{i}"] = {
            "room_id": f"!r{i}:t", "room_alias": f"#welcome-{i}:t",
            "created_at": 1.0, "joined_by": f"@u{i}:t", "joined_at": 2.0,
        }
    approver._save(approver.WELCOME_PATH, welcome)
    approver._save(approver.OPERATOR_ANNOUNCE_PATH,
                   {"seed": {"joined": True}})

    client = MagicMock()
    asyncio.run(approver.announce_welcome_events(client))
    fired = [t for _r, t in sends if "joined via code" in t]
    assert len(fired) == 26, f"expected 26 🚪, got {len(fired)}: {sends[:3]}"

    sends.clear()
    asyncio.run(approver.announce_welcome_events(client))
    asyncio.run(approver.announce_welcome_events(client))
    assert sends == [], f"re-fired after already-announced: {sends[:3]}"


def test_first_run_backfill_suppresses_history():
    """No announce file yet + existing state → seed, send nothing."""
    sends = _install_send_recorder()
    approver._save(approver.WELCOME_PATH, {
        "abc": {"room_id": "!r:t", "room_alias": "#welcome-abc:t",
                "created_at": 1.0, "joined_by": "@u:t", "joined_at": 2.0},
    })
    p = approver.OPERATOR_ANNOUNCE_PATH
    if p.exists():
        p.unlink()

    client = MagicMock()
    asyncio.run(approver.announce_welcome_events(client))
    assert sends == [], f"first run must backfill silently, got {sends}"
    seen = json.loads(p.read_text())
    assert seen == {"abc": {"joined": True}}, f"bad backfill: {seen}"


def test_unjoined_room_announces_nothing_until_joined():
    sends = _install_send_recorder()
    welcome = {
        "abc": {"room_id": "!r:t", "room_alias": "#welcome-abc:t",
                "created_at": 1.0},
    }
    approver._save(approver.WELCOME_PATH, welcome)
    approver._save(approver.OPERATOR_ANNOUNCE_PATH,
                   {"seed": {"joined": True}})

    client = MagicMock()
    asyncio.run(approver.announce_welcome_events(client))
    assert sends == [], f"un-joined room must not announce: {sends}"

    welcome["abc"]["joined_by"] = "@late:t"
    welcome["abc"]["joined_at"] = 2.0
    approver._save(approver.WELCOME_PATH, welcome)
    asyncio.run(approver.announce_welcome_events(client))
    fired = [t for _r, t in sends if "@late:t" in t and "joined via code" in t]
    assert len(fired) == 1, f"expected exactly 1 🚪, got {sends}"


def test_reaped_room_gc_does_not_refire():
    """A reaped room disappears from the store; its seen record is GC'd; the
    SAME code minting a NEW room with a NEW joiner announces again (a
    genuinely new join), but nothing fires while the key is simply gone."""
    sends = _install_send_recorder()
    welcome = {
        "abc": {"room_id": "!r:t", "room_alias": "#welcome-abc:t",
                "created_at": 1.0, "joined_by": "@u:t", "joined_at": 2.0},
    }
    approver._save(approver.WELCOME_PATH, welcome)
    approver._save(approver.OPERATOR_ANNOUNCE_PATH,
                   {"seed": {"joined": True}})

    client = MagicMock()
    asyncio.run(approver.announce_welcome_events(client))
    assert len(sends) == 1

    # Room reaped: removed from the store entirely.
    sends.clear()
    approver._save(approver.WELCOME_PATH, {})
    asyncio.run(approver.announce_welcome_events(client))
    asyncio.run(approver.announce_welcome_events(client))
    assert sends == [], f"reaped room must not fire anything: {sends}"
    seen = json.loads(approver.OPERATOR_ANNOUNCE_PATH.read_text())
    assert "abc" not in seen, f"seen record must be GC'd: {seen}"


if __name__ == "__main__":
    test_no_flood_on_repeated_cycles()
    print("ok: no_flood_on_repeated_cycles")
    test_first_run_backfill_suppresses_history()
    print("ok: first_run_backfill_suppresses_history")
    test_unjoined_room_announces_nothing_until_joined()
    print("ok: unjoined_room_announces_nothing_until_joined")
    test_reaped_room_gc_does_not_refire()
    print("ok: reaped_room_gc_does_not_refire")
    print("all tests passed")
