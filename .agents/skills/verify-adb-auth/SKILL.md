---
name: verify-adb-auth
description: Verify a generated apk2cli command's auth --adb against an already logged-in Android app. Use for real device or emulator acceptance, session import failures, or regression checks; distinguish live Android tests from protocol simulators and unit tests.
---

# Verify ADB authentication

Read the adapter's exact package, credential source, service origin and identity
verification endpoint. Follow [Android constraints and evidence levels](../../../docs/references.md).
Keep personal sessions and raw app data outside the repository and report.

Check `adb devices -l`. Select one authorized device explicitly when several
are connected. Confirm the named package is installed and already logged in.
Do not replace a personal app or erase its data to prepare a test. If no device
is available, run the offline tests and report that Android acceptance is still
unverified.

Run the generated command and preserve its exit code and redacted JSON result:

```sh
xxx auth --adb --serial DEVICE_SERIAL
xxx auth status --verify
```

Then execute one read-only app command and confirm its result matches the same
account. Check that session import alone never reports success before the
authenticated identity request succeeds. Confirm local session files are
private and tokens do not appear in stdout, stderr, reports or generated code.
Use a private test `--state-dir` or `APK2CLI_STATE_DIR` for disposable runs.

Exercise relevant negative paths using a disposable test app/account: expired
or rejected session, absent credential, offline/unauthorized device, ambiguous
device selection, and an app that disallows `run-as`. Keep the prior valid
local session intact when a new import fails. Check that `xxx auth logout`
removes only the CLI's saved session and leaves the phone app logged in.

Record APK SHA-256, package/version, Android version, ADB version, backend used,
commands, exit statuses and observed results. Classify each result explicitly:

- `unit`: parser/storage tests without Android.
- `protocol`: a simulated ADB process or test HTTP service.
- `android-emulator`: a real Android emulator running an installed APK.
- `physical-device`: a real connected Android phone.

State the exact app and backend covered. A test APK proves the supported import
mechanism; it does not prove compatibility with every production app. Remove
temporary test credentials from evidence before committing results.

