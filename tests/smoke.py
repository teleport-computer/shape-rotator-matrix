#!/usr/bin/env python3
"""End-to-end smoke test for shape-rotator-matrix.

Runs against a configured homeserver (default: the deployed prod instance) and
verifies the onboarding paths end-to-end:

  1. POST /signup/api -> new account, all "steps" true, user is joined to
     space + all children, and the DM room to the configured inviter exists.
  2. /join?code=... flow -> knock with code as reason; approver auto-approves
     within a few seconds; new user ends up invited.
  3. Welcome-room flow (issue #3) -> POST /join/api maps a code to one public
     #welcome-… room (idempotent, not consumed by the POST); joining the room
     consumes the use, posts the confirmation and invites the joiner to the
     space; duplicate joins by outsiders get nothing; after cleanup the
     single-use code is exhausted.

Leaves the homeserver in its original state by kicking both test users at
the end. Uses stdlib only; run with plain `python3 tests/smoke.py`.

Env:
  HOMESERVER       default https://mtrx.shaperotator.xyz
  ADMIN_TOKEN      required — used to kick test users after the test
  SIGNUP_CODE      required — a signup code with >= 1 use remaining
  KNOCK_CODE       required — a knock (invite) code with >= 1 use remaining
  WELCOME_CODE     required — a code with >= 1 use for the welcome-room path
  WELCOME_SINGLE   required — a code with exactly 1 use (consumed by the test)
  WELCOME_DEAD     required — a code with 0 uses (code_exhausted branch)
  REG_TOKEN        required — continuwuity server-wide registration token
                   (used only for the knock path, to create the federated
                   "guest" account that does the knocking)
  SPACE_ID         default !4FL8uL5OEYLATG1VH4wC2CD3pfIV6BMFId9VT7rmm-g
  SPACE_CHILDREN   comma-separated, default: the three prod children

Exit code 0 on all-pass, nonzero otherwise.
"""
import json, os, secrets, sys, time, urllib.error, urllib.parse, urllib.request

HS            = os.environ.get("HOMESERVER", "https://mtrx.shaperotator.xyz").rstrip("/")
ADMIN_TOKEN   = os.environ.get("ADMIN_TOKEN", "")
SIGNUP_CODE   = os.environ.get("SIGNUP_CODE", "")
KNOCK_CODE    = os.environ.get("KNOCK_CODE", "")
WELCOME_CODE  = os.environ.get("WELCOME_CODE", "")
WELCOME_SINGLE = os.environ.get("WELCOME_SINGLE", "")
WELCOME_DEAD  = os.environ.get("WELCOME_DEAD", "")
REG_TOKEN     = os.environ.get("REG_TOKEN", "")
SPACE_ID      = os.environ.get("SPACE_ID", "!4FL8uL5OEYLATG1VH4wC2CD3pfIV6BMFId9VT7rmm-g")
SPACE_CHILDREN = [c.strip() for c in os.environ.get(
    "SPACE_CHILDREN",
    "!z85RFatK8w0f04i8yVOCidnYRKXlZuRjK4kYkdXVhUc,"
    "!9p9ZAr8CFo8WjD8g0hKv_1sOewNWt0zTBCWMAkWnLxo,"
    "!a8L-8zCDgQZhddUWkb4FYkCVjPBu0lY6QwtLVBXIRXc"
).split(",") if c.strip()]

results = []

def log(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    results.append((name, ok))

def http(method, url, token=None, body=None, timeout=15):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"_raw": raw.decode("utf-8", errors="replace")}

def admin_kick(mxid, rooms):
    for rid in rooms:
        http("POST",
             f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(rid)}/kick",
             token=ADMIN_TOKEN,
             body={"user_id": mxid, "reason": "smoke-test cleanup"})


# --- Path A: signup ---

