"""Unit test for _welcome_room_alive.

Regression for the PR #91 review finding: the liveness check read only
m.room.tombstone, so a room the bot had LEFT or been evicted from —
without a tombstone — read as "alive" and /join/api kept handing out the
stale, unusable alias instead of reminting. Membership must now be asked
of /joined_rooms, which answers for the bot's own token.

Standalone — `python3 tests/welcome_liveness_unit.py`. Doesn't need
continuwuity: aiohttp is monkey-patched at the module level, the same way
self_heal_unit.py stubs the credential helpers.
"""
import asyncio, os, sys, tempfile
from pathlib import Path

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


class _Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body
    async def text(self):
        return self._body
    async def json(self):
        return self._body


class _GetCtx:
    def __init__(self, resp):
        self._resp = resp
    async def __aenter__(self):
        return self._resp
    async def __aexit__(self, *exc):
        return False


class _Session:
    """Fake aiohttp.ClientSession: GETs are routed by URL suffix. A GET no
    route matches raises, so a code path reaching for an endpoint the test
    did not stage fails loudly instead of passing."""
    routes = ()

    def __init__(self, headers=None):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *exc):
        return False
    def get(self, url):
        for suffix, resp in _Session.routes:
            if url.endswith(suffix):
                return _GetCtx(resp)
        raise AssertionError(f"unexpected GET {url}")


def _install(routes):
    _Session.routes = tuple(routes)
    approver.aiohttp.ClientSession = _Session


TOMBSTONE = "/state/m.room.tombstone"
JOINED = "/joined_rooms"


def test_joined_and_untombstoned_is_alive():
    _install([
        (TOMBSTONE, _Resp(404, "{}")),
        (JOINED, _Resp(200, {"joined_rooms": ["!w:t", "!other:t"]})),
    ])
    assert asyncio.run(approver._welcome_room_alive("!w:t")) is True


def test_bot_left_without_tombstone_is_dead():
    """The PR #91 defect: tombstone reads 404 (room never closed) but the
    bot is no longer in the room — must read as dead so /join/api remints."""
    _install([
        (TOMBSTONE, _Resp(404, "{}")),
        (JOINED, _Resp(200, {"joined_rooms": ["!other:t"]})),
    ])
    assert asyncio.run(approver._welcome_room_alive("!w:t")) is False


def test_tombstoned_is_dead_without_membership_lookup():
    # No JOINED route staged: reaching for /joined_rooms after a tombstone
    # would raise AssertionError here.
    _install([(TOMBSTONE, _Resp(200, '{"replacement_room": "!x:t"}'))])
    assert asyncio.run(approver._welcome_room_alive("!w:t")) is False


def test_unreadable_state_is_dead():
    _install([(TOMBSTONE, _Resp(403, '{"errcode":"M_FORBIDDEN"}'))])
    assert asyncio.run(approver._welcome_room_alive("!w:t")) is False


def test_unexpected_tombstone_status_raises():
    _install([(TOMBSTONE, _Resp(500, "boom"))])
    try:
        asyncio.run(approver._welcome_room_alive("!w:t"))
    except RuntimeError as e:
        assert "tombstone check" in str(e), e
    else:
        raise AssertionError("500 on tombstone must raise, not guess")


def test_joined_rooms_failure_raises():
    _install([
        (TOMBSTONE, _Resp(404, "{}")),
        (JOINED, _Resp(502, "bad gateway")),
    ])
    try:
        asyncio.run(approver._welcome_room_alive("!w:t"))
    except RuntimeError as e:
        assert "joined_rooms" in str(e), e
    else:
        raise AssertionError("502 on joined_rooms must raise, not guess")


if __name__ == "__main__":
    test_joined_and_untombstoned_is_alive()
    print("ok: joined_and_untombstoned_is_alive")
    test_bot_left_without_tombstone_is_dead()
    print("ok: bot_left_without_tombstone_is_dead")
    test_tombstoned_is_dead_without_membership_lookup()
    print("ok: tombstoned_is_dead_without_membership_lookup")
    test_unreadable_state_is_dead()
    print("ok: unreadable_state_is_dead")
    test_unexpected_tombstone_status_raises()
    print("ok: unexpected_tombstone_status_raises")
    test_joined_rooms_failure_raises()
    print("ok: joined_rooms_failure_raises")
    print("all tests passed")
