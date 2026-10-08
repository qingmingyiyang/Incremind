# Desktop loopback session contract

## Scope

This contract is the required input to `P0-DESK-001`. It defines one Electron-main-owned session between a desktop launch and its Python/FastAPI sidecar. It is not implemented by this contract task.

## Threat model

| Threat | Contract response |
| --- | --- |
| Malicious local process or port competition | Main requests port `0`; the OS assigns a loopback port. Requests authenticate with a per-launch 256-bit session secret and exact instance origin. |
| Fake or stale service | Main accepts health only when protocol version, instance ID, nonce, child PID, expiry and session fingerprint all match its manifest. A mismatch terminates/quarantines the child and fails closed. |
| Renderer XSS | Renderer never receives the secret. Main injects the header only for the exact current sidecar origin; XSS can still act inside the current window and therefore remains a separate renderer-hardening risk. |
| Secret disclosure | The secret is passed only through an inherited environment or anonymous pipe, never command line, URL, log, error response or crash report. Health never returns it. |
| Child crash or old manifest | Session expiry, process exit and app shutdown revoke the session. A restart creates a new instance ID, nonce, secret and port. |

## Ownership and transport

- Electron main creates the manifest, selects port `0`, owns the secret and starts/stops the child.
- The sidecar receives the manifest only via inherited environment or anonymous pipe, then reports the health object.
- Renderer receives only a safe API base origin. It cannot read `secret`, `nonce` or session configuration.
- `Origin` is CORS metadata only. It is never an authentication credential.
- The selected future transport is Electron `session.webRequest` header injection scoped to the exact manifest origin. It preserves existing `fetch` call sites while keeping the secret out of renderer JavaScript. A typed IPC proxy may replace it only after equivalent isolation and regression evidence exist.

## Fail-closed lifecycle

1. Main rejects malformed manifest data with `desktop_session_config_invalid` before spawning a child.
2. Sidecar accepts protected requests only with `X-Chriptmas-Desktop-Session` equal to its current session secret. Production desktop mode refuses to start or returns `desktop_session_unauthorized` when session configuration is absent.
3. Main accepts a health response only when it is `ready` and matches protocol version, instance ID, nonce, child PID and unexpired session values. Any mismatch emits `desktop_session_health_mismatch` and prevents renderer API access.
4. Expiry, child exit, application quit and explicit restart revoke the old secret. Old instances receive no new requests and cannot be reused.
5. Development mode is explicit: it may use a supplied development origin only when `mode=desktop_development` and a session manifest is still present. Plain Web development outside Electron is not production desktop mode and must not silently enable a desktop bypass.

## Error semantics

| Code | Meaning | Renderer-safe response |
| --- | --- | --- |
| `desktop_session_config_invalid` | Main or sidecar configuration is incomplete or violates the schema | Generic local backend configuration error |
| `desktop_session_unauthorized` | Header missing or wrong | Generic local backend authorization error |
| `desktop_session_expired` | Session reached expiry or was revoked | Local backend session expired; restart required |
| `desktop_session_health_mismatch` | Health does not bind to the active child | Local backend identity verification failed |
| `desktop_session_child_exited` | Owned child stopped before readiness or during use | Local backend stopped unexpectedly |
| `desktop_session_origin_rejected` | Request did not target the active exact origin | Local backend origin rejected |

Errors must not echo session secret, nonce, command line, VaultRoot path or unredacted child configuration.
