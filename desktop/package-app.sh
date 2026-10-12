#!/bin/sh
# Build dist/Hearth.app from the pinned Electron in node_modules and the compiled shell, ad-hoc signed (ADR-0038, D5).
# Electron's own app with ours in Resources/app, as Electron's manual packaging describes; the shell needs no packages
# beyond Electron and Node's own modules. Install it with: ditto dist/Hearth.app ~/Applications/Hearth.app
set -eu
cd "$(dirname "$0")"
node_modules/.bin/tsc
rm -rf dist/Hearth.app
ditto node_modules/electron/dist/Electron.app dist/Hearth.app
mkdir dist/Hearth.app/Contents/Resources/app
cp package.json dist/Hearth.app/Contents/Resources/app/
ditto out dist/Hearth.app/Contents/Resources/app/out
plist=dist/Hearth.app/Contents/Info.plist
plutil -replace CFBundleName -string Hearth "$plist"
plutil -replace CFBundleDisplayName -string Hearth "$plist"
plutil -replace CFBundleIdentifier -string local.hearth.app "$plist"
codesign --force --deep --sign - dist/Hearth.app  # Apple Silicon runs nothing unsigned; editing Info.plist broke Electron's own signature.
codesign --verify --deep --strict dist/Hearth.app
echo "Built $(pwd)/dist/Hearth.app"
