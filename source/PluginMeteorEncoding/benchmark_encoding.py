#!/usr/bin/env python3
"""
Meteor Encoding Benchmark Script

Standalone script to measure the actual encoding performance of the Meteor
steganography codec on the current system (GPU or CPU) and output an
estimated max encoding time for the configured maximum payload size.

Usage:
    python3 benchmark_encoding.py [--payload-size BYTES] [--runs N] [--safety-margin FACTOR]

The script:
1. Generates a synthetic max-size payload (default 384 bytes = 8 chunks)
2. Runs encoding multiple times to get stable timing
3. Outputs recommended max_encoding_time_seconds with safety margin
4. Can be used to set the 'encoding-time-seconds' parameter in the plugin
"""

import argparse
import base64
import json
import os
import sys
import time
import statistics
from pathlib import Path

# Add the plugin directory to path
PLUGIN_DIR = Path(__file__).parent
sys.path.insert(0, str(PLUGIN_DIR))

# Model path - can be overridden via METEOR_MODEL_PATH env var or --model-path argument
# We read this early so it's available when codec module is imported
# Default assumes raceboat runtime container with pre-cached model
DEFAULT_MODEL_PATH = "/opt/raceboat/models/gpt2-medium"
MODEL_PATH = os.environ.get("METEOR_MODEL_PATH", DEFAULT_MODEL_PATH)

from codec import MeteorCodec, parse_params, CHUNK_SIZE_BYTES, PAYLOAD_LENGTH_BYTES


def generate_test_payload(size_bytes: int) -> bytes:
    """Generate a synthetic payload of the specified size."""
    # Use deterministic pattern for reproducibility
    import hashlib
    data = b""
    counter = 0
    while len(data) < size_bytes:
        data += hashlib.sha256(f"meteor-benchmark-{counter}".encode()).digest()
        counter += 1
    return data[:size_bytes]


def generate_test_params() -> dict:
    """Generate valid encoding parameters for benchmarking."""
    import secrets
    return {
        "context": "Benchmark test context for meteor steganography encoding performance measurement.",
        "input_key": base64.b64encode(secrets.token_bytes(64)).decode(),
        "sample_seed_prefix": base64.b64encode(secrets.token_bytes(6)).decode(),
        "input_nonce": base64.b64encode(secrets.token_bytes(64)).decode()
    }


def run_benchmark(payload_size: int, num_runs: int, warmup_runs: int = 1, device: str = "cpu", model_path: str = "gpt2-medium") -> dict:
    """
    Run encoding benchmark and return timing statistics.
    
    Args:
        payload_size: Size of payload in bytes (max 384 for current config)
        num_runs: Number of timed runs
        warmup_runs: Number of warmup runs (not counted in statistics)
    
    Returns:
        Dictionary with timing statistics
    """
    print(f"\n{'='*60}")
    print(f"Meteor Encoding Benchmark")
    print(f"{'='*60}")
    print(f"Payload size: {payload_size} bytes")
    print(f"Chunk size: {CHUNK_SIZE_BYTES} bytes")
    print(f"Number of chunks: {(payload_size + PAYLOAD_LENGTH_BYTES + CHUNK_SIZE_BYTES - 1) // CHUNK_SIZE_BYTES}")
    print(f"Timed runs: {num_runs}")
    print(f"Warmup runs: {warmup_runs}")
    print(f"Model path: {model_path}")
    print(f"{'='*60}\n")
    
    # Check if model path exists (only for local paths)
    model_path_obj = Path(model_path)
    if not model_path_obj.exists() and "/" not in model_path and "\\" not in model_path:
        # It's likely a Hugging Face model ID, not a local path
        print(f"Using Hugging Face model ID: {model_path}")
    elif not model_path_obj.exists():
        print(f"WARNING: Model path does not exist: {model_path}")
        print(f"Please ensure the model is available locally or use a valid Hugging Face model ID.")
    
    # Initialize codec (loads model)
    print("Loading model...")
    start_load = time.time()
    codec = MeteorCodec(model_path=model_path, device=device)
    load_time = time.time() - start_load
    print(f"Model loaded in {load_time:.2f} seconds")
    
    # Generate test payload and params
    payload = generate_test_payload(payload_size)
    params = generate_test_params()
    params_json = json.dumps(params)
    
    # Warmup runs
    print(f"\nRunning {warmup_runs} warmup run(s)...")
    for i in range(warmup_runs):
        start = time.time()
        _ = codec.encode(payload, params_json)
        elapsed = time.time() - start
        print(f"  Warmup {i+1}: {elapsed:.2f}s")
    
    # Timed runs
    print(f"\nRunning {num_runs} timed run(s)...")
    times = []
    for i in range(num_runs):
        start = time.time()
        encoded = codec.encode(payload, params_json)
        elapsed = time.time() - start
        times.append(elapsed)
        print(f"  Run {i+1}: {elapsed:.2f}s (encoded: {len(encoded)} bytes)")
    
    # Statistics
    times = sorted(times)
    mean_time = statistics.mean(times)
    median_time = statistics.median(times)
    stdev_time = statistics.stdev(times) if len(times) > 1 else 0
    min_time = min(times)
    max_time = max(times)
    p95_time = times[int(len(times) * 0.95)]
    p99_time = times[int(len(times) * 0.99)]
    
    print(f"\n{'='*60}")
    print(f"Results:")
    print(f"{'='*60}")
    print(f"  Min:        {min_time:.2f}s")
    print(f"  Max:        {max_time:.2f}s")
    print(f"  Mean:       {mean_time:.2f}s")
    print(f"  Median:     {median_time:.2f}s")
    print(f"  StdDev:     {stdev_time:.2f}s")
    print(f"  P95:        {p95_time:.2f}s")
    print(f"  P99:        {p99_time:.2f}s")
    print(f"{'='*60}")
    
    return {
        "times": times,
        "min": min_time,
        "max": max_time,
        "mean": mean_time,
        "median": median_time,
        "stdev": stdev_time,
        "p95": p95_time,
        "p99": p99_time,
        "payload_size": payload_size,
        "num_runs": num_runs,
        "warmup_runs": warmup_runs
    }


