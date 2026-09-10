"""Unit test for process_welcome_join's consumption semantics.

Regression for the second PR #91 review finding: the use was decremented
and joined_by recorded BEFORE the space invite was attempted, so any
invite status other than 200/403 (a transient 5xx/429) permanently burned
the code and armed the exactly-once guard — the joiner never got the
invite and no later join could retry. Consumption must now happen only
after the invite went out; a failed invite leaves both the use and the
guard untouched.

Standalone — `python3 tests/welcome_consume_unit.py`. Doesn't need
continuwuity: aiohttp is monkey-patched at the module level, the same way
welcome_liveness_unit.py stubs the liveness reads.
"""
import asyncio, json, os, sys, tempfile
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update({
    "HS": "http://localhost",
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


sent = []  # bodies handed to _send_msg_raw's PUT


class _Session:
    """Fake aiohttp.ClientSession: POSTs/PUTs are routed by URL suffix. A
    call no route matches raises, so a code path reaching for an endpoint
    the test did not stage fails loudly instead of passing — a failed
    invite must not even try to post the confirmation."""
    invite_status = None
    invite_body = ""

    def __init__(self, headers=None):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *exc):
        return False
    def post(self, url, json=None):
        if url.endswith("/invite"):
            return _Ctx(_Resp(_Session.invite_status, _Session.invite_body))
        raise AssertionError(f"unexpected POST {url}")
    def put(self, url, json=None):
        if "/send/m.room.message/" in url:
            sent.append(json["body"])
            return _Ctx(_Resp(200, {"event_id": "$1"}))
        raise AssertionError(f"unexpected PUT {url}")


def _install(invite_status, invite_body=""):
    _Session.invite_status, _Session.invite_body = invite_status, invite_body
    approver.aiohttp.ClientSession = _Session


def _seed(uses=5):
    approver._save(approver.CODES_PATH, {"dev": {"uses_remaining": uses}})
    approver.WELCOME_PATH.unlink(missing_ok=True)
    approver.LOG_PATH.unlink(missing_ok=True)
    sent.clear()
    return {"room_id": "!w:t", "room_alias": "#welcome-x:t", "created_at": 1}


def _uses():
    return approver._load(approver.CODES_PATH)["dev"]["uses_remaining"]


def _audit_types():
    return [json.loads(l)["type"] for l in approver.LOG_PATH.read_text().splitlines()]


JOINER, LOBBY = "@joiner:elsewhere", "@bot:t"


def test_failed_invite_consumes_nothing():
    """The PR #91 defect: a 500 from /invite must not burn the use, must
    not arm the exactly-once guard, and must not post a confirmation."""
    meta = _seed(uses=5)
    _install(500, '{"errcode":"M_UNKNOWN"}')
    asyncio.run(approver.process_welcome_join("dev", meta, JOINER, LOBBY))
    assert _uses() == 5, _uses()
    assert "joined_by" not in meta, meta
    assert _audit_types() == ["welcome_invite_failed"], _audit_types()
    assert sent == [], sent  # no confirmation on a failed invite


def test_rate_limited_invite_consumes_nothing():
    meta = _seed(uses=5)
    _install(429, '{"errcode":"M_LIMIT_EXCEEDED"}')
    asyncio.run(approver.process_welcome_join("dev", meta, JOINER, LOBBY))
    assert _uses() == 5, _uses()
    assert "joined_by" not in meta, meta
    assert _audit_types() == ["welcome_invite_failed"], _audit_types()
    assert sent == [], sent


def test_retry_after_failure_consumes_once():
    """The rejoin that follows a failed invite must go through the whole
    consume + invite + confirm path — exactly one use taken."""
    meta = _seed(uses=5)
    _install(500, '{"errcode":"M_UNKNOWN"}')
    asyncio.run(approver.process_welcome_join("dev", meta, JOINER, LOBBY))
    _install(200, "{}")
    asyncio.run(approver.process_welcome_join("dev", meta, JOINER, LOBBY))
    assert _uses() == 4, _uses()
    assert meta["joined_by"] == JOINER, meta
    assert _audit_types() == ["welcome_invite_failed", "welcome_joined"]
    assert sent == ["invite sent — accept it in Element and you're in."], sent


def test_already_member_403_consumes():
    meta = _seed(uses=5)
    _install(403, '{"errcode":"M_FORBIDDEN"}')
    asyncio.run(approver.process_welcome_join("dev", meta, JOINER, LOBBY))
    assert _uses() == 4, _uses()
    assert meta["joined_by"] == JOINER, meta
    assert _audit_types() == ["welcome_joined"], _audit_types()
    assert sent == ["you're already in shape rotator — see you in the space."], sent


if __name__ == "__main__":
    for t in (test_failed_invite_consumes_nothing,
              test_rate_limited_invite_consumes_nothing,
              test_retry_after_failure_consumes_once,
              test_already_member_403_consumes):
        t()
        print(f"ok: {t.__name__}")
    print("all tests passed")
