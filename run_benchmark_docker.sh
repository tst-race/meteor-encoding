#!/usr/bin/env bash
#
# Docker-based Meteor Encoding Benchmark Runner
#
# Runs the benchmark inside the raceboat runtime container to measure
# actual plugin performance in the exact execution environment.
#
# Usage:
#   ./run_benchmark_docker.sh [options]
#
# Options (passed through to benchmark_encoding.py):
#   --payload-size BYTES    Payload size in bytes (default: 384)
#   --runs N                Number of timed runs (default: 3)
#   --warmup N              Number of warmup runs (default: 1)
#   --safety-margin FACTOR  Safety margin factor (default: 2.0)
#   --device DEVICE         cpu|cuda|mps (default: cpu)
#   --model-path PATH       Model path or HF model ID (default: /opt/raceboat/models/gpt2-medium)
#   --output-json           Output results as JSON
#   --image-tag TAG         Docker image tag (default: local)
#   --timeout SECONDS       Container timeout in seconds (default: 7200)
#
# Examples:
#   ./run_benchmark_docker.sh --runs 3 --warmup 1 --payload-size 384
#   ./run_benchmark_docker.sh --device cuda --runs 5 --output-json
#   ./run_benchmark_docker.sh --model-path gpt2-medium --payload-size 48 --runs 1

set -euo pipefail

# Default values
IMAGE_TAG="${RACEBOAT_IMAGE_TAG:-local}"
TIMEOUT_SECONDS=7200
BENCHMARK_ARGS=()

# Parse known args for docker runner, pass rest to benchmark script
while [[ $# -gt 0 ]]; do
    case $1 in
        --image-tag)
            IMAGE_TAG="$2"
            shift 2
            ;;
        --timeout)
            TIMEOUT_SECONDS="$2"
            shift 2
            ;;
        --help|-h)
            echo "Usage: $0 [docker-options] [benchmark-options]"
            echo ""
            echo "Docker options:"
            echo "  --image-tag TAG     Docker image tag (default: local, or RACEBOAT_IMAGE_TAG env)"
            echo "  --timeout SECONDS   Container timeout in seconds (default: 7200)"
            echo "  --help, -h          Show this help"
            echo ""
            echo "Benchmark options (passed to benchmark_encoding.py):"
            echo "  --payload-size BYTES    Payload size in bytes (default: 384)"
            echo "  --runs N                Number of timed runs (default: 3)"
            echo "  --warmup N              Number of warmup runs (default: 1)"
            echo "  --safety-margin FACTOR  Safety margin factor (default: 2.0)"
            echo "  --device DEVICE         cpu|cuda|mps (default: cpu)"
            echo "  --model-path PATH       Model path or HF model ID (default: /opt/raceboat/models/gpt2-medium)"
            echo "  --output-json           Output results as JSON"
            exit 0
            ;;
        *)
            BENCHMARK_ARGS+=("$1")
            shift
            ;;
    esac
done

# Resolve absolute path to meteor-encoding directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
METEOR_ENCODING_DIR="$SCRIPT_DIR"

# Check if image exists
if ! docker image inspect "raceboat-runtime:${IMAGE_TAG}" >/dev/null 2>&1; then
    echo "Error: Docker image 'raceboat-runtime:${IMAGE_TAG}' not found."
    echo "Build it first or set RACEBOAT_IMAGE_TAG to the correct tag."
    echo "Available images:"
    docker images raceboat-runtime --format "table {{.Repository}}:{{.Tag}}\t{{.Size}}"
    exit 1
fi

echo "============================================================"
echo "Meteor Encoding Benchmark (Docker)"
echo "============================================================"
echo "Image: raceboat-runtime:${IMAGE_TAG}"
echo "Timeout: ${TIMEOUT_SECONDS}s"
echo "Benchmark args: ${BENCHMARK_ARGS[*]}"
echo "============================================================"

# Build docker run command
# - Use --gpus all if CUDA device requested
GPU_FLAG=""
if [[ " ${BENCHMARK_ARGS[*]} " =~ " --device cuda " ]]; then
    GPU_FLAG="--gpus all"
    echo "GPU support enabled (--gpus all)"
fi

# Run the benchmark in container
# Benchmark script now defaults to /opt/raceboat/models/gpt2-medium
docker run --rm \
    ${GPU_FLAG} \
    -v "${METEOR_ENCODING_DIR}:/workspace" \
    -w /workspace \
    --memory=8g \
    --cpus=4 \
    "raceboat-runtime:${IMAGE_TAG}" \
    timeout "${TIMEOUT_SECONDS}" \
    python3 ./source/PluginMeteorEncoding/benchmark_encoding.py "${BENCHMARK_ARGS[@]}"

EXIT_CODE=$?

if [[ $EXIT_CODE -eq 124 ]]; then
    echo ""
    echo "============================================================"
    echo "ERROR: Benchmark timed out after ${TIMEOUT_SECONDS} seconds"
    echo "============================================================"
    echo "Consider:"
    echo "  - Reducing payload size (--payload-size)"
    echo "  - Reducing number of runs (--runs)"
    echo "  - Increasing timeout (--timeout)"
    echo "  - Using GPU (--device cuda) if available"
    exit 1
elif [[ $EXIT_CODE -ne 0 ]]; then
    echo ""
    echo "============================================================"
    echo "ERROR: Benchmark failed with exit code $EXIT_CODE"
    echo "============================================================"
    exit $EXIT_CODE
fi