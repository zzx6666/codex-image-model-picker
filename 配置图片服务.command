#!/bin/sh
set -eu
image2_dir=$(CDPATH= cd "$(dirname "$0")" && pwd)
python3 "$image2_dir/codex-image2.py" configure
