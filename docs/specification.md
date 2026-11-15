# App specification

`app.json` records an application's observed HTTP contract and the exact source of its existing Android session. The generator validates this contract and the APK package before creating a standalone client. Every application needs its own verified contract.

## Minimal working example

This specification matches the repository's Android demo. The E2E runner replaces the loopback port with the live test server's port. Production adapters use the application's verified HTTPS API.

```json
{
  "schema_version": 1,
  "name": "demo",
  "package": "dev.apk2cli.demo",
  "base_url": "http://127.0.0.1:8765",
  "auth": {
    "sources": [
      {
        "path": "shared_prefs/session.xml",
        "format": "shared_prefs",
        "key": "access_token"
      }
    ],
    "header": "Authorization",
    "prefix": "Bearer ",
    "verify": {
      "method": "GET",
      "path": "/me",
      "identity_pointer": "/username"
    }
  },
  "commands": [
    {"name": "me", "method": "GET", "path": "/me", "help": "Show account"},
    {"name": "items", "method": "GET", "path": "/items", "help": "List items"}
  ]
}
```

## Contract fields

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer `1` |
| `name` | CLI name; lowercase letter followed by letters, digits or hyphens, at most 48 characters |
| `package` | Exact Android application ID from the supplied APK |
| `base_url` | HTTPS origin or API prefix, without credentials, query or fragment; loopback HTTP supports local testing |
| `apk_sha256` | Optional input fingerprint from `analysis.json`; build rejects a different APK when supplied |
| `auth` | Exact session source and account verification contract |
| `commands` | Nonempty array of named API operations |

The generator writes `apk_sha256` into its output. Use the observed fingerprint in agent-authored adapters to detect an APK change before reusing an earlier analysis. The fingerprint checks file identity, not the trustworthiness of the APK publisher.

## Authentication sources

`auth.sources` contains one to sixteen known locations, in preference order. The importer attempts each location and accepts only a credential whose verification response contains the configured account identity. The first successfully verified credential becomes the CLI session. A failed attempt preserves an existing local session.

Each `path` names one relative file under `shared_prefs/`, `files/` or `databases/`. Allowed characters are letters, digits, underscores, dots, hyphens, `@` and path separators. Empty, `.` and `..` path segments are rejected. Reads are limited to 4 MiB per source. Only the declared field is retained.

| Format | Required selectors | Example |
| --- | --- | --- |
| `shared_prefs` | `key` | `shared_prefs/session.xml`, string named `access_token` |
| `json` | `key` or `pointer` | `files/session.json`, `/session/access_token` |
| `sqlite` | `table`, `key_column`, `value_column`, `key` | `databases/session.db`, one key-value row |

JSON pointers follow [RFC 6901](https://www.rfc-editor.org/rfc/rfc6901): `/session/access_token` selects a nested value, `~1` escapes `/`, and `~0` escapes `~`. Credentials must be nonempty strings suitable for an HTTP header. No recursive token guessing is performed.

For a JSON session:

```json
{"path":"files/session.json","format":"json","pointer":"/session/access_token"}
```

For a SQLite key-value table:

```json
{"path":"databases/session.db","format":"sqlite","table":"preferences","key_column":"name","value_column":"value","key":"access_token"}
```

SQLite identifiers must be simple letters/digits/underscores. The key is bound as a query parameter, and the source is read locally in read-only immutable mode. The importer reads one database file; it does not merge a live WAL, decrypt SQLCipher databases, or execute application code. Use an observed stable source whose credential is present in that file.

`auth.header` defaults to `Authorization`; `auth.prefix` defaults to `Bearer `, including its final space. For a session cookie, an adapter can set `header` to `Cookie` and use the appropriate fixed cookie-name prefix. The runtime sends this value only to the configured origin and rejects HTTP redirects.

`auth.verify` requires a read-only GET path and `identity_pointer`. An HTTP success without a nonempty account identity fails authentication. Optional `expires_at_pointer` reads an expiry from the verified response; the stored session is rejected after expiry. Expiry can be a Unix timestamp or an ISO 8601 timestamp with a timezone.

## ADB access

`xxx auth --adb` selects an authorized online device, respecting `--serial` and `ANDROID_SERIAL`. Multiple connected devices require an explicit selection. Offline and unauthorized devices produce specific errors before reading app data.

The default reader runs with the application's identity using `run-as`, which requires a debuggable application. `--root` explicitly selects `su` on an already rooted device and confines the requested canonical path to `/data/user/0/PACKAGE`. Android work-profile/secondary-user selection is outside the current reader contract. The command does not root devices, install instrumentation, alter certificates, or change the application.

The application must have a session that its API accepts from the CLI. Keystore-encrypted credentials, device-bound tokens, request signatures and hardware attestation require additional app-specific implementation. Keep these requirements in the analysis rather than declaring a successful generic import.

## Command parameters

Each command defines `name`, `method`, `path`, optional `help`, and optional `parameters`. `auth`, `help`, and `doctor` are reserved names. Supported methods are GET, HEAD, POST, PUT, PATCH and DELETE. API responses are JSON; empty HEAD/204 responses produce a status object.

Every parameter becomes a `--name VALUE` CLI option. Supported types are `string`, `integer`, `number` and `boolean`; boolean values use `true` or `false`.

Parameter names may include underscores, such as `--user_id`. Optional `key` maps a friendly option name to an exact query/body field: `{"name":"page-size","key":"page_size","in":"query","type":"integer"}` sends `page_size`. Numeric inputs and defaults must be finite, and defaults must match the declared type.

```json
{
  "name": "item",
  "method": "GET",
  "path": "/items/{id}",
  "help": "Read an item by ID",
  "parameters": [
    {"name": "id", "in": "path", "type": "string", "required": true},
    {"name": "details", "in": "query", "type": "boolean", "default": false}
  ]
}
```

```sh
xxx item --id one --details true
```

Path parameters are required and URL-encoded. Query parameters are URL-encoded and appended to the configured API path. Body parameters form a JSON object and are available for POST/PUT/PATCH/DELETE. Optional absent parameters are omitted; `default` supplies an explicit default.

```json
{
  "name": "create-item",
  "method": "POST",
  "path": "/items",
  "parameters": [
    {"name": "title", "in": "body", "type": "string", "required": true},
    {"name": "priority", "in": "body", "type": "integer", "default": 1}
  ]
}
```

```sh
xxx create-item --title "Read the manual" --priority 2
```

Executing a mutating command performs the configured API operation. Adapters should give commands clear action names and descriptions. The generator does not discover endpoints by trial, infer destructive operations, or automatically execute generated commands.

## Session lifecycle and errors

`auth status` returns local metadata, including account identity and import time. Add `--verify` to check the server now. `auth logout` removes only the local CLI session. A server-side revocation causes the next protected request to fail; run `auth --adb` after signing in again on the phone.

The generated CLI writes JSON results to stdout, JSON application errors to stderr, and uses a nonzero exit code on failure. Argument parsing errors use the standard argparse message and exit code 2. Authentication failure, timeout, inaccessible sources, malformed input, unsupported paths and refused redirects have distinct error codes. Responses and device output are excluded from error messages because they may contain credentials.

## Verification requirements

Record the APK package/version, environment, observed account endpoint and every tested command. Demonstrate that a rejected token does not create a session, unauthenticated requests fail, successful commands access the expected account, logout removes local access, and importing the phone's session does not cause another app login. Mark emulator and physical-phone results separately.

The repository's `scripts/android_e2e.py` implements this protocol against the supplied Android demo. Applying the same protocol to another app is required before describing that app as supported.
