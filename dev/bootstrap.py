#!/usr/bin/env python3
"""Bootstrap a local continuwuity dev environment for shape-rotator-matrix.

Prereq: dev/docker-compose.yml is up (continuwuity reachable at localhost:46167,
registration enabled with token "dev-token").

What this does:
  1. Creates an admin user (idempotent — reuses an existing one if present)
  2. Creates a Shape Rotator space + three child rooms (general, announcements,
     bot-noise) with the same join rules as prod (space=knock, children=restricted)
  3. Seeds a test signup code and a test knock code
  4. Prints shell export lines you can `eval` to run the approver locally

The local stack is a dev convenience, not a production mirror. Matrix server
name is `localhost:46167`, federation off, no TLS.

Run:
  python3 dev/bootstrap.py               # prints exports
  eval "$(python3 dev/bootstrap.py)"     # sets env vars in your current shell
  python3 knock-approver/approver.py     # approver connects to the local stack

Idempotency: state is persisted in dev/.dev-state.json so re-running this
reuses the admin + space + codes.
"""
import json, os, re, secrets, subprocess, sys, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

HS = os.environ.get("DEV_HS", "http://localhost:46167").rstrip("/")
REG_TOKEN = os.environ.get("CONDUWUIT_REGISTRATION_TOKEN", "dev-token")
STATE = Path(os.environ.get("DEV_STATE_PATH",
                            str(Path(__file__).parent / ".dev-state.json")))
# Pre-supplied bootstrap token (e.g. CI scrapes from `docker logs` on the host
# and passes it in here so the bootstrap container doesn't need docker access).
ENV_BOOTSTRAP_TOKEN = os.environ.get("CONDUWUIT_BOOTSTRAP_TOKEN", "").strip()

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "dev-admin-password-longenough"

SPACE_NAME = "Shape Rotator (dev)"
SPACE_ALIAS = "shape-rotator-dev"
CHILDREN = [
    ("general",       "Shape Rotator - General", "general chat",        True),
    ("announcements", "Announcements",           "broadcast",           True),
    ("bot-noise",     "Bot Noise",               "agent playground",    False),
]


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
    except urllib.error.URLError as e:
        print(f"cannot reach {HS}: {e}", file=sys.stderr)
        print(f"is `docker compose up -d` running in dev/?", file=sys.stderr)
        sys.exit(2)


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {}

def save_state(state):
    STATE.write_text(json.dumps(state, indent=2, sort_keys=True))


def read_bootstrap_token():
    """Continuwuity prints a one-time registration token on first boot, before
    the configured CONDUWUIT_REGISTRATION_TOKEN takes effect. Either pass it
    in via CONDUWUIT_BOOTSTRAP_TOKEN env (CI / containerised case where
    docker isn't available) or scrape it from container logs (local dev)."""
    if ENV_BOOTSTRAP_TOKEN:
        return ENV_BOOTSTRAP_TOKEN
    try:
        logs = subprocess.run(
            ["docker", "logs", "dev-continuwuity-1"],
            capture_output=True, text=True, timeout=10,
        ).stdout + subprocess.run(
            ["docker", "logs", "dev-continuwuity-1"],
            capture_output=True, text=True, timeout=10,
        ).stderr
    except Exception as e:
        return None
    m = re.search(r"using the registration token (?:\x1b\[[0-9;]*m)?([A-Za-z0-9]+)", logs)
    return m.group(1) if m else None


def register(username, password, token_override=None):
    """Register via the m.login.registration_token flow. Returns (mxid, token)."""
    _, init = http("POST", f"{HS}/_matrix/client/v3/register", body={})
    session = init.get("session")
    if not session:
        raise RuntimeError(f"register init: {init}")
    status, r = http("POST", f"{HS}/_matrix/client/v3/register", body={
        "auth": {"type": "m.login.registration_token",
                 "token": token_override or REG_TOKEN, "session": session},
        "username": username,
        "password": password,
    })
    if status == 400 and r.get("errcode") == "M_USER_IN_USE":
        return None  # caller should try login
    if status != 200:
        raise RuntimeError(f"register: {status} {r}")
    return r["user_id"], r["access_token"]


