#!/usr/bin/env bash
# Build a real Java/DEX APK using the Android SDK; no Gradle dependency download.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
sdk_root="${ANDROID_SDK_ROOT:-${ANDROID_HOME:-}}"
if [[ -z "$sdk_root" || ! -d "$sdk_root/build-tools" ]]; then
  echo "Set ANDROID_SDK_ROOT to an Android SDK with build-tools and platform 35." >&2
  exit 1
fi

android_jar="$sdk_root/platforms/android-35/android.jar"
if [[ ! -f "$android_jar" ]]; then
  echo "Missing platform 35. Run: sdkmanager 'platforms;android-35' 'build-tools;35.0.0'" >&2
  exit 1
fi
build_tools="$sdk_root/build-tools/35.0.0"
if [[ ! -x "$build_tools/d8" || ! -x "$build_tools/aapt" ]]; then
  echo "Missing build-tools 35.0.0. Run: sdkmanager 'build-tools;35.0.0'" >&2
  exit 1
fi

output_dir="${1:-$repo_root/build/android-demo}"
mkdir -p "$output_dir"
output_dir="$(cd "$output_dir" && pwd)"
work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT
mkdir -p "$work_dir/classes" "$work_dir/dex"

javac --release 8 -classpath "$android_jar" \
  -d "$work_dir/classes" \
  "$repo_root/examples/android-demo/src/dev/apk2cli/demo/MainActivity.java"
mapfile -t class_files < <(find "$work_dir/classes" -name '*.class' -type f | sort)
"$build_tools/d8" --min-api 26 --lib "$android_jar" \
  --output "$work_dir/dex" "${class_files[@]}"
keytool -genkeypair -noprompt -keystore "$work_dir/demo.keystore" \
  -storepass android -keypass android -alias apk2cli-fixture \
  -keyalg RSA -keysize 2048 -validity 2 -dname "CN=apk2cli test fixture" >/dev/null 2>&1

for variant in debug release; do
  variant_dir="$work_dir/$variant"
  mkdir -p "$variant_dir"
  if [[ "$variant" == debug ]]; then
    cp "$repo_root/examples/android-demo/AndroidManifest.xml" "$variant_dir/AndroidManifest.xml"
  else
    sed 's/android:debuggable="true"/android:debuggable="false"/' \
      "$repo_root/examples/android-demo/AndroidManifest.xml" > "$variant_dir/AndroidManifest.xml"
  fi
  "$build_tools/aapt" package -f -M "$variant_dir/AndroidManifest.xml" \
    -I "$android_jar" -F "$variant_dir/unsigned.apk"
  python3 - "$variant_dir/unsigned.apk" "$work_dir/dex/classes.dex" <<'PY'
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
import sys
with ZipFile(sys.argv[1], 'a', compression=ZIP_DEFLATED) as apk:
    apk.write(sys.argv[2], 'classes.dex')
PY
  "$build_tools/zipalign" -f 4 "$variant_dir/unsigned.apk" "$variant_dir/aligned.apk"
  "$build_tools/apksigner" sign --ks "$work_dir/demo.keystore" --ks-key-alias apk2cli-fixture \
    --ks-pass pass:android --key-pass pass:android \
    --out "$output_dir/demo-$variant.apk" "$variant_dir/aligned.apk"
  "$build_tools/apksigner" verify "$output_dir/demo-$variant.apk"
done
printf '%s\n' "$output_dir/demo-debug.apk" "$output_dir/demo-release.apk"