def calculate_recommended_encoding_time(stats: dict, safety_margin: float = 2.0) -> float:
    """
    Calculate recommended max encoding time with safety margin.
    
    Uses P99 + safety_margin * stddev to account for variance.
    Minimum of 1.0 second.
    """
    p99 = stats["p99"]
    stddev = stats["stdev"]
    
    # Use P99 + 2*stddev as conservative estimate, then apply safety margin
    conservative_estimate = p99 + 2 * stddev
    recommended = max(1.0, conservative_estimate * safety_margin)
    
    # Round up to nearest 5 seconds for cleaner config
    import math
    recommended = math.ceil(recommended / 5.0) * 5.0
    
    return recommended


def main():
    parser = argparse.ArgumentParser(
        description="Benchmark Meteor encoding performance and recommend max encoding time"
    )
    parser.add_argument(
        "--payload-size",
        type=int,
        default=384,
        help="Payload size in bytes (default: 384, max for current config)"
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=5,
        help="Number of timed runs (default: 5)"
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=1,
        help="Number of warmup runs (default: 1)"
    )
    parser.add_argument(
        "--safety-margin",
        type=float,
        default=2.0,
        help="Safety margin factor for recommended time (default: 2.0)"
    )
    parser.add_argument(
        "--output-json",
        action="store_true",
        help="Output results as JSON for programmatic use"
    )
    parser.add_argument(
        "--device",
        type=str,
        default="cpu",
        choices=["cpu", "cuda", "mps"],
        help="Device to run on (default: cpu)"
    )
    parser.add_argument(
        "--model-path",
        type=str,
        default=None,
        help="Path to model directory or Hugging Face model ID (default: METEOR_MODEL_PATH env var or gpt2-medium)"
    )
    
    args = parser.parse_args()
    
    # Validate payload size
    max_payload = 384  # From metadata.json max_bytes
    if args.payload_size > max_payload:
        print(f"Warning: payload size {args.payload_size} exceeds max_bytes ({max_payload}), capping")
        args.payload_size = max_payload
    
    # Determine model path: CLI arg > env var > default
    model_path = args.model_path or os.environ.get("METEOR_MODEL_PATH") or DEFAULT_MODEL_PATH
    
    # Set device
    os.environ["METEOR_DEVICE"] = args.device
    
    # Run benchmark
    stats = run_benchmark(args.payload_size, args.runs, args.warmup, args.device, model_path)
    
    # Calculate recommendation
    recommended = calculate_recommended_encoding_time(stats, args.safety_margin)
    
    print(f"\n{'='*60}")
    print(f"RECOMMENDATION")
    print(f"{'='*60}")
    print(f"Recommended max_encoding_time_seconds: {recommended:.1f}s")
    print(f"  (Based on P99={stats['p99']:.2f}s + 2*StdDev={2*stats['stdev']:.2f}s")
    print(f"   with safety margin {args.safety_margin}x)")
    print(f"{'='*60}")
    
    # Output for plugin configuration
    print(f"\nTo use this value, set the 'encoding-time-seconds' parameter")
    print(f"when running the plugin, or update metadata.json:")
    print(f'  "encoding_time_seconds": {recommended:.1f}')
    
    if args.output_json:
        output = {
            "benchmark": stats,
            "recommended_encoding_time_seconds": recommended,
            "safety_margin": args.safety_margin,
            "device": args.device,
            "model_path": model_path
        }
        print(f"\n--- JSON OUTPUT ---")
        print(json.dumps(output, indent=2))
    
    return 0


if __name__ == "__main__":
    sys.exit(main())