def login(username, password):
    status, r = http("POST", f"{HS}/_matrix/client/v3/login", body={
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": username},
        "password": password,
    })
    if status != 200:
        raise RuntimeError(f"login: {status} {r}")
    return r["user_id"], r["access_token"]


def ensure_admin(state):
    if state.get("admin_token"):
        # Verify still valid
        status, _ = http("GET", f"{HS}/_matrix/client/v3/account/whoami", token=state["admin_token"])
        if status == 200:
            return state["admin_mxid"], state["admin_token"]

    # First try login (admin already exists from a previous bootstrap run)
    try:
        mxid, token = login(ADMIN_USERNAME, ADMIN_PASSWORD)
        state["admin_mxid"], state["admin_token"] = mxid, token
        return mxid, token
    except RuntimeError:
        pass

    # Otherwise, register — continuwuity requires the first user to use the
    # one-time bootstrap token printed in the container logs, not the
    # configured token.
    boot_token = read_bootstrap_token()
    if not boot_token:
        raise RuntimeError(
            "Could not find a continuwuity bootstrap registration token in "
            "the container logs. Check `docker logs dev-continuwuity-1`.")
    result = register(ADMIN_USERNAME, ADMIN_PASSWORD, token_override=boot_token)
    if not result:
        # Hit M_USER_IN_USE but login also failed — odd state.
        raise RuntimeError("admin user exists but login failed")
    mxid, token = result
    state["admin_mxid"] = mxid
    state["admin_token"] = token
    return mxid, token


def create_room(token, name, topic, alias, is_space=False, encrypted=False, pl_users=None):
    body = {
        "name": name,
        "topic": topic,
        "preset": "public_chat",
        "room_alias_name": alias,
        "power_level_content_override": {
            "users": pl_users or {},
            "events_default": 0,
        },
        "initial_state": [],
    }
    if is_space:
        body["creation_content"] = {"type": "m.space"}
    if encrypted:
        body["initial_state"].append({
            "type": "m.room.encryption",
            "state_key": "",
            "content": {"algorithm": "m.megolm.v1.aes-sha2"},
        })
    status, r = http("POST", f"{HS}/_matrix/client/v3/createRoom", token=token, body=body)
    if status != 200:
        raise RuntimeError(f"createRoom {name}: {status} {r}")
    return r["room_id"]


def put_state(token, room_id, state_type, content):
    url = f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/state/{state_type}"
    status, r = http("PUT", url, token=token, body=content)
    if status != 200:
        raise RuntimeError(f"state {state_type} on {room_id}: {status} {r}")


def ensure_space(state, admin_mxid, admin_token):
    if state.get("space_id"):
        status, _ = http(
            "GET",
            f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(state['space_id'])}/state/m.room.create",
            token=admin_token)
        if status == 200:
            return state["space_id"], state["child_ids"]

    space_id = create_room(
        admin_token,
        SPACE_NAME, "local dev space", SPACE_ALIAS,
        is_space=True, encrypted=False,
        pl_users={admin_mxid: 100},
    )
    # Space -> knock
    put_state(admin_token, space_id, "m.room.join_rules", {"join_rule": "knock"})

    child_ids = []
    for alias, name, topic, suggested in CHILDREN:
        rid = create_room(
            admin_token, name, topic, f"{alias}-dev",
            is_space=False, encrypted=True,
            pl_users={admin_mxid: 100},
        )
        # Link child to space
        put_state(admin_token, space_id, "m.space.child",
                  {"via": ["localhost:46167"], "suggested": suggested, "auto_join": True})
        # Actually m.space.child uses state_key = child room_id, so redo:
        url = (f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(space_id)}"
               f"/state/m.space.child/{urllib.parse.quote(rid)}")
        http("PUT", url, token=admin_token, body={
            "via": ["localhost:46167"], "suggested": suggested, "auto_join": True,
        })
        # Children -> restricted (space members auto-join)
        put_state(admin_token, rid, "m.room.join_rules", {
            "join_rule": "restricted",
            "allow": [{"type": "m.room_membership", "room_id": space_id}],
        })
        child_ids.append(rid)

    state["space_id"] = space_id
    state["child_ids"] = child_ids
    return space_id, child_ids


