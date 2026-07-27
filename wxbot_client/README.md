# wx-bot client

Standalone WeChat bot client that runs on Windows alongside WeChat desktop.

## Requirements

- Windows 10/11 with WeChat 4.0 desktop
- Python 3.11+
- `pip install -r requirements.txt`

## Setup

1. Copy `config.example.json` to `config.json`
2. Review the device-binding disclosure below. If you consent, set
   `device_binding_consent=true`, provide the operator-approved
   `auth_base_url` and `activation_code`, and keep the generic `device_name`
   (or choose a non-personal label).
3. Generate a unique API token with
   `python -c "import secrets; print(secrets.token_urlsafe(32))"` and place it
   in `api_token`. The SDK rejects missing, placeholder, low-entropy, or
   shorter-than-32-character tokens even when bound only to loopback.
4. Keep `api_host=127.0.0.1` when Agent Console runs on the same Windows host.
   For a Docker bridge, bind the SDK to a Docker-reachable interface, protect
   port 5080 with Windows Firewall, and keep the API token mandatory.
5. Configure the matching SDK URL and **secret reference** in Agent Console's
   “平台连接” page. The bridge initiates requests to this SDK; the client does
   not push ordinary messages to an Agent Console callback.
6. Run the versioned local-queue migration once:
   `python -m queue_migrations ./data/queue.db`
7. Run `python main.py`. The entry point and `queue_store` only verify the
   revision; they never create or alter schema and fail closed on an old
   revision.

## Device binding and privacy

The client fails closed without explicit `device_binding_consent=true` and an
operator-supplied authorization endpoint. With consent, it sends the configured
activation code, a locally generated persistent pseudonymous device ID, the
generic device label, and runtime-integrity hashes to that `auth_base_url`. The
pseudonymous ID is stored in the private `data/client_state.json` file so it is
stable across restarts.

The client does **not** inspect or transmit the Windows MachineGuid, BIOS UUID,
CPU ID, disk serial number, hostname, user profile, SystemRoot, or raw file
paths for device binding. Delete `data/client_state.json` to reset the
pseudonymous identity; doing so may require the device to be activated again.

## Group capture and replies

`group_require_at_me` controls whether ordinary group messages are captured, not
whether the bot replies. Keep it `false` when the bot should understand recent
group context. The Agent Console group reply policy still decides when the bot
speaks, for example only after an `@` mention or configured keyword.

## Architecture

```
WeChat DB (decrypted) → local SDK queue/API ← Agent Console bridge → pipeline
→ reply queue → Agent Console bridge → local SDK `/send` → WeChat UI
```
