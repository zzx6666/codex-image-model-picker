#!/bin/sh
set -eu
skill_dir=$(CDPATH= cd "$(dirname "$0")" && pwd)
python3 "$skill_dir/codex_image_model_picker.py" configure