def ensure_codes(state):
    if not state.get("signup_code"):
        state["signup_code"] = "dev-" + secrets.token_hex(6)
    if not state.get("knock_code"):
        state["knock_code"] = "dev-" + secrets.token_hex(6)
    for i in (1, 2, 3):
        if not state.get(f"welcome_code_{i}"):
            state[f"welcome_code_{i}"] = "dev-welcome-" + secrets.token_hex(6)
    # Persisted like the others: `docker compose run` re-runs this one-shot
    # bootstrap (test-runner depends_on it), and a fresh-per-run value would
    # mismatch the code the already-running approver seeded from the FIRST
    # run's INITIAL_CODES.
    if not state.get("welcome_single"):
        state["welcome_single"] = "dev-welcome-single-" + secrets.token_hex(4)
    return (state["signup_code"], state["knock_code"],
            state["welcome_code_1"], state["welcome_code_2"],
            state["welcome_code_3"], state["welcome_single"])


def main():
    state = load_state()
    admin_mxid, admin_token = ensure_admin(state)
    space_id, child_ids = ensure_space(state, admin_mxid, admin_token)
    (signup_code, knock_code, welcome_code_1, welcome_code_2,
     welcome_code_3, welcome_single) = ensure_codes(state)
    # Fixed string that always tests the code_exhausted branch deterministically.
    welcome_dead = "dev-welcome-dead"
    save_state(state)

    signup_seed = json.dumps({signup_code: {"uses_remaining": 99, "label": "dev"}})
    welcome_seed = json.dumps({
        knock_code:     {"uses_remaining": 99, "label": "dev"},
        welcome_code_1: {"uses_remaining": 99, "label": "dev welcome 1"},
        welcome_code_2: {"uses_remaining": 99, "label": "dev welcome 2"},
        welcome_code_3: {"uses_remaining": 99, "label": "dev welcome 3"},
        welcome_single: {"uses_remaining": 1,  "label": "dev welcome single-use"},
        welcome_dead:   {"uses_remaining": 0,  "label": "dev welcome exhausted"},
    })

    # Print exports on stderr so users who `eval` the stdout get only exports.
    def echo(line):
        print(line)

    echo(f"# shape-rotator-matrix dev env — eval me")
    echo(f"export HS={HS!r}")
    echo(f"export HS_PUBLIC={HS!r}")
    echo(f"export MATRIX_TOKEN={admin_token!r}")
    echo(f"export ADMIN_TOKEN={admin_token!r}")
    echo(f"export ADMIN_MXID={admin_mxid!r}")
    # Use whatever form createRoom returned. Continuwuity is inconsistent
    # about whether room IDs come back with or without the `:server` suffix
    # — /invite rejects the wrong form with a misleading error. Trust the
    # server's returned string; don't normalize.
    echo(f"export SPACE_ID={space_id!r}")
    echo(f"export SPACE_CHILD_IDS={','.join(child_ids)!r}")
    # Designate the last (encrypted) child room as the admin command room
    # for tests. The migrated mautrix-based bot decrypts admin commands
    # there; admin_e2ee.py exercises that path.
    if child_ids:
        echo(f"export ADMIN_COMMAND_ROOM={child_ids[-1]!r}")
    echo(f"export CONDUWUIT_REGISTRATION_TOKEN={REG_TOKEN!r}")
    echo(f"export ONBOARDING_INVITER_MXID={admin_mxid!r}")
    echo(f"export INITIAL_CODES={welcome_seed!r}")
    echo(f"export INITIAL_SIGNUP_CODES={signup_seed!r}")
    echo(f"export DEV_SIGNUP_CODE={signup_code!r}")
    echo(f"export DEV_KNOCK_CODE={knock_code!r}")
    echo(f"export DEV_WELCOME_CODE={welcome_code_1!r}")
    echo(f"export DEV_WELCOME_CODE_2={welcome_code_2!r}")
    echo(f"export DEV_WELCOME_CODE_3={welcome_code_3!r}")
    echo(f"export DEV_WELCOME_SINGLE={welcome_single!r}")
    echo(f"export DEV_WELCOME_DEAD={welcome_dead!r}")
    echo(f"# Admin MXID: {admin_mxid}")
    echo(f"# Space ID:   {space_id}")
    echo(f"# Children:   {' '.join(child_ids)}")


if __name__ == "__main__":
    main()
