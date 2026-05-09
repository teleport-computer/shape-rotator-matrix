"""Unit test for _resolve_credentials.

Specifically validates: a stale env_token + a previously-cached working
token + a cached device_id results in NO /login (cached token still
serves), and a stale env+cached token results in a /login that REUSES
the cached device_id (so the crypto store doesn't need wiping).

Standalone — `python3 tests/self_heal_unit.py`. Doesn't need
continuwuity. Mocks aiohttp at the module level via monkey-patching the
two helpers `_whoami` and `_login_with_password`.
"""
import asyncio, os, sys, tempfile
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update({
    "HS": "http://localhost",
    "SPACE_ID": "!s:t",
    "SPACE_CHILD_IDS": "",
    "REG_TOKEN": "x",
    "ADMIN_COMMAND_ROOM": "!admin:t",
})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "knock-approver"))
import approver


async def _run(coro):
    return await coro


def _patch_http(*, whoami_responses, login_responses):
    """Install fake _whoami / _login_with_password.
      - whoami_responses: dict mapping access_token -> /whoami JSON or None.
      - login_responses: list of (token, device, mxid) tuples returned in
        order by successive _login_with_password calls.
    Returns recorded call lists for assertions."""
    whoami_calls = []
    login_calls = []
    login_iter = iter(login_responses)

    async def fake_whoami(tok):
        whoami_calls.append(tok)
        return whoami_responses.get(tok)

    async def fake_login(username, password, device_id=None):
        login_calls.append((username, device_id))
        try:
            return next(login_iter)
        except StopIteration:
            return None, None, None

    approver._whoami = fake_whoami
    approver._login_with_password = fake_login
    return whoami_calls, login_calls


def _fresh_paths(name):
    base = Path(tempfile.mkdtemp(prefix=f"selfheal-{name}-"))
    return (base / "password", base / "token", base / "device_id")


def test_env_token_works():
    pw_p, tok_p, dev_p = _fresh_paths("env")
    pw_p.write_text("pass")
    whoami_calls, login_calls = _patch_http(
        whoami_responses={
            "ENV_TOK": {"user_id": "@u:t", "device_id": "DEV_A"},
        },
        login_responses=[],
    )
    out = asyncio.run(approver._resolve_credentials(
        "ENV_TOK", "u", pw_p, tok_p, dev_p, "u"))
    assert out == ("ENV_TOK", "DEV_A", "@u:t", False, False), out
    assert login_calls == [], "should not /login when env token works"


def test_env_stale_cached_works():
    pw_p, tok_p, dev_p = _fresh_paths("cached")
    pw_p.write_text("pass")
    tok_p.write_text("CACHED_TOK")
    dev_p.write_text("DEV_A")
    whoami_calls, login_calls = _patch_http(
        whoami_responses={
            "ENV_TOK": None,  # stale
            "CACHED_TOK": {"user_id": "@u:t", "device_id": "DEV_A"},
        },
        login_responses=[],
    )
    out = asyncio.run(approver._resolve_credentials(
        "ENV_TOK", "u", pw_p, tok_p, dev_p, "u"))
    # was_reminted=False, device_changed=False (DEV_A == cached DEV_A)
    assert out == ("CACHED_TOK", "DEV_A", "@u:t", False, False), out
    assert login_calls == [], "should not /login when cached token works"


def test_both_stale_login_reuses_device():
    pw_p, tok_p, dev_p = _fresh_paths("relogin")
    pw_p.write_text("pass")
    tok_p.write_text("CACHED_TOK")
    dev_p.write_text("DEV_A")
    _wc, login_calls = _patch_http(
        whoami_responses={
            "ENV_TOK": None,
            "CACHED_TOK": None,  # also stale
        },
        login_responses=[("FRESH_TOK", "DEV_A", "@u:t")],
    )
    out = asyncio.run(approver._resolve_credentials(
        "ENV_TOK", "u", pw_p, tok_p, dev_p, "u"))
    assert out == ("FRESH_TOK", "DEV_A", "@u:t", True, False), out
    assert login_calls == [("u", "DEV_A")], \
        f"expected /login with cached device_id; got {login_calls}"
    # Newly minted token + device persisted to /data
    assert tok_p.read_text() == "FRESH_TOK"
    assert dev_p.read_text() == "DEV_A"


def test_first_run_no_cache_login_fresh():
    pw_p, tok_p, dev_p = _fresh_paths("first")
    pw_p.write_text("pass")
    # no tok_p, no dev_p
    _wc, login_calls = _patch_http(
        whoami_responses={"ENV_TOK": None},
        login_responses=[("FRESH_TOK", "NEW_DEV", "@u:t")],
    )
    out = asyncio.run(approver._resolve_credentials(
        "ENV_TOK", "u", pw_p, tok_p, dev_p, "u"))
    # was_reminted=True, device_changed=False (no cached device — first run isn't a "change")
    assert out == ("FRESH_TOK", "NEW_DEV", "@u:t", True, False), out
    assert login_calls == [("u", None)], login_calls


def test_login_with_cached_device_fails_retries_fresh():
    pw_p, tok_p, dev_p = _fresh_paths("retry")
    pw_p.write_text("pass")
    tok_p.write_text("CACHED_TOK")
    dev_p.write_text("STALE_DEV")
    _wc, login_calls = _patch_http(
        whoami_responses={
            "ENV_TOK": None,
            "CACHED_TOK": None,
        },
        login_responses=[
            (None, None, None),                # 1st login w/ device fails
            ("FRESH_TOK", "NEW_DEV", "@u:t"),  # 2nd login fresh succeeds
        ],
    )
    out = asyncio.run(approver._resolve_credentials(
        "ENV_TOK", "u", pw_p, tok_p, dev_p, "u"))
    # device DID change: cached=STALE_DEV, current=NEW_DEV → device_changed=True
    assert out == ("FRESH_TOK", "NEW_DEV", "@u:t", True, True), out
    assert login_calls == [("u", "STALE_DEV"), ("u", None)], login_calls


def test_no_password_no_env_returns_none():
    pw_p, tok_p, dev_p = _fresh_paths("nopass")
    # password file doesn't exist
    _wc, login_calls = _patch_http(
        whoami_responses={"ENV_TOK": None},
        login_responses=[],
    )
    out = asyncio.run(approver._resolve_credentials(
        "ENV_TOK", "u", pw_p, tok_p, dev_p, "u"))
    assert out == (None, None, None, False, False), out
    assert login_calls == [], "should not /login without a password file"


def test_device_changed_helper():
    assert not approver._device_changed(None, "X")    # first run isn't a change
    assert not approver._device_changed("", "X")      # empty cache isn't a change
    assert not approver._device_changed("X", "X")     # same is not a change
    assert approver._device_changed("X", "Y")         # different is a change
    assert not approver._device_changed("X", None)    # missing current isn't a change


if __name__ == "__main__":
    test_env_token_works();                           print("ok: env_token_works")
    test_env_stale_cached_works();                    print("ok: env_stale_cached_works")
    test_both_stale_login_reuses_device();            print("ok: both_stale_login_reuses_device")
    test_first_run_no_cache_login_fresh();            print("ok: first_run_no_cache_login_fresh")
    test_login_with_cached_device_fails_retries_fresh(); print("ok: login_with_cached_device_fails_retries_fresh")
    test_no_password_no_env_returns_none();           print("ok: no_password_no_env_returns_none")
    test_device_changed_helper();                     print("ok: device_changed_helper")
    print("all tests passed")
