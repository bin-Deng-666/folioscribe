#!/usr/bin/env bash
# Run inside the model's own vLLM environment; see docs/deployment/README.md.
set -euo pipefail
if [[ $# -lt 2 ]]; then
  echo "Usage: bash scripts/deployment/serve_vlm.sh {ovis|paddle|qwen|logics} MODEL_DIRECTORY [extra vLLM args...]" >&2
  exit 2
fi
model_role=$1
model_directory=$2
shift 2
case "$model_role" in
  ovis) service_name=ovis-ocr2; service_port=8031 ;;
  paddle) service_name=PaddleOCR-VL-1.6-0.9B; service_port=8041 ;;
  qwen) service_name=qwen35-2b; service_port=8067 ;;
  logics) service_name=logics-v3; service_port=8068 ;;
  *) echo "Unknown model role: $model_role" >&2; exit 2 ;;
esac
if [[ ! -f "$model_directory/config.json" ]]; then
  echo "Missing model config: $model_directory/config.json" >&2
  exit 2
fi
extra_options=()
if [[ "$model_role" == ovis || "$model_role" == logics ]]; then
  extra_options+=(--gdn-prefill-backend triton)
fi
exec vllm serve "$model_directory" \
  --served-model-name "$service_name" \
  --host "${FOLIOSCRIBE_SERVE_HOST:-127.0.0.1}" \
  --port "${FOLIOSCRIBE_SERVE_PORT:-$service_port}" \
  --tensor-parallel-size "${FOLIOSCRIBE_TP_SIZE:-1}" \
  --gpu-memory-utilization "${FOLIOSCRIBE_GPU_MEMORY:-0.8}" \
  --max-model-len "${FOLIOSCRIBE_MAX_MODEL_LEN:-32768}" \
  "${extra_options[@]}" "$@"
