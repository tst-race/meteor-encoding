# Meteor Steganographic Encoding Component

Raceboat `IEncodingComponent` plugin implementing **perfectly-secure steganography** (Meteor, Kaptchuk et al., CCS 2021) with **gpt2-medium** as the carrier model. Produces **text/plain** UTF-8 output.

## Overview

This plugin encodes arbitrary binary payloads into natural-looking English text (stegotext) using a GPT-2 language model as the cover distribution. The encoding provides **exact byte recovery** — it is not lossy. Decoding the stegotext returns the original payload byte-for-byte.

**Key properties:**
- **MIME type**: `text/plain`
- **Max payload**: 384 bytes (8 chunks × 48 bytes)
- **Encoding time**: ~300–340s per chunk on CPU (raceboat-runtime:local), ~2700s for full 384-byte payload
- **Model**: gpt2-medium (345M params), pre-cached at `/opt/raceboat/models/gpt2-medium/`
- **Security**: Cryptographic DRBG masking, HKDF per-chunk nonces, bin-sorting for prefix-bit maximization

## Algorithm

1. **Payload framing**: Prefix payload with 4-byte big-endian length (enables self-delimiting decode, supports zero-message encoding)
2. **Chunking**: Split framed payload into 48-byte (384-bit) chunks
3. **Per-chunk encoding**:
   - Derive chunk nonce via HKDF-SHA256 from base nonce
   - Initialize GPT-2 with sliding-window context (last 128 tokens from previous chunk)
   - For each 32-bit word: DRBG-mask message bits → arithmetic coding with bin-sort → sample token
   - Finish sentence if `finish_sent=True`
4. **Concatenation**: Join chunk stegotexts directly (no delimiter; boundaries implicit via length prefix)
5. **Output**: UTF-8 encoded stegotext bytes

## Configuration Parameters

The plugin requests these parameters via `requestPluginUserInput` at initialization:

| Parameter | Required | Description |
|-----------|----------|-------------|
| `encoding-params` | Yes | JSON object with: `context` (string), `input_key` (base64, 512 bits), `sample_seed_prefix` (base64, 48 bits), `input_nonce` (base64, 512 bits) |
| `save-encoded-output` | No | Boolean (default: false). When true, writes successful encoded bytes to `<loggingDirectory>/encoding-outputs/` for offline analysis |
| `encoding-time-seconds` | No | Float. Overrides `metadata.json` default. Use `run_benchmark_docker.sh` to determine value for target hardware |

**Example `encoding-params` JSON:**
```json
{
  "context": "Your context text here",
  "input_key": "base64-encoded-64-byte-key",
  "sample_seed_prefix": "base64-encoded-6-byte-prefix",
  "input_nonce": "base64-encoded-64-byte-nonce"
}
```

## Building the Plugin

```bash
# From meteor-encoding directory
./build_artifacts.py --arch x86_64
# Output: kit/artifacts/linux-x86_64-{client,server}/PluginMeteorEncoding/
```

No compilation step (pure Python). Dependencies in `requirements.txt` must be present in the runtime image.

## Running Benchmarks

Measure actual encoding time on target hardware (CPU/GPU) to set `encoding-time-seconds`:

```bash
# Quick test (48 bytes, 2 chunks)
./run_benchmark_docker.sh --payload-size 48 --runs 1 --warmup 0

# Full benchmark (384 bytes, 8 chunks) - takes ~1 hour on CPU
./run_benchmark_docker.sh --payload-size 384 --runs 3 --warmup 1 --timeout 7200

# GPU (if available)
./run_benchmark_docker.sh --device cuda --payload-size 384 --runs 3

# JSON output for automation
./run_benchmark_docker.sh --payload-size 384 --output-json
```

**Outputs recommended `encoding-time-seconds` with configurable safety margin (default 2x).**

## Integration Testing

Register plugin and run scenarios with `twoSixIndirect` transport:

```bash
# From raceboat/test/integration
python3 run_scenario.py --scenario-id meteor-send-recv --image-tag local
python3 run_scenario.py --scenario-id meteor-send --image-tag local
python3 run_scenario.py --scenario-id meteor-client-connect --image-tag local
```

**Channel parameters** (set in `NodeContribution.params`):
```yaml
meteorComposition.save-encoded-output: true
meteorComposition.encoding-time-seconds: 2015.0  # from benchmark
meteorComposition.encoding-params: '{"context":"...","input_key":"...","sample_seed_prefix":"...","input_nonce":"..."}'
```

## Zero-Message Encoding

- `encodeBytes` with empty payload → valid stegotext encoding the 4-byte length prefix (0)
- `decodeBytes` of that stegotext → returns exactly empty bytes
- No special-casing; framing handles it naturally

## Test Payload Capacity Boundary

- `max_bytes = 384` (enforced in `encodeBytes`/`decodeBytes`)
- 384 bytes = 8 chunks × 48 bytes (framed payload = 388 bytes → 9 chunks)
- Payloads > 384 bytes raise `ENCODE_FAILED`

## Dependencies

Runtime (must be in `raceboat-runtime` image):
- `torch>=2.0.0` (BSD-3-Clause)
- `transformers>=4.30.0` (Apache-2.0)
- `numpy>=1.21.0` (BSD-3-Clause)
- `bitarray>=2.6.0` (PSF)

Raceboat framework:
- `commsPluginBindings` (Apache-2.0, from raceboat runtime)

Integration test only (not in plugin kit):
- `twoSixIndirect` transport (decomposed-exemplars)
- `rapidUser` user model (decomposed-exemplars)
- `twosix-whiteboard` + `redis` sidecars

## Third-Party Attribution

See `THIRD_PARTY_NOTICES.md` for complete inventory including:
- Meteor reference implementation (CC BY 4.0, schroederdewitt/perfectly-secure-steganography)
- GPT-2 model weights (MIT, OpenAI/Hugging Face)
- All transitive Python dependencies

## Architecture Notes

- **Worker subprocess isolation**: PyTorch/transformers runs in separate Python process to avoid embedded Python conflicts; communicates via JSON-RPC over stdin/stdout
- **Model caching**: gpt2-medium pre-loaded in runtime image at `/opt/raceboat/models/gpt2-medium/`; respects `METEOR_MODEL_PATH` env var
- **Sliding context**: Last 128 tokens carried between chunks for coherence
- **Deterministic**: Same params + payload = same stegotext (cryptographic DRBG)

## Directory Structure

```
meteor-encoding/
├── source/PluginMeteorEncoding/
│   ├── plugin.py          # Raceboat API adapter, factory, user-input handling
│   ├── codec.py           # Meteor codec implementation (encode/decode)
│   ├── metadata.json      # encoding_id, mime_type, max_bytes, encoding_time_seconds
│   ├── manifest.json      # Encoding-only plugin registration
│   ├── config/            # User model action files (send-only, combined, receive-only)
│   └── __init__.py
├── test/
│   ├── test_component.py  # Unit tests with mocked SDK (8 tests, including zero-message)
│   ├── adapter.py         # Integration test adapter for meteorComposition
│   └── run_scenarios.py   # Standalone scenario runner
├── run_benchmark_docker.sh  # Docker-based benchmark runner
├── build_artifacts.py     # Kit staging script
├── requirements.txt       # Python dependencies
└── THIRD_PARTY_NOTICES.md # License attribution inventory
```

## License

Apache-2.0 (plugin code). Third-party components retain their licenses per `THIRD_PARTY_NOTICES.md`.