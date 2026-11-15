---
name: build-apk-cli
description: Build a standalone command line interface from an analyzed APK using apk2cli. Use after APK inspection to map evidenced app operations into a checked adapter specification, generate a named CLI, and implement auth --adb without inventing app behavior.
---

# Build an APK CLI

Read the workspace task, APK analysis, and the project's specification reference.
Use `$inspect-apk` first if the package, API origin, operations, or session source
are still unknown. Read [reference responsibilities](../../../docs/references.md)
for the distinction between tool evidence and runtime verification.

Write `app.json` from recorded evidence. Include the exact package, HTTPS
origin, authentication header and prefix, app-private source path and key, and
the authenticated identity check. Define named commands with their methods,
paths, help text and typed `parameters`. Each parameter declares `name`,
`in` (`path`, `query` or `body`), and `type` (`string`, `integer`,
`number` or `boolean`); set `required` or `default` when supported by the
observed API. Match path parameters to placeholders such as `/items/{id}`.
Keep credentials out of the specification. Preserve unknowns in the analysis
instead of inserting guessed endpoints.

Generate the CLI after checking the adapter:

```sh
apk2cli build /absolute/app.apk --name xxx --spec ./app.json --out ./dist/xxx
```

Require the generated CLI's public interface to include `xxx auth --adb`,
`xxx auth status`, `xxx auth logout`, JSON output, help, and the selected
application commands. Pass a named argument as `--id VALUE`. Run the generated
entry point from outside the source checkout to establish that it is standalone.

Use the Android app's supported session source. A normal ADB connection does
not grant access to private data in every installed app. Use `run-as` only when
the app permits it; use `--root` only when explicitly selected for an already
authorized rooted device. Report encrypted or device-bound sessions as
unsupported unless the app offers a usable export. Do not silently switch
to root, reinstall the app, clear app data, or disable platform checks.

Validate one authenticated read operation, required and optional arguments,
non-JSON/error responses, and absence of secrets in terminal output. Do not
mark implementation complete from generation alone. Pass the exact generated
CLI, APK and adapter to `$verify-adb-auth`; record remaining limitations next
to the corresponding operation.

