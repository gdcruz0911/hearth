#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="$repository_root/.venv/bin/python"
generate_bin="$repository_root/.venv/bin/mlx_lm.generate"
benchmark_bin="$repository_root/.venv/bin/mlx_lm.benchmark"
model_dir="${HEARTH_GENERATOR_MODEL_DIR:-$HOME/Library/Application Support/Hearth/models/qwen3-8b-4bit}"

if [[ ! -x "$python_bin" || ! -x "$generate_bin" || ! -x "$benchmark_bin" ]]; then
  echo "Missing Hearth virtual environment at $python_bin." >&2
  exit 1
fi

if [[ ! -f "$model_dir/model.safetensors" ]]; then
  echo "Missing local generator weights at $model_dir/model.safetensors." >&2
  exit 1
fi

export HF_HUB_OFFLINE=1

"$python_bin" -c '
import mlx.core as mx

print(f"MLX device: {mx.default_device()}")
'

echo "Running public-prompt smoke test..."
"$generate_bin" \
  --model "$model_dir" \
  --prompt "Reply with the single word: ready" \
  --max-tokens 8 \
  --temp 0 \
  --ignore-chat-template \
  --verbose false

echo "Running performance benchmark..."
"$benchmark_bin" \
  --model "$model_dir" \
  --prompt-tokens 512 \
  --generation-tokens 128 \
  --num-trials 3
