# Matrix Onboarding — Team Knowledge Base

Compiled from ~60 Claude sessions across the hermes-agent, dstack-matrix, dstack-hermes-introducer, teleport-dev-router-matrix, and shape-rotator-matrix projects. Treat this as the start-here doc for anyone operating, debugging, or extending our Matrix infrastructure.

Covers: running Matrix homeservers (Continuwuity on dstack), connecting bots/gateways (mautrix, matrix-nio, raw HTTP), federation, E2EE bootstrap, invite flows, moderation, and known upstream bugs we've worked around.

---

## Environments at a glance

Two live CVMs (as of April 2026):

- **dstack-continuwuity** — the homeserver. Continuwuity 0.5.6 (Conduit fork), image `ghcr.io/amiller/dstack-continuwuity:latest` (MUST be public). Bots registered via reverse-CAPTCHA MCP. Federated with matrix.org.
- **hermes-staging** — the bot host. `tee-daemon` CVM running hermes gateway + knock-approver + per-profile bots. Image `ghcr.io/amiller/hermes-tee@sha256:...`. Deploy: `phala deploy --cvm-id hermes-staging -c docker-compose.staging.yaml -e deploy-notes/.env.staging`.

Separate Continuwuity deployments live at:
- `shape-rotator-matrix/` (this repo) — SR-specific landing + knock-approver for `mtrx.shaperotator.xyz`
- `~/projects/dstack/hermes-introducer/` — local dev + 3-agent test compose
- `~/projects/dstack/dstack-matrix/` — generic Continuwuity-on-dstack example (keep this one pristine)

Do not conflate `dstack-matrix/` (example) with `shape-rotator-matrix/` (SR infra).

---

## Deploying Continuwuity on dstack

**Canonical shape.** `dstack-ingress:2.2` sidecar on 443 for TLS + HAProxy (March 2026 switched off nginx — better for Matrix WS/federation) + continuwuity with no exposed port. Let's Encrypt via DNS-01 inside the TEE, auto-renews every 30 days. Attestation at `/evidences/`.

**Config-as-env is the clean path.** Drop the TOML bind-mount entirely and use `CONDUWUIT_*` env vars. This sidesteps two traps simultaneously:

1. **The TOML-bake-in footgun.** TOML is `COPY`'d at image build; editing `continuwuity.toml` locally doesn't change the running server — you'd need to rebuild the image.
2. **Dstack packages single files as directories.** A mount like `./continuwuity.toml:/etc/conduwuit.toml:ro` silently becomes a dir on the CVM. Container crash-loops with `missing field 'server_name'`. Visible via `ls -la /tapp/continuwuity/` showing `drwxr-xr-x continuwuity.toml`.

**`${VAR}` compose substitution only runs at dstack launch.** If you SSH in and `docker compose up` manually, the literal string `${REGISTRATION_TOKEN}` gets baked into the DB and sticks. Only `phala cvms restart` / `phala deploy` sees sealed env vars. This has cost hours of "Invalid registration token" debugging.

**`registration_token` is consumed by the DB on first boot.** Changing `CONDUWUIT_REGISTRATION_TOKEN` later has no effect. Reset: `docker rm -f dstack-continuwuity-1 && docker volume rm dstack_continuwuity-data` then let phala restart re-bake with real env.

**Bootstrap registration token is printed in logs on first start** and supersedes the configured one — register one user with the bootstrap token before the configured token activates. Values are per-instance, look like a random 16-char alnum string.

**`well_known_client` / `well_known_server` TOML keys are deprecated** in current Continuwuity (WARN-logs ignored). Use env-var form or new section names.

**Conduwuit ignores unknown keys with a warning** (`unknown to conduwuit, ignoring`) — common for Synapse-style pasted config (`database_backend`, `yes_i_am_very_very_sure_...`).

**No Synapse-compatible admin API for registration-token creation** (`M_UNRECOGNIZED: Not Found`). Continuwuity uses `!admin token ...` chat commands, OR have the challenge/gateway service register directly with the static token.