def test_signup_path():
    username = f"smoke_signup_{int(time.time())}_{secrets.token_hex(2)}"
    print(f"\n[signup] username={username}", flush=True)

    status, r = http("POST", f"{HS}/signup/api", body={
        "code":         SIGNUP_CODE,
        "username":     username,
        "password":     secrets.token_urlsafe(32),
        "display_name": "smoke test",
        "intro":        "automated smoke test — please ignore",
    })
    log("signup returns 200", status == 200, f"status={status} body={r}")
    if status != 200:
        return None

    mxid  = r.get("user_id", "")
    token = r.get("access_token", "")
    steps = r.get("steps", {})
    log("register step",       steps.get("register") is True)
    log("space_invited step",  steps.get("space_invited") is True)
    log("space_joined step",   steps.get("space_joined") is True)
    log("inviter_dm step",     steps.get("inviter_dm") is True)
    children_joined = steps.get("children_joined") or []
    log(f"children_joined ({len(children_joined)}/{len(SPACE_CHILDREN)})",
        len(children_joined) == len(SPACE_CHILDREN))
    log("dm_room returned",    bool(r.get("dm_room")))

    # Verify from the new user's own sync
    time.sleep(1)
    _s, sync = http("GET", f"{HS}/_matrix/client/v3/sync?timeout=0", token=token)
    joined = list(sync.get("rooms", {}).get("join", {}).keys())
    log(f"user sees space in join set",
        any(rid.split(":")[0] == SPACE_ID.split(":")[0] for rid in joined))
    log(f"user joined all {len(SPACE_CHILDREN)} children",
        all(any(rid.split(":")[0] == c.split(":")[0] for rid in joined)
            for c in SPACE_CHILDREN))
    log("user sees DM room", any(rid == r.get("dm_room") for rid in joined))

    return mxid


# --- Path B: knock -> vetting room -> haiku captcha -> space invite ---

import re

