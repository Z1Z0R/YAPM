# Development notes

YAPM is an Omarchy `bar-widget` plugin. `BarWidget.qml` owns its bar button and
loads `Panel.qml`; `backend.py` handles catalog data and invokes Omarchy's plugin
commands. It uses only the Python standard library.

## Validation

```sh
python3 -m unittest discover -s tests -v
omarchy plugin validate .
```

Quickshell supplies virtual `qs` imports at runtime. To use `qmllint` locally:

```sh
mkdir -p /tmp/yapm-qml-imports
ln -sfn /usr/share/omarchy/shell /tmp/yapm-qml-imports/qs
/usr/lib/qt6/bin/qmllint -I /tmp/yapm-qml-imports \
  BarWidget.qml Panel.qml PlainText.qml CompactButton.qml
```

Omarchy's dynamic font objects and Loader items have incomplete static type
information. `qmllint` can report missing-property warnings for these runtime
properties. Inspect the running shell separately for load errors:

```sh
qs log -p /usr/share/omarchy/shell --tail 100
```

## Runtime behavior

- Catalog requests use `https://plugins.omarchy.org/catalog.json`, compressed
  HTTP responses, ETags, a 45-second request timeout, and a 64 MiB decoded limit.
- At most 20,000 catalog entries are accepted; strings are length-limited.
- The gzip catalog cache migrates older JSON caches without dropping cached
  fields. Writes are atomic and decompression is bounded.
- Git update checks run four at a time with a 20-second timeout per repository.
  Install and update commands have a 180-second timeout.
- Mutations use Omarchy's commands. Workers are detached from QML, with a lock
  shared across monitors, so panel closure or ordinary reloads do not cancel them.
- Only local state is polled while the panel is open or an operation is running.
- Display text uses plain-text components. The translated bar tooltip is
  escaped before being passed to the shell's shared tooltip component.
- Translations live in `i18n/`; English is the fallback. New catalogs follow
  Omarchy's live language selection.
- YAPM protects itself from disabling or removal through its own panel. Use
  Omarchy's CLI to manage YAPM itself.

After a command timeout, refresh the installed list and inspect the affected
repository before retrying. Omarchy owns cloning, validation, fast-forward
merges, and its validation rollback behavior.