**Empty-string config regressed between builds.** `new_user_displayname_suffix=""` was ignored in 0.5.6 (still appended 🏳️‍⚧️) but works in `continuwuity:latest`. The bake-pin matters — test after bumping.

**Tombstones** (`m.room.tombstone` with `replacement_room`) are the migration primitive. Spaces are rooms with type `m.space` — same mechanism applies. `/rooms/<id>/upgrade` is the one-call wrapper and links `predecessor`.

---

## DNS / TLS / ingress

- **`GATEWAY_DOMAIN` env var (ingress sidecar) is required** — not just `DOMAIN`. Without it: `Error: --content is required for alias records`. Pin the raw `<app-id>-443.dstack-pha-prodN.phala.network`. If you redeploy to a new CVM, update this pin or DNS keeps pointing at the deleted app.
- Namecheap provider (`DNS_PROVIDER=namecheap`, `NAMECHEAP_USERNAME`, `NAMECHEAP_API_KEY`) needs the CVM gateway IP whitelisted in Namecheap's dashboard first. Resolve via `dig gateway-rpc.dstack-pha-prod9.phala.network`. Namecheap API doesn't do CAA — set those manually or skip.
- Dstack gateway only exposes the configured port (6167), not canonical 8448 — well-known must advertise `:443` (gateway TLS) as the federation port.
- `restart: "no"` trick delays Continuwuity's first start until config is right, when you can't avoid using the ephemeral gateway URL as `server_name`.
- End-to-end latency of ~380–440ms is the WireGuard gateway tunnel, not the server. Custom domain won't improve it.

---

## Dstack CVM gotchas (beyond Matrix)

- **`--ssh-pubkey` flag is cosmetic.** It shows in the Phala dashboard but does NOT populate `authorized_keys`. Real mechanism: `DSTACK_AUTHORIZED_KEYS` env var (must also appear in `allowed_envs` in `app-compose.json`). Use the SSH sidecar image `ghcr.io/amiller/dstack-ssh-sidecar:latest` and `phala ssh <cvm> -- -i <deploy_key>`.
- Deploying over a just-deleted CVM name → 409 Conflict. Wait for deletion: `until ! phala cvms list | grep -q <name>; do sleep 5; done`.
- `/dev/mapper/rootfs` reports **100% full (203MB)** on every CVM — it's the read-only overlay; writes go to `/dstack/data`. Not a real problem.
- Continuwuity at idle: ~73MB RAM, ~0% CPU on a 1.8GB CVM after 10 days; RocksDB ~62MB. A 2GB instance handles ~20 invitees comfortably.
- **prod7 is flagged bad.** "Connectivity lost" drops traced to dstack's `wg-checker.sh` failing to re-register with `gateway-rpc.dstack-pha-prod7.phala.network` (`Failed to get collateral ... operation timed out`). The container was healthy the whole time — it's the gateway node. `phala logs --serial` surfaces this; container logs don't. **Prefer prod5 or prod9.**

---

## Federation / well-known

- `.well-known/matrix/server` MUST be served — 404 blocks matrix.org discovery. Client well-known: full HTTPS URL, **no port**. Server well-known: **domain:port**.
- `federationtester.matrix.org` caches failure results. After fixing well-known, wait or re-invite. First failing invite produced `Failed to find any key to satisfy: _FetchKeyRequest(...)`.
- Even with `FederationOK: true`, cross-server invite can need a retry after DNS/TLS propagation.
- `server_name` is identity: `@alice:server-a` and `@alice:server-b` are fully distinct Olm identities, no key reuse across homeservers. Megolm `export_keys`/`import_keys` only lets you *read* old messages — it does not migrate identity.

---

## Bot bootstrap (hermes-staging side)

