#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
lxsync_state=${LXSYNC_STATE_DIR:-"$HOME/.local/share/lx-apple-sync"}
mkdir -p "$lxsync_state/vendor" "$lxsync_state/bin"
alx_checkout="$lxsync_state/vendor/agent-lx-music-v0.4.0"

if [ ! -d "$alx_checkout" ]; then
    git clone --depth 1 --branch v0.4.0 https://github.com/Xuepoo/agent-lx-music.git "$alx_checkout"
    git -C "$alx_checkout" apply "$project_dir/vendor/alx-0.4.0.patch"
fi
cargo build --manifest-path "$alx_checkout/Cargo.toml" --release --bin alx
cp "$alx_checkout/target/release/alx" "$lxsync_state/bin/alx"
printf 'alx installed: %s\n' "$lxsync_state/bin/alx"
