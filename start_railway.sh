#!/bin/sh
set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
DATA_ROOT="${PANEL_DATA_DIR:-/data}"

case "$DATA_ROOT" in
  /*) ;;
  *) echo "PANEL_DATA_DIR must be an absolute path (for example /data)." >&2; exit 1 ;;
esac

mkdir -p \
  "$DATA_ROOT/storage/uploads" \
  "$DATA_ROOT/storage/encfiles" \
  "$DATA_ROOT/storage/data" \
  "$DATA_ROOT/storage/logs" \
  "$DATA_ROOT/storage/backups" \
  "$DATA_ROOT/storage/tickets" \
  "$DATA_ROOT/storage/bot_data" \
  "$DATA_ROOT/storage/photos" \
  "$DATA_ROOT/sandbox"

# Seed the stock Telegram menu artwork once. Never overwrite user-customized art.
for photo in "$ROOT_DIR"/default_photos/*; do
  [ -f "$photo" ] || continue
  target="$DATA_ROOT/storage/photos/$(basename "$photo")"
  [ -e "$target" ] || cp "$photo" "$target"
done

link_data_path() {
  link="$1"
  target="$2"
  if [ -L "$link" ]; then
    current="$(readlink "$link")"
    if [ "$current" != "$target" ]; then
      rm -f "$link"
      ln -s "$target" "$link"
    fi
  elif [ -e "$link" ]; then
    echo "Refusing to replace existing non-symlink data path: $link" >&2
    exit 1
  else
    ln -s "$target" "$link"
  fi
}

link_data_path "$ROOT_DIR/storage" "$DATA_ROOT/storage"
link_data_path "$ROOT_DIR/sandbox" "$DATA_ROOT/sandbox"

cd "$ROOT_DIR"
exec python -u "$ROOT_DIR/bot.py"