**Two-bot-per-homeserver pattern is live.** One community identity + one agent identity share the homeserver but run as separate mautrix clients. The hermes gateway and the knock-approver can share a single token — fine because `/sync` is stateless per call, but only the gateway owns E2EE state; the approver reads only unencrypted membership events.

**Each identity gets its own `crypto.db`**, keyed on profile:
- default: `/root/.hermes/platforms/matrix/store/crypto.db`
- profiles: `/root/.hermes/profiles/<name>/platforms/matrix/store/crypto.db`

Never share one file between two mxids. `PgCryptoStore(account_id=user, pickle_key=f"{user}:{device}", ...)` — pickle_key is deterministically `{mxid}:{device_id}`. Changing device_id implicitly orphans the DB.

**Fresh login pattern.** POST `/_matrix/client/v3/login` with `m.login.password`. The returned `device_id` is server-assigned unless you pin it. Record both access_token AND device_id into `.env.staging` immediately — losing device_id means rebootstrapping crypto.

**Reverse-CAPTCHA MCP** at `-8080/mcp` (FastMCP, streamable-http). `matrix_onboard(username)` returns a JS coding challenge (30s expiry) + generated password; `matrix_submit(challenge_id, code)` registers on success, then `/login` gives an access token. Bypasses `registration_token` friction. Keep `PUBLIC_HOMESERVER` separate from internal `HOMESERVER` so clients don't get `http://continuwuity:6167`.

---

## E2EE bootstrap mechanics

**Rule zero: once, in-place, with the gateway stopped.** Creating the DB in `/tmp` and copying it around causes key mismatches between what was uploaded to the server and what's on disk. The gateway will crash-loop with "server has different identity keys" until the DB is deleted and re-bootstrapped.

Canonical one-shots: `bootstrap_mtrx_bots.py`, `bootstrap_shape_rotator_e2ee.py`:
```
Database.create("sqlite:///...", upgrade_table=PgCryptoStore.upgrade_table)
→ store.open()
→ OlmMachine.load()
→ share_keys()
```

