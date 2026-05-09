#!/usr/bin/env python3
"""Bulk-redact a contiguous flood of messages by a single sender in one room.

Logs in as --user with --password-file (so it gets its own ephemeral
device), paginates the room backward, filters to events from --user with
origin_server_ts in [--since-iso, --until-iso], and redacts each. Skips
already-redacted events.

Run --dry-run first; nothing destructive happens. Then re-run with
--apply (and optionally --limit N to take it in stages).

Example (May 9 announce-flood cleanup):

  python3 deploy/admin/bulk_redact.py \\
      --hs https://mtrx.shaperotator.xyz \\
      --user shape-rotator-2 \\
      --password-file /tmp/sr2-pass.txt \\
      --room '!amfRBsRwYJl45PGqxnRjh0t1oxj0pOPF1v_-07GdJcE' \\
      --since-iso 2026-05-09T11:30:00Z \\
      --until-iso 2026-05-09T15:17:00Z \\
      --dry-run
"""
import argparse, datetime, json, secrets, sys, time, urllib.parse
import urllib.request


def http(method, url, headers=None, body=None):
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, json.loads(r.read())


def login(hs, user, password):
    body = json.dumps({
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": user},
        "password": password,
        "initial_device_display_name": "bulk_redact-tool",
    })
    status, resp = http("POST", f"{hs}/_matrix/client/v3/login",
                        {"Content-Type": "application/json"}, body)
    if status != 200:
        raise SystemExit(f"login failed: {status} {resp}")
    return resp["access_token"], resp["device_id"], resp["user_id"]


def logout(hs, token):
    try:
        http("POST", f"{hs}/_matrix/client/v3/logout",
             {"Authorization": f"Bearer {token}"})
    except Exception as e:
        print(f"warn: logout failed: {e}", file=sys.stderr)


def parse_iso(s):
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.datetime.fromisoformat(s).timestamp() * 1000


def is_redacted(ev):
    return bool((ev.get("unsigned") or {}).get("redacted_because"))


def collect_targets(hs, token, room, sender_mxid, since_ms, until_ms,
                   max_pages=2000):
    """Walk /messages backward from now until origin_server_ts < since_ms.
    Return list of (event_id, ts) for matching events."""
    headers = {"Authorization": f"Bearer {token}"}
    enc_room = urllib.parse.quote(room)
    targets = []
    seen_ids = set()
    token_from = ""
    pages_done = 0
    older_than_since = False
    while pages_done < max_pages and not older_than_since:
        url = (f"{hs}/_matrix/client/v3/rooms/{enc_room}/messages"
               f"?dir=b&limit=100")
        if token_from:
            url += "&from=" + urllib.parse.quote(token_from)
        status, resp = http("GET", url, headers)
        if status != 200:
            raise SystemExit(f"messages fetch failed: {status} {resp}")
        chunk = resp.get("chunk", [])
        if not chunk:
            break
        for ev in chunk:
            ts = ev.get("origin_server_ts", 0)
            if ts < since_ms:
                older_than_since = True
                continue
            if ts > until_ms:
                continue
            if ev.get("sender") != sender_mxid:
                continue
            if is_redacted(ev):
                continue
            eid = ev.get("event_id")
            if not eid or eid in seen_ids:
                continue
            seen_ids.add(eid)
            targets.append((eid, ts, ev.get("type")))
        token_from = resp.get("end") or ""
        pages_done += 1
        if not token_from:
            break
    return targets, pages_done


def redact(hs, token, room, event_id, reason):
    enc_room = urllib.parse.quote(room)
    enc_eid = urllib.parse.quote(event_id)
    txn = secrets.token_hex(8)
    body = json.dumps({"reason": reason})
    url = f"{hs}/_matrix/client/v3/rooms/{enc_room}/redact/{enc_eid}/{txn}"
    status, resp = http("PUT", url,
                        {"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"}, body)
    return status, resp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hs", required=True)
    ap.add_argument("--user", required=True,
                    help="local-part of the bot user (e.g. shape-rotator-2)")
    ap.add_argument("--password-file", required=True)
    ap.add_argument("--room", required=True, help="room id")
    ap.add_argument("--since-iso", required=True,
                    help="lower bound ISO timestamp (inclusive)")
    ap.add_argument("--until-iso", required=True,
                    help="upper bound ISO timestamp (inclusive)")
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--apply", dest="dry_run", action="store_false")
    ap.add_argument("--limit", type=int, default=0,
                    help="max redactions this run (0 = no cap)")
    ap.add_argument("--reason", default="flood cleanup")
    ap.add_argument("--rate", type=float, default=5.0,
                    help="redaction rate cap (req/s)")
    args = ap.parse_args()

    pw = open(args.password_file).read().strip()
    since_ms = parse_iso(args.since_iso)
    until_ms = parse_iso(args.until_iso)

    print(f"[1/4] login as {args.user}")
    token, device, mxid = login(args.hs, args.user, pw)
    print(f"      mxid={mxid} device={device}")

    try:
        print(f"[2/4] scanning {args.room} for {mxid} events in "
              f"[{args.since_iso}, {args.until_iso}]")
        targets, pages = collect_targets(args.hs, token, args.room, mxid,
                                         since_ms, until_ms)
        print(f"      pages walked: {pages}; matches: {len(targets)}")
        if targets:
            ts_min = datetime.datetime.utcfromtimestamp(
                min(t[1] for t in targets) / 1000).isoformat()
            ts_max = datetime.datetime.utcfromtimestamp(
                max(t[1] for t in targets) / 1000).isoformat()
            print(f"      window of matches: {ts_min}Z .. {ts_max}Z")
            print(f"      first 3: {[t[0] for t in targets[:3]]}")
            print(f"      last 3:  {[t[0] for t in targets[-3:]]}")
            kinds = {}
            for _e, _t, k in targets:
                kinds[k] = kinds.get(k, 0) + 1
            print(f"      type counts: {kinds}")

        if args.dry_run:
            print("[3/4] dry-run: skipping redaction")
            print("[4/4] done (dry-run). Re-run with --apply to redact.")
            return

        cap = args.limit if args.limit > 0 else len(targets)
        targets = targets[:cap]
        print(f"[3/4] applying redaction to {len(targets)} events "
              f"(rate={args.rate}/s, reason={args.reason!r})")
        ok = 0
        fail = 0
        gap = 1.0 / args.rate if args.rate > 0 else 0
        for i, (eid, ts, _kind) in enumerate(targets):
            status, resp = redact(args.hs, token, args.room, eid, args.reason)
            if status == 200:
                ok += 1
            else:
                fail += 1
                print(f"      FAIL {eid}: {status} {resp}")
            if (i + 1) % 25 == 0:
                print(f"      progress: {i+1}/{len(targets)} "
                      f"(ok={ok} fail={fail})")
            if gap and i + 1 < len(targets):
                time.sleep(gap)
        print(f"[4/4] done. ok={ok} fail={fail}")

    finally:
        logout(args.hs, token)


if __name__ == "__main__":
    main()
