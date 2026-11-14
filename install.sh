#!/bin/bash
set -euo pipefail

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
plugin_id=yapm
plugin_root="$HOME/.config/omarchy/plugins"
target="$plugin_root/$plugin_id"
for dependency in omarchy omarchy-shell python3 git curl; do
  command -v "$dependency" >/dev/null || { echo "Missing dependency: $dependency" >&2; exit 1; }
done
[[ ! -e $target && ! -L $target ]] || { echo "Already installed: $target" >&2; exit 1; }
omarchy plugin validate "$source_dir"
omarchy-shell shell ping >/dev/null
mkdir -p "$plugin_root"
stage=$(mktemp -d "$plugin_root/.yapm-install.XXXXXX")
trap 'rm -rf -- "$stage"' EXIT
for file in manifest.json BarWidget.qml Panel.qml PlainText.qml CompactButton.qml backend.py README.md LICENSE; do
  cp -- "$source_dir/$file" "$stage/"
done
cp -r -- "$source_dir/i18n" "$stage/i18n"
omarchy plugin validate "$stage"
if [[ -f $HOME/.config/omarchy/shell.json ]]; then
  cp -- "$HOME/.config/omarchy/shell.json" "$HOME/.config/omarchy/shell.json.yapm-backup-$(date +%Y%m%d-%H%M%S)"
fi
mv -- "$stage" "$target"
omarchy-shell shell rescanPlugins >/dev/null
for ((attempt=0; attempt<80; attempt++)); do
  if omarchy plugin list --json | python3 -c 'import json,sys; sys.exit(not any(p["id"] == "yapm" for p in json.load(sys.stdin)))'; then
    omarchy plugin enable "$plugin_id" --section right
    echo "Yet Another Plugin Manager is ready. Click its cube icon in the bar."
    exit 0
  fi
  sleep 0.1
done
echo "Installed, but shell discovery timed out. Enable with: omarchy plugin enable $plugin_id --section right" >&2
exit 1