**Cross-signing auto-path via `olm.generate_recovery_key()` is the right default.** It does generate-seeds → upload-to-SSSS → publish-publics → sign-own-device in one call, and produces correctly-formatted unpadded keyids (mautrix uses python-olm's `PkSigning.public_key`, which is unpadded). On Continuwuity it works without UIA; Synapse needs `auth=<m.login.password>`. Soft-failure pattern (warning + continue) is fine — recovery-key path on next start with `MATRIX_RECOVERY_KEY` set will recover. *Open PR adding this as a hermes-agent default: [NousResearch/hermes-agent#14871](https://github.com/NousResearch/hermes-agent/pull/14871).*

Manual fallback (rarely needed now):
1. Generate `CrossSigningSeeds`
2. `ssss.generate_and_upload_key`
3. `_upload_cross_signing_keys_to_ssss`
4. `_publish_cross_signing_keys(seeds.to_keys(), auth=<m.login.password>)`
5. `ssss.set_default_key_id`
6. `sign_own_device`

**⚠️ Padded base64 in keyids = silent Element rejection.** This is *the* cross-signing footgun. If you write your own bootstrap (the "fully custom path" below) and use `base64.b64encode(b).decode()` instead of `base64.b64encode(b).rstrip(b"=").decode()`, the resulting keyids look like `ed25519:akWAqt...=` (trailing `=`). The keys upload fine, the homeserver stores them fine, federation serves them fine — but **matrix-rust-sdk silently rejects the entire master_keys entry** in `/keys/query` validation. Element's `userHasCrossSigningKeys(bot, true)` returns `false`, no chain is computed, and the bot shows "Encrypted by a device not verified by its owner" forever. No error anywhere. Confirmed via clean A/B (same homeserver, two users, only padding differs). The `_crosssign` endpoint in `knock-approver/approver.py:_b64()` does the right thing; old `bootstrap_cross_signing.py` in `hermes-agent/` did not — fixed in-tree.

**Fully custom path without OlmMachine also works** (kept for reference). Generate three ed25519 keypairs with `nacl.signing`, sign them into `master_key`/`self_signing_key`/`user_signing_key` objects, POST to `/_matrix/client/v3/keys/device_signing/upload` with a password-UIA `auth` block. Then `/keys/query` to retrieve the existing device, sign it with `self_signing_key`, POST to `/keys/signatures/upload`. Canonical JSON: `json.dumps(..., ensure_ascii=False, separators=(",", ":"), sort_keys=True)`, stripping `signatures`/`unsigned` before signing. **Always `.rstrip(b"=")` the base64 of any pubkey that becomes part of a keyid.**

**Trust relaxation for bots:**
```python
olm.share_keys_min_trust = TrustState.UNVERIFIED
olm.send_keys_min_trust = TrustState.UNVERIFIED
```
Otherwise Megolm keys are withheld from unverified Element sessions.

**`MATRIX_RECOVERY_KEY` env var** carries the generated key across restarts so mautrix doesn't regenerate and orphan SSSS.

---

## mautrix-python known bugs (load-bearing)

- **Late-invite `IntEvt.INVITE` never fires.** `handle_sync` uses `next(e for e in events if e["type"]=="m.room.member" and e["state_key"]==self.mxid)` with no default. Continuwuity's `invite_state.events` often omits the recipient's own `m.room.member`, so `StopIteration → RuntimeError` (PEP 479) kills the whole dispatch batch — that's why invites received while the gateway is running are silently dropped. Repro in `hermes-agent/matrix-invite-bug-repro/`. **Workarounds:** POST `/rooms/{room_id}/join` directly off raw `/sync` output, or patch mautrix to `next(..., None)`.
- **`MemoryStateStore` lacks `is_encrypted`/`find_shared_rooms`/`get_encryption_info`** — OlmMachine silently misbehaves. Wrap it (`_CryptoStateStore`) as done in `hermes-agent/matrix-e2ee-repro/` + staging.
- **`next_batch` must be persisted** via `sync_store.put_next_batch(nb)` every sync, and `handle_sync` returns tasks that must be `await asyncio.gather(*tasks)`'d — skipping either drops dispatch.
- **Continuwuity room IDs lack the `:server` suffix.** `/rooms/{id}/state/...` with the full form 404s. Use `!foo` not `!foo:server.tld`.

---

## `patch_matrix_require_e2ee.py`

Upstream `gateway/platforms/matrix.py` answers in cleartext rooms. The patch wraps `_on_room_message` so `MATRIX_REQUIRE_ENCRYPTION=true` both refuses and auto-leaves non-E2EE rooms. Applied at image build via `RUN python3 /app/patch_matrix_require_e2ee.py`.

**Idempotency check MUST be `"MATRIX_REQUIRE_ENCRYPTION" in src`** — the earlier `docstring in src` check silently no-oped on rebuilds because the docstring exists unpatched. Patch uses `getattr(self._client, "state_store", None)` + `ss.is_encrypted(room_id)` for the same MemoryStateStore reason above.

---

## SAS verification + key-request recovery

- **`sas_verification.py`:** the bot auto-accepts any verification request (no emoji confirmation), then marks the device `TrustState.VERIFIED` in the crypto store. Registers to-device handlers via `EventType.find(name, EventType.Class.TO_DEVICE)`. Info-string format: `"MATRIX_KEY_VERIFICATION_MAC" + their_user + their_device + our_user + our_device + txn_id + key_id`, plus `_info("KEY_IDS")` for `keys_mac`. MAC method: `hkdf-hmac-sha256.v2`.
- **Undecryptable-history recovery (`request_keys.py`):** sends `m.room_key_request` to-device events to specific target devices via `PUT /sendToDevice/m.room_key_request/{txn}`, one per session_id. Must come from a device the sender already trusts; otherwise silently ignored.

---

## Element client behavior

- Element withholds Megolm keys from a new bot device **until the bot speaks first** in the room. After enabling `MATRIX_ENCRYPTION=true`, send a wake message from the bot. Cross-signing with the recovery-key path lets Element trust-without-speak.

---

## Invite flows

**Shape Rotator knock-approver pattern** (this repo):
- Space `!4FL8uL5OEYLATG1VH4wC2CD3pfIV6BMFId9VT7rmm-g` with `join_rule=knock`
- Child rooms (general, announcements, bot-noise) are `restricted` — auto-join for space members
- `knock-approver` long-polls `/sync` as `shape-rotator-2`, reads `/data/codes.json`, auto-invites if the knock reason matches a code
- Share URL: `https://mtrx.shaperotator.xyz/join?code=XYZ` — page shows the code prominently + "Open in Element" button
- `INITIAL_CODES` JSON env seeds codes on first container start; subsequent restarts only add missing codes (uses_remaining is preserved)

**Stale-DM gotcha** (matrix-greeter bug, now fixed): creating a fresh DM on every redeploy when target hadn't accepted → 5 orphan rooms. **Fix:** read `m.direct` account data instead of scanning `joined_rooms` members, or persist the DM room id. GC via `rooms/{id}/leave` + `forget`.

---

## Bot-without-SDK recipe (for raw-HTTP bots)

From `teleport/dev-router-matrix/notebook-relay/BOT_PATTERN.md`. Single bot account, one token shared across every tee-daemon app. Helper:
```ts
fetch(`${HS}/_matrix/client/v3/${path}`, {
  headers: {Authorization: `Bearer ${TOKEN}`}
})
```
Covers: send (`PUT rooms/{id}/send/m.room.message/{txn}`), state, DM create (`POST createRoom {preset:"private_chat",invite:[user]}`).

**tee-daemon warmup pattern.** All Deno apps share one container and only see `env` via the handler's second arg (not `Deno.env.get`). Router fires a synthetic `Request("http://localhost/_warmup")` at startup so handlers can stash creds into module globals for `setInterval` polling loops.

---

## Moderation: humans-only-speak power-levels

Single `PUT m.room.power_levels` from the bot (PL 100):
```json
{
  "events_default": 50,
  "users_default": 0,
  "users": {"<bot>": 100, "<admin>": 50},
  "events": {"m.reaction": 0}
}
```
Effect: randos who join can `/sync` and react but cannot send `m.room.message` → `M_FORBIDDEN`. Applied to `#teleport-router` room `!Sis8Hxt0Clqx2ss33G:...` on 2026-04-07.

---

## matrix-nio gotchas

- `AsyncClient(...)` + manual `client.access_token = ...` puts the token into the **query string** as `access_token=None` before the assignment applies. Continuwuity parses it as a user ID and rejects: "leading sigil is incorrect or missing". **Fix:** use `restore_login()` so nio uses the `Authorization` header.
- `room_create(is_direct=False)` → `'room_id' is a required property`. Omit the field. Isolate with raw curl first.
- nio does not fully support Conduit's `/keys/changes` → device tracking silently stays empty. E2EE still works but peer-device discovery is fragile.

---

## Migrating an E2EE bot between machines (THE BIG ONE)

**The olm/megolm identity lives in the bot's local `nio_store/`, NOT in MXID + `device_id` + access token.** If you redeploy a bot to a new host (laptop → TEE, CVM A → CVM B, container with fresh volume) without copying `nio_store`, nio generates fresh olm keys, uploads them under the same `device_id`, and Matrix overwrites the previous identity on the homeserver. But every cohort member's Element client still has the **old** identity cached and silently refuses to share megolm keys with the "new" device. Result: bot syncs fine, receives encrypted events, never decrypts any. Looks identical to "bot isn't running" — no error in logs, just silence.

This bites every Matrix-bot redeploy. Workflow:

1. **Stop the old bot** (don't let two run under the same `device_id`).
2. **Tar the `nio_store` directory** from the source (e.g. `~/.calendar-bot/nio_store/`).
3. **Place it at the same path on the destination** before first launch. For a tee-daemon image-runtime tenant, this means populating the named volume via a helper container before the project's container starts:
   ```sh
   docker run -d --name nio-restore -v <project>-store:/store alpine sleep 60
   docker cp /tmp/nio_store.tgz nio-restore:/tmp/
   docker exec nio-restore sh -c \
     'rm -rf /store/nio && tar xzf /tmp/nio_store.tgz -C /store && mv /store/nio_store /store/nio'
   docker rm -f nio-restore
   ```
4. **Start the new bot.** Boots with the original olm identity; Element clients already trust it; key sharing carries over.

If you forgot and already uploaded fresh keys, you can still recover: tar whatever nio_store is still around (the original machine's, or any decommissioned host's), restore it as above, restart. The homeserver accepts the device-keys re-upload and existing clients re-sync. Element may briefly show a "session reset" warning.

---

## Tee-daemon (gVisor) image-runtime DNS

Tee-daemon runs image-runtime tenants under `runsc` (gVisor) for isolation. The sandbox **cannot reach Docker's embedded DNS at `127.0.0.11`** — that resolver lives outside gVisor's user-space network stack. The container boots with `/etc/resolv.conf` pointing at `127.0.0.11`, every libc lookup fails with `Errno -3 Temporary failure in name resolution`, and the bot crashes on its first outbound call (gspread, Anthropic, openai, etc.).

Symptom: `/cadence/health` returns 500 from ingress; `docker logs tee-image-<name>-dev` shows a `socket.gaierror` traceback.

**Fix:** entrypoint script that unconditionally overwrites `/etc/resolv.conf`:
```sh
#!/bin/sh
echo "nameserver 8.8.8.8" > /etc/resolv.conf
echo "nameserver 1.1.1.1" >> /etc/resolv.conf
exec python3 /app/bot.py
```
Do **not** gate this on `[ ! -s /etc/resolv.conf ]` — the file is non-empty (it has the bad 127.0.0.11 line), just unreachable.

---

## Container / volume hazards

Staging compose mounts EVERYWHERE state lands to named volumes: `hermes_data:/root/.hermes`, `claude_data:/root/.claude`, plus `opt_data`, `hermes_install`, `usr_local`, `var_lib`, `etc_data`, `home_data`, `root_home`. **Missing any one → crypto.db wiped on next CVM recreate → re-bootstrap required.** Image is pinned by `@sha256:...` in compose so re-pulls don't stomp state.

Entrypoint launches per-profile gateways via `hermes -p <name> gateway run` in parallel background loops — each profile is an independent crypto identity.

---

## Key files / paths

**Hermes-agent (bot side):**
- `bootstrap_mtrx_bots.py`, `bootstrap_shape_rotator_e2ee.py`, `bootstrap_cross_signing.py` — E2EE one-shots
- `fresh_login.py`, `sas_verification.py`, `request_keys.py`, `create_space.py` — operational scripts
- `patch_matrix_require_e2ee.py` — image-time patch
- `matrix-invite-bug-repro/`, `matrix-e2ee-repro/` — minimal repros for upstream bugs
- `deploy-notes/.env.staging` — tokens, device ids, recovery key

**Homeserver side:**
- `~/projects/dstack/dstack-matrix/continuwuity/continuwuity.toml` — working TOML (if mount-as-dir is fixed)
- `~/projects/dstack/dstack-matrix/continuwuity/skills/matrix-onboarding/matrix_client.py` — nio wrapper with nio-bug workaround
- `~/projects/dstack/shape-rotator-matrix/` — SR infra (landing + approver + compose)
- `~/projects/dstack/hermes-introducer/` — local Continuwuity dev + 3-agent test compose
- `~/projects/teleport/dev-router-matrix/notebook-relay/BOT_PATTERN.md` — raw-HTTP bot recipe