def _wait_for_invite(token, predicate, timeout=15):
    """Poll /sync until an invited room matches predicate(rid). Returns rid or None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        _s, sync = http("GET", f"{HS}/_matrix/client/v3/sync?timeout=0", token=token)
        for rid in sync.get("rooms", {}).get("invite", {}).keys():
            if predicate(rid):
                return rid
        time.sleep(1)
    return None


def test_knock_path():
    # Register a throwaway account on THIS server to simulate a federated guest.
    username = f"smoke_knock_{int(time.time())}_{secrets.token_hex(2)}"
    print(f"\n[knock] test account: {username}", flush=True)

    _s, init = http("POST", f"{HS}/_matrix/client/v3/register", body={})
    session = init.get("session")
    if not session:
        log("register init", False, f"body={init}")
        return None

    status, r = http("POST", f"{HS}/_matrix/client/v3/register", body={
        "auth": {"type": "m.login.registration_token",
                 "token": REG_TOKEN, "session": session},
        "username": username,
        "password": secrets.token_urlsafe(32),
    })
    log("knock-test account registered", status == 200, f"status={status}")
    if status != 200:
        return None
    token = r["access_token"]
    mxid  = r["user_id"]

    # _vet requires a non-empty displayname; set one before knocking.
    http("PUT", f"{HS}/_matrix/client/v3/profile/{urllib.parse.quote(mxid)}/displayname",
         token=token, body={"displayname": f"smoke-{secrets.token_hex(2)}"})

    status, _ = http(
        "POST",
        f"{HS}/_matrix/client/v3/knock/{urllib.parse.quote(SPACE_ID)}",
        token=token, body={"reason": KNOCK_CODE})
    log("knock posted", status == 200, f"status={status}")

    # Approver should invite to a NEW per-knock vetting room (not the space).
    space_prefix = SPACE_ID.split(":")[0]
    vetting_room = _wait_for_invite(token, lambda rid: rid.split(":")[0] != space_prefix)
    log("vetting room invite within 15s", bool(vetting_room),
        f"room={vetting_room}")
    if not vetting_room:
        return [mxid]

    status, _ = http("POST",
                     f"{HS}/_matrix/client/v3/join/{urllib.parse.quote(vetting_room)}",
                     token=token, body={})
    log("joined vetting room", status == 200, f"status={status}")

    # Pull the challenge — server posts it after createRoom, but the bot's
    # message may take a beat to be visible to a fresh joiner.
    keyword = None
    deadline = time.time() + 10
    while time.time() < deadline:
        _s, sync = http("GET", f"{HS}/_matrix/client/v3/sync?timeout=0", token=token)
        joined = sync.get("rooms", {}).get("join", {}).get(vetting_room, {})
        for ev in joined.get("timeline", {}).get("events", []):
            if ev.get("type") != "m.room.message":
                continue
            body = (ev.get("content") or {}).get("body", "")
            m = re.search(r'include the word "([^"]+)"', body)
            if m:
                keyword = m.group(1)
                break
        if keyword:
            break
        time.sleep(1)
    log("challenge keyword visible", bool(keyword), f"keyword={keyword!r}")
    if not keyword:
        return [mxid]

    haiku = f"silent {keyword} hum\nfloating in the morning fog\nspring wind blowing through"
    status, _ = http(
        "PUT",
        f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(vetting_room)}"
        f"/send/m.room.message/smoke-haiku-{int(time.time())}",
        token=token, body={"msgtype": "m.text", "body": haiku})
    log("haiku sent", status == 200, f"status={status}")

    # Now wait for the actual space invite.
    space_invite = _wait_for_invite(token, lambda rid: rid.split(":")[0] == space_prefix)
    log("space invite after vetting (within 15s)", bool(space_invite))

    # Bad-code path: knock with a bogus code; nothing should ever be invited.
    username2 = f"smoke_badcode_{int(time.time())}_{secrets.token_hex(2)}"
    _s, init = http("POST", f"{HS}/_matrix/client/v3/register", body={})
    sess2 = init["session"]
    _s, r2 = http("POST", f"{HS}/_matrix/client/v3/register", body={
        "auth": {"type": "m.login.registration_token",
                 "token": REG_TOKEN, "session": sess2},
        "username": username2,
        "password": secrets.token_urlsafe(32),
    })
    bad_token = r2["access_token"]
    bad_mxid  = r2["user_id"]
    http("POST",
         f"{HS}/_matrix/client/v3/knock/{urllib.parse.quote(SPACE_ID)}",
         token=bad_token, body={"reason": "definitely-not-a-real-code-" + secrets.token_hex(4)})
    time.sleep(8)
    _s, sync = http("GET", f"{HS}/_matrix/client/v3/sync?timeout=0", token=bad_token)
    invites = list(sync.get("rooms", {}).get("invite", {}).keys())
    log("bad code: no invites at all", len(invites) == 0,
        f"invites={invites}")

    return [mxid, bad_mxid]


# --- Path C: welcome rooms (/join/api → public room Join → space invite) ---

def _register_throwaway(prefix):
    """Register a throwaway local account. Returns (mxid, token) or (None, None)."""
    username = f"{prefix}_{int(time.time())}_{secrets.token_hex(2)}"
    _s, init = http("POST", f"{HS}/_matrix/client/v3/register", body={})
    session = init.get("session")
    if not session:
        log(f"[{prefix}] register init", False, f"body={init}")
        return None, None
    status, r = http("POST", f"{HS}/_matrix/client/v3/register", body={
        "auth": {"type": "m.login.registration_token",
                 "token": REG_TOKEN, "session": session},
        "username": username,
        "password": secrets.token_urlsafe(32),
    })
    log(f"[{prefix}] account registered", status == 200, f"status={status}")
    if status != 200:
        return None, None
    return r["user_id"], r["access_token"]


def _room_message_bodies(token, room_id):
    _s, sync = http("GET", f"{HS}/_matrix/client/v3/sync?timeout=0", token=token)
    room = sync.get("rooms", {}).get("join", {}).get(room_id, {})
    evs = (room.get("timeline", {}).get("events", [])
           + room.get("state", {}).get("events", []))
    return [(ev.get("content") or {}).get("body", "") for ev in evs
            if ev.get("type") == "m.room.message"]


def test_welcome_path():
    print("\n[welcome] welcome-room path", flush=True)
    to_cleanup = []

    # Error branches — distinct errors, and neither mints a room.
    s, j = http("POST", f"{HS}/join/api",
                body={"code": "definitely-not-a-code-" + secrets.token_hex(4)})
    log("unknown code -> invalid_code",
        s == 403 and j.get("error") == "invalid_code",
        f"status={s} body={j}")
    s, j = http("POST", f"{HS}/join/api", body={})
    log("missing code -> invalid_code",
        s == 403 and j.get("error") == "invalid_code",
        f"status={s} body={j}")
    s, j = http("POST", f"{HS}/join/api", body={"code": WELCOME_DEAD})
    log("exhausted code -> code_exhausted",
        s == 403 and j.get("error") == "code_exhausted",
        f"status={s} body={j}")

    # Idempotent mint of a SINGLE-USE code: two POSTs return the same alias,
    # and the code still works afterwards — POSTing never consumes a use.
    s, j = http("POST", f"{HS}/join/api", body={"code": WELCOME_SINGLE})
    log("single-use code mints #welcome- room",
        s == 200 and j.get("room_alias", "").startswith("#welcome-"),
        f"status={s} body={j}")
    if s != 200:
        return to_cleanup
    alias = j["room_alias"]
    s, j2 = http("POST", f"{HS}/join/api", body={"code": WELCOME_SINGLE})
    log("second POST returns the same alias (idempotent)",
        s == 200 and j2.get("room_alias") == alias,
        f"status={s} body={j2}")

    mxid, token = _register_throwaway("smoke_welcome")
    if not mxid:
        return to_cleanup
    to_cleanup.append(mxid)

    _s, dirr = http("GET", f"{HS}/_matrix/client/v3/directory/room/"
                      f"{urllib.parse.quote(alias)}")
    room_id = dirr.get("room_id")
    log("alias resolves to a room", bool(room_id), f"dir={dirr}")

    # The one Element UI step: a plain public-room join via the alias.
    s, _ = http("POST", f"{HS}/_matrix/client/v3/join/"
                 f"{urllib.parse.quote(alias)}", token=token, body={})
    log("user joins welcome room via alias", s == 200, f"status={s} alias={alias}")

    space_prefix = SPACE_ID.split(":")[0]
    invited = _wait_for_invite(
        token, lambda rid: rid.split(":")[0] == space_prefix, timeout=15)
    log("space invite after welcome-room join (within 15s)", bool(invited))

    confirm = "invite sent — accept it in Element and you're in."
    bodies = _room_message_bodies(token, room_id)
    log("confirmation message posted in welcome room",
        any(confirm in b for b in bodies), f"bodies={bodies}")

    if invited:
        s, _ = http("POST", f"{HS}/_matrix/client/v3/rooms/"
                     f"{urllib.parse.quote(SPACE_ID)}/join", token=token, body={})
        log("accepted space invite", s == 200, f"status={s}")
        for child in SPACE_CHILDREN:
            http("POST", f"{HS}/_matrix/client/v3/rooms/"
                 f"{urllib.parse.quote(child)}/join", token=token, body={})
        time.sleep(1)
        _s, sync = http("GET", f"{HS}/_matrix/client/v3/sync?timeout=0", token=token)
        joined = list(sync.get("rooms", {}).get("join", {}).keys())
        log(f"joined all {len(SPACE_CHILDREN)} children via restricted rule",
            all(any(rid.split(":")[0] == c.split(":")[0] for rid in joined)
                for c in SPACE_CHILDREN))

    # Duplicate join in a live room: an outsider with no code of their own
    # joins the already-consumed room and must get NOTHING (exactly-once).
    alias2 = None
    s, j = http("POST", f"{HS}/join/api", body={"code": WELCOME_CODE})
    log("multi-use code mints #welcome- room",
        s == 200 and j.get("room_alias", "").startswith("#welcome-"),
        f"status={s} body={j}")
    if s == 200:
        alias2 = j["room_alias"]
        u1, t1 = _register_throwaway("smoke_wel_a")
        u2, t2 = _register_throwaway("smoke_wel_b")
        if u1:
            to_cleanup.append(u1)
        if u2:
            to_cleanup.append(u2)
        s1, _ = http("POST", f"{HS}/_matrix/client/v3/join/"
                      f"{urllib.parse.quote(alias2)}", token=t1, body={})
        log("[outsider-test] first user joins second room", s1 == 200,
            f"status={s1}")
        got1 = _wait_for_invite(
            t1, lambda rid: rid.split(":")[0] == space_prefix, timeout=15)
        log("[outsider-test] first user got the space invite", bool(got1))
        s2, _ = http("POST", f"{HS}/_matrix/client/v3/join/"
                      f"{urllib.parse.quote(alias2)}", token=t2, body={})
        log("[outsider-test] second user joins the SAME room", s2 == 200,
            f"status={s2}")
        time.sleep(8)
        _s, sync = http("GET", f"{HS}/_matrix/client/v3/sync?timeout=0", token=t2)
        invites = list(sync.get("rooms", {}).get("invite", {}).keys())
        log("[outsider-test] duplicate joiner gets no invite at all",
            len(invites) == 0, f"invites={invites}")

    # The join consumed the single use: every later POST for that code says
    # code_exhausted (the codes check precedes the idempotent mapping lookup).
    s, j = http("POST", f"{HS}/join/api", body={"code": WELCOME_SINGLE})
    log("single-use code exhausted after the join (consumed exactly once)",
        s == 403 and j.get("error") == "code_exhausted",
        f"status={s} body={j}")

    # Cleanup reap (CI runs a short WELCOME_JOINED_TTL): the room is kicked +
    # tombstoned and its mapping deleted. Poll until the joiner is gone from
    # the room, then confirm a re-mint: a code with uses left whose room died
    # gets a FRESH room on the next POST (the liveness check sees the
    # tombstone).
    kicked = False
    deadline = time.time() + 120
    while time.time() < deadline:
        _s, sync = http("GET", f"{HS}/_matrix/client/v3/sync?timeout=0", token=token)
        joined = list(sync.get("rooms", {}).get("join", {}).keys())
        if not any(rid == room_id for rid in joined):
            kicked = True
            break
        time.sleep(3)
    log("reaped: joiner kicked out of the welcome room", kicked)

    # A code with uses left whose room died gets a FRESH room on the next
    # POST (the liveness check sees the tombstone). The alias is
    # deterministic per code, so detect the re-mint by the room_id the
    # alias now resolves to. Poll: the multi-use room is reaped a few
    # seconds after the single one (it was joined later).
    _s, dirr = http("GET", f"{HS}/_matrix/client/v3/directory/room/"
                      f"{urllib.parse.quote(alias2)}")
    old_room2 = dirr.get("room_id")
    fresh = False
    new_room = None
    deadline = time.time() + 120
    while time.time() < deadline:
        s, j = http("POST", f"{HS}/join/api", body={"code": WELCOME_CODE})
        if s == 200:
            _s, dirr = http("GET", f"{HS}/_matrix/client/v3/directory/room/"
                              f"{urllib.parse.quote(alias2)}")
            if dirr.get("room_id") != old_room2:
                fresh = True
                new_room = dirr.get("room_id")
                break
        time.sleep(3)
    log("re-mint after reap: fresh room for a code with uses left", fresh,
        f"old_room={old_room2} new_room={new_room} last_status={s}")

    return to_cleanup


def main():
    missing = [k for k in ("ADMIN_TOKEN", "SIGNUP_CODE", "KNOCK_CODE", "REG_TOKEN",
                           "WELCOME_CODE", "WELCOME_SINGLE", "WELCOME_DEAD")
               if not globals()[k]]
    if missing:
        print(f"missing env vars: {missing}", file=sys.stderr)
        return 2

    print(f"smoke test against {HS}", flush=True)

    to_cleanup = []
    signup_mxid = test_signup_path()
    if signup_mxid:
        to_cleanup.append(signup_mxid)
    knock_mxids = test_knock_path()
    if knock_mxids:
        to_cleanup.extend(knock_mxids)
    welcome_mxids = test_welcome_path()
    if welcome_mxids:
        to_cleanup.extend(welcome_mxids)

    # Cleanup
    if to_cleanup:
        print(f"\n[cleanup] kicking {len(to_cleanup)} test user(s)", flush=True)
        for mxid in to_cleanup:
            admin_kick(mxid, [SPACE_ID] + SPACE_CHILDREN)

    failed = [n for n, ok in results if not ok]
    print(f"\n=== {len(results) - len(failed)}/{len(results)} passed ===", flush=True)
    if failed:
        print(f"failed: {failed}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
