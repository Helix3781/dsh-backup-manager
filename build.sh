#!/bin/bash
# 构建 DSH 备份管理器.app
set -e
cd "$(dirname "$0")"
swiftc -O -swift-version 5 -parse-as-library -o /tmp/dsh-backup-bin main.swift
APP="/Applications/DSH备份管理器.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp /tmp/dsh-backup-bin "$APP/Contents/MacOS/DSHBackup"
cp AppIcon.icns "$APP/Contents/Resources/AppIcon.icns" 2>/dev/null || true
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
 <key>CFBundleName</key><string>DSH备份管理器</string>
 <key>CFBundleDisplayName</key><string>DSH备份管理器</string>
 <key>CFBundleIdentifier</key><string>local.dsh.backup-manager</string>
 <key>CFBundleVersion</key><string>1.0</string>
 <key>CFBundleShortVersionString</key><string>1.0</string>
 <key>CFBundleExecutable</key><string>DSHBackup</string>
 <key>CFBundleIconFile</key><string>AppIcon</string>
 <key>CFBundlePackageType</key><string>APPL</string>
 <key>LSMinimumSystemVersion</key><string>13.0</string>
 <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
codesign --force -s - "$APP"
echo "构建完成: $APP"
