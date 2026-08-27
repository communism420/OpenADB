# ACBridge Shizuku protocol

The exported `.ShizukuActivity` is protected by `android.permission.DUMP`, so
only ADB/shell or another privileged caller can invoke it. Supported
`operation` extras are `status`, `requestPermission`, `execute`, and `cancel`. Every
operation requires a cryptographically random `request_id` matching exactly
`[0-9a-f]{32}`. `execute` also accepts a bounded `timeout_seconds`; command
text is never accepted in an Intent.

`cancel` is request-scoped. It atomically creates an app-private cancellation
marker and signals a matching live activity in the ACBridge process. Active
status, permission, and execution activities poll that marker, cancel a bound
UserService command, remove Shizuku listeners and timeouts, unbind the
UserService with removal enabled, and finish without publishing a late status
file. The marker is deleted by the active request or expired after 30 seconds
when no matching activity exists. Shizuku does not expose an API that can
forcibly dismiss a permission surface already owned by the separate Shizuku
Manager app; ACBridge nevertheless detaches its callback and ignores any late
result after cancellation.

Only `requestPermission` is allowed to remain visible because the user must
interact with the Shizuku permission flow. `status`, `execute`, and `cancel`
use a transparent, non-focusable, non-touchable Activity window. The task is
deliberately not moved to the background: some Android OEMs destroy a
translucent, excluded task as soon as `moveTaskToBack` is called, which used to
cancel every Shizuku command before its UserService connected. The Activity
remains alive only to own the Shizuku binder lifecycle; its window never accepts
input or dims the application underneath it.

Status is written atomically to the app-owned external directory used by
ACBridge:

`/sdcard/Android/data/com.communism420.acbridge/files/openadb/shizuku_status_<request_id>.txt`

It starts with `OPENADB_SHIZUKU_STATUS 1` and contains `key=value` lines for
`request_id`, `state`, `installed`, `binder`, `permission`, `uid`, `mode`,
`api`, and `message_b64`. Messages are UTF-8 encoded with unwrapped Base64.
`mode` is `shell` only for UID 2000 and `root` only for UID 0; all other UIDs
are reported as `unknown` and cannot execute commands.

For execution, OpenADB first creates this shell-owned request file:

`/data/local/tmp/openadb-shizuku-<request_id>.request`

The request format is:

```text
OPENADB_SHIZUKU_REQUEST 1
expected_uid=2000
argv_count=N
arg_b64=<Base64 UTF-8 argv[0]>
arg_b64=<Base64 UTF-8 argv[1]>
...
```

`expected_uid` is mandatory and may be only `2000` or `0`. The desktop obtains
it from a fresh Shizuku status check after the user approves the action. The
UserService compares it with its real runtime UID and rejects the request
before `ProcessBuilder` if Shizuku changed between shell and root identity.

The file is limited to 128 KiB, at most 32 arguments, and at most 64 KiB per
decoded argument. It must resolve to the exact request path and be a regular
file. ACBridge deletes it immediately after a successful parse and starts the
argv directly with `ProcessBuilder`; it never reconstructs or logs a command
line.

The UserService creates sibling `.stdout`, `.stderr`, and `.result` files.
Each output channel is drained concurrently and capped at 8 MiB while excess
data is discarded to prevent deadlocks. Creating the sibling `.cancel` file,
or calling the binder `cancel(request_id)` method, terminates the command.
Timeouts are clamped to 1–3600 seconds. The result starts with
`OPENADB_SHIZUKU_RESULT 1` and includes `state`, `exit_code`, `uid`, `mode`,
`timed_out`, `cancelled`, `stdout_truncated`, `stderr_truncated`, and
`message_b64`, plus byte and duration metadata. Result and output ownership is
made readable only to Android shell (UID/GID 2000) where the active UID permits
it. Result publication uses an internal `.result.tmp` sibling, `fsync`,
shell-only ownership, and an atomic rename, so the desktop cannot parse a
partial result.

Shizuku started by wireless debugging normally grants shell-level authority,
not root. It does not bypass SELinux, Android permission checks that shell does
not hold, or a filesystem mounted read-only. Root is reported only when the
actual Shizuku UserService UID is 0.
