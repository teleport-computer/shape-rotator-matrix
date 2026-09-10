"""Unit test for the /join/api mint path (_create_welcome_room + the
join_handler failure handling around it).

Regression for the PR #91 review finding (2026-09-10T11:17Z): a non-200
post-create /join was logged as a warning and the mint continued, so
/join/api persisted and returned a room whose onboarding bot was not
joined — an unusable alias handed to the user. A non-200 must raise, so
the handler answers create_failed without persisting the mapping or
burning a use; the stranded alias is freed by the M_ROOM_IN_USE retry
on the next request.

Standalone — `python3 tests/welcome_mint_unit.py`. Doesn't need
continuwuity: aiohttp is monkey-patched at the module level, the same
way welcome_consume_unit.py stubs the invite/send calls.
"""
import asyncio, json, os, sys, tempfile
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update({
    "HS": "http://localhost",
    "SERVER_NAME": "t",
    "SPACE_ID": "!s:t",
    "SPACE_CHILD_IDS": "",
    "REG_TOKEN": "x",
    "CODES_PATH": f"{TMP}/codes.json",
    "WELCOME_PATH": f"{TMP}/welcome_rooms.json",
    "WELCOME_SECRET_PATH": f"{TMP}/welcome_secret",
    "LOG_PATH": f"{TMP}/log.jsonl",
    "ENDORSEMENTS_PATH": f"{TMP}/endorsements.jsonl",
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


class _Ctx:
    def __init__(self, resp):
        self._resp = resp
    async def __aenter__(self):
        return self._resp
    async def __aexit__(self, *exc):
        return False


class _Req:
    def __init__(self, body):
        self._body = body
    async def json(self):
        return self._body


class _Session:
    """Fake aiohttp.ClientSession: POSTs/PUTs are routed by URL suffix. A
    call no route matches raises, so a code path reaching for an endpoint
    the test did not stage fails loudly instead of passing — a failed
    mint must not even try to send or pin the welcome message."""
    create_status, create_body = 200, {"room_id": "!w:t"}
    join_status, join_body = 200, {"room_id": "!w:t"}

    def __init__(self, headers=None):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *exc):
        return False
    def post(self, url, json=None):
        if url.endswith("/createRoom"):
            return _Ctx(_Resp(_Session.create_status, _Session.create_body))
        if url.endswith("/join"):
            return _Ctx(_Resp(_Session.join_status, _Session.join_body))
        raise AssertionError(f"unexpected POST {url}")
    def put(self, url, json=None):
        if "/send/m.room.message/" in url:
            return _Ctx(_Resp(200, {"event_id": "$1"}))
        if url.endswith("/state/m.room.pinned_events"):
            return _Ctx(_Resp(200, {}))
        raise AssertionError(f"unexpected PUT {url}")


def _install(create=(200, {"room_id": "!w:t"}), join=(200, {"room_id": "!w:t"})):
    (_Session.create_status, _Session.create_body) = create
    (_Session.join_status, _Session.join_body) = join
    approver.aiohttp.ClientSession = _Session


def _seed(uses=3):
    approver._save(approver.CODES_PATH, {"dev": {"uses_remaining": uses}})
    approver.WELCOME_PATH.unlink(missing_ok=True)
    approver.LOG_PATH.unlink(missing_ok=True)


def _uses():
    return approver._load(approver.CODES_PATH)["dev"]["uses_remaining"]


def _audit_types():
    return [json.loads(l)["type"] for l in approver.LOG_PATH.read_text().splitlines()]


def test_create_room_raises_when_join_fails():
    """The PR #91 defect: a non-200 post-create /join must raise, not
    warn-and-continue with a room the bot is not joined in."""
    _install(join=(500, '{"errcode":"M_UNKNOWN"}'))
    try:
        asyncio.run(approver._create_welcome_room("welcome-x"))
    except RuntimeError as e:
        assert "post-create join" in str(e), e
    else:
        raise AssertionError("500 on the post-create join must raise, not continue")


def test_create_room_raises_when_createroom_fails():
    _install(create=(500, '{"errcode":"M_UNKNOWN"}'))
    try:
        asyncio.run(approver._create_welcome_room("welcome-x"))
    except RuntimeError as e:
        assert "createRoom" in str(e), e
    else:
        raise AssertionError("500 on createRoom must raise")


def test_happy_path_returns_room_id():
    _install()
    assert asyncio.run(approver._create_welcome_room("welcome-x")) == "!w:t"


def test_handler_failed_join_persists_nothing():
    """End to end through join_handler: the failed mint must answer
    create_failed, persist no mapping, and burn no use — the room the
    bot isn't joined in must never be handed out."""
    _seed(uses=3)
    _install(join=(500, '{"errcode":"M_UNKNOWN"}'))
    resp = asyncio.run(approver.join_handler(_Req({"code": "dev"})))
    assert resp.status == 500, resp.status
    assert json.loads(resp.text) == {"error": "create_failed",
                                     "detail": "post-create join !w:t -> 500: "
                                               '{"errcode":"M_UNKNOWN"}'}, resp.text
    assert approver._load(approver.WELCOME_PATH) == {}, approver.WELCOME_PATH
    assert _uses() == 3, _uses()
    assert _audit_types() == ["welcome_room_failed"], _audit_types()


def test_handler_happy_path_mints():
    _seed(uses=3)
    _install()
    resp = asyncio.run(approver.join_handler(_Req({"code": "dev"})))
    assert resp.status == 200, resp.status
    alias = json.loads(resp.text)["room_alias"]
    assert alias.startswith("#welcome-") and alias.endswith(":t"), alias
    meta = approver._load(approver.WELCOME_PATH)["dev"]
    assert meta["room_id"] == "!w:t" and meta["room_alias"] == alias, meta
    assert _uses() == 3, _uses()  # POST /join/api consumes nothing
    assert _audit_types() == ["welcome_created"], _audit_types()


if __name__ == "__main__":
    for t in (test_create_room_raises_when_join_fails,
              test_create_room_raises_when_createroom_fails,
              test_happy_path_returns_room_id,
              test_handler_failed_join_persists_nothing,
              test_handler_happy_path_mints):
        t()
        print(f"ok: {t.__name__}")
    print("all tests passed")
