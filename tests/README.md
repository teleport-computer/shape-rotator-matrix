# Tests

The full PR gate is `bash tests/run_e2e.sh` — see "Running everything" below.

Three test files. Each addresses a different failure mode and runs as part
of the e2e gate.

## What each test covers

### `smoke.py` — flow / integration

Stdlib only. End-to-end *flow* check, cleartext (no Olm) — fast and easy
to debug when something at the HTTP level breaks.

- `POST /signup/api` returns 200 with every `steps` true; account joins
  space + child rooms + creates inviter DM.
- Knock with a valid code → invite to a *vetting* room (not the space).
- Posting a valid 3-line haiku containing the wikipedia keyword → invite
  to the space.
- Knock with a bogus code → no invites at all.
- Welcome-room path (issue #3): `POST /join/api` maps a code to one public
  `#welcome-…` room idempotently (a single-use code survives two POSTs),
  the join consumes the use and brings the confirmation + space invite,
  an outsider joining the live room gets nothing, and after the cleanup
  reap the code reads `code_exhausted`.

Designed to also run against prod by setting `HOMESERVER` + the env vars
in `tests/README.md` of yore. Inside the CI stack `run_in_runner.sh`
points it at the in-network landing nginx.

### `vetting_e2e.py` — real E2EE round-trip of the vetting flow

Two `mautrix-python` clients with `OlmMachine` + `PgCryptoStore`. Two
fresh users each:

1. Knock the space with a valid code.
2. Land in a per-knock vetting room.
3. Solve the haiku captcha.
4. Get promoted to the space + auto-join the encrypted `#bot-noise` child.

Then user A sends an encrypted message in `#bot-noise`, user B's
OlmMachine decrypts it. **This** is the actual E2EE assertion — a
megolm round-trip between two independently-onboarded users that
proves the new vetting flow doesn't wedge crypto.

### `sas_e2e.py` — Paste A+B+C SAS verification (informational)

Pre-existing test. Boots `landing/responder.py` and an initiator-side
`_SASInitiator`, runs the full SAS dance, asserts the verifier's
`PgCryptoStore` flips the bot's device to `TrustState.VERIFIED`.

**Run as informational, not as a gate** — the SAS dance against
continuwuity is known-flaky (tracked in issue #1) and we don't want to
block PR merges on it. `run_in_runner.sh` runs it and reports the
outcome but a failure does not fail the run. Tighten this once SAS is
stable.

Together these three tests cover the things a PR could plausibly
break in this repo:

| Test             | Gates? | Breaks if you break...                                |
|------------------|--------|-------------------------------------------------------|
| `smoke.py`       | YES    | the HTTP signup/knock/vetting flow                    |
| `vetting_e2e.py` | YES    | E2EE megolm correctness for a freshly-onboarded user |
| `sas_e2e.py`     | no     | the Paste A+B+C SAS verification chain                |

## Running everything (the PR gate)

```bash
bash tests/run_e2e.sh
```

That wrapper:

1. Brings up `continuwuity` alone, polls its logs for the one-time
   bootstrap registration token continuwuity prints on first boot
   (`docker compose logs ... | grep ...`).
2. Brings up the rest of the stack (`bootstrap`, `knock-approver`,
   `landing`, `test-runner`) with that token in env.
3. The `bootstrap` container creates the admin user, space, child
   rooms, and codes; writes `/shared/test.env`.
4. `knock-approver` and `test-runner` source that env file at startup.
5. `test-runner` runs `smoke.py`, `vetting_e2e.py`, `sas_e2e.py`
   sequentially.
6. Stack is torn down with `down -v` on exit (clean run every time).

CI calls this same script from `.github/workflows/test.yml`.

## Running one test against the dev stack (faster iteration)

```bash
cd dev && docker compose up -d && cd ..
eval "$(python3 dev/bootstrap.py)"
# In another shell, start the approver:
HTTP_PORT=18001 python3 knock-approver/approver.py
# Run a single test:
HOMESERVER=http://localhost:46167 \
  ADMIN_TOKEN=$MATRIX_TOKEN \
  REG_TOKEN=$CONDUWUIT_REGISTRATION_TOKEN \
  SIGNUP_CODE=$DEV_SIGNUP_CODE KNOCK_CODE=$DEV_KNOCK_CODE \
  SPACE_ID=$SPACE_ID SPACE_CHILDREN=$SPACE_CHILD_IDS \
  python3 tests/smoke.py
```

The dev path skips `landing/`'s nginx — `/signup/api` won't be reachable
on the bare continuwuity port, so the `smoke.py` signup checks will
fail there. Use `tests/run_e2e.sh` for the full picture.

## Running against prod

`tests/sas_prod.py` runs the SAS-verification subset against
`mtrx.shaperotator.xyz` for spot-checking a deploy:

```bash
SIGNUP_CODE=... ADMIN_TOKEN=... python3 tests/sas_prod.py
```

This is **not** part of the PR gate — production is out-of-band.

## Files

- `Dockerfile` — test-runner image (libolm + mautrix + nio + cryptography).
- `docker-compose.test.yml` — full stack used by the PR gate.
- `run_e2e.sh` — host-side wrapper that handles the bootstrap-token dance.
- `run_in_runner.sh` — runs inside the test-runner; sources env, runs
  every test in sequence.
- `smoke.py`, `vetting_e2e.py`, `sas_e2e.py`, `sas_prod.py` — the tests.
