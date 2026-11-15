#!/bin/bash
set -euo pipefail
project_dir="$(cd "$(dirname "$0")/.." && pwd)"
app_dir="$HOME/Library/Application Support/AITrading/AITrading Credentials.app"
mkdir -p "$app_dir/Contents/MacOS"
/usr/bin/swiftc -parse-as-library -swift-version 5 -O -framework AppKit -framework Security \
  "$project_dir/tools/CredentialSetup.swift" \
  -o "$app_dir/Contents/MacOS/AITradingCredentials"
cat > "$app_dir/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>AITradingCredentials</string>
<key>CFBundleIdentifier</key><string>com.antanarr.AITrading.credentials</string>
<key>CFBundleName</key><string>AITrading Credentials</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleVersion</key><string>1</string>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
/usr/bin/codesign --force --sign - --identifier com.antanarr.AITrading.credentials "$app_dir"
printf 'Built AITrading credential window.\n'
