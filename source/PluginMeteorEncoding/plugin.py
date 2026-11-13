"""Raceboat API adapter; format-specific work belongs in codec.py."""

import base64
import faulthandler
import json
import math
import subprocess
import threading
import uuid
from pathlib import Path

import commsPluginBindings as bindings

from . import codec

# Enable faulthandler to get traceback on C++ crashes
faulthandler.enable()

# Worker process for encoding/decoding to isolate PyTorch from embedded Python
_worker_process = None
_worker_stdin = None
_worker_stdout = None
_worker_lock = threading.Lock()
_worker_initialized = False


def _get_worker():
    """Get or create the worker subprocess."""
    global _worker_process, _worker_stdin, _worker_stdout, _worker_initialized

    with _worker_lock:
        if _worker_initialized and _worker_process is not None and _worker_process.poll() is None:
            return _worker_stdin, _worker_stdout

        # Start new worker subprocess
        worker_code = '''
import sys
import json
import base64
sys.path.insert(0, "/kits/PluginMeteorEncoding")
from codec import MeteorCodec
codec_instance = MeteorCodec()
print("Worker ready", file=sys.stderr, flush=True)
while True:
    line = sys.stdin.readline()
    if not line:
        break
    request = json.loads(line.strip())
    req_id = request.get("id")
    method = request.get("method")
    params = request.get("params", {})
    try:
        if method == "encode":
            payload_b64 = params.get("payload", "")
            params_json = params.get("params_json", "{}")
            payload = base64.b64decode(payload_b64) if payload_b64 else b""
            encoded = codec_instance.encode(payload, params_json)
            response = {"id": req_id, "success": True, "result": base64.b64encode(encoded).decode()}
        elif method == "decode":
            encoded_b64 = params.get("encoded", "")
            params_json = params.get("params_json", "{}")
            encoded = base64.b64decode(encoded_b64) if encoded_b64 else b""
            decoded = codec_instance.decode(encoded, params_json)
            response = {"id": req_id, "success": True, "result": base64.b64encode(decoded).decode()}
        else:
            response = {"id": req_id, "success": False, "error": "Unknown method: " + method}
    except Exception as e:
        import traceback
        response = {"id": req_id, "success": False, "error": type(e).__name__ + ": " + str(e) + "\\n" + traceback.format_exc()}
    sys.stdout.write(json.dumps(response) + "\\n")
    sys.stdout.flush()
'''

        # Capture stderr to a log file for debugging
        import os
        log_dir = os.environ.get("RACEBOAT_LOG_DIR", "/tmp")
        stderr_log = open(os.path.join(log_dir, "meteor_worker_stderr.log"), "a")
        stderr_log.write(f"[{__import__('datetime').datetime.now()}] Starting worker subprocess\n")
        stderr_log.flush()

        _worker_process = subprocess.Popen(
            ['python3', '-c', worker_code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_log,
            text=True,
            bufsize=1
        )

        _worker_stdin = _worker_process.stdin
        _worker_stdout = _worker_process.stdout
        _worker_initialized = True
        stderr_log.write(f"[{__import__('datetime').datetime.now()}] Worker subprocess started (pid={_worker_process.pid})\n")
        stderr_log.flush()
        return _worker_stdin, _worker_stdout


def _call_worker(method, **params):
    """Call worker subprocess for encode/decode."""
    req_id = str(uuid.uuid4())
    request = {'id': req_id, 'method': method, 'params': params}

    stdin, stdout = _get_worker()

    # Log request
    import os
    log_dir = os.environ.get("RACEBOAT_LOG_DIR", "/tmp")
    debug_log = open(os.path.join(log_dir, "meteor_worker_debug.log"), "a")
    debug_log.write(f"[{__import__('datetime').datetime.now()}] Calling worker: method={method}, req_id={req_id}\n")
    debug_log.flush()

    try:
        stdin.write(json.dumps(request) + '\n')
        stdin.flush()

        response_line = stdout.readline()
        if not response_line:
            debug_log.write(f"[{__import__('datetime').datetime.now()}] Worker process died (no response)\n")
            debug_log.flush()
            raise RuntimeError("Worker process died")

        response = json.loads(response_line.strip())
        if response.get('id') != req_id:
            debug_log.write(f"[{__import__('datetime').datetime.now()}] Response ID mismatch\n")
            debug_log.flush()
            raise RuntimeError("Response ID mismatch")

        if not response.get('success'):
            debug_log.write(f"[{__import__('datetime').datetime.now()}] Worker error: {response.get('error', 'unknown')}\n")
            debug_log.flush()
            raise RuntimeError("Worker error: " + response.get('error', 'unknown'))

        result = base64.b64decode(response['result'])
        debug_log.write(f"[{__import__('datetime').datetime.now()}] Worker success: {len(result)} bytes\n")
        debug_log.flush()
        return result
    except Exception as e:
        global _worker_initialized
        _worker_initialized = False
        debug_log.write(f"[{__import__('datetime').datetime.now()}] Exception: {type(e).__name__}: {e}\n")
        debug_log.flush()
        raise


class PluginMeteorEncoding(bindings.IEncodingComponent):
    def __init__(self, sdk, role_name, plugin_config):
        if sdk is None:
            raise ValueError("An encoding SDK is required")
        metadata = json.loads(Path(__file__).with_name("metadata.json").read_text())
        if not isinstance(metadata["mime_type"], str) or not metadata["mime_type"]:
            raise ValueError("Set the designed MIME type using new_encoding.py")
        capacity = metadata["max_bytes"]
        if type(capacity) is not int or not 0 < capacity <= 2**31 - 1:
            raise ValueError("Set a verified positive int32 max_bytes")
        seconds = metadata["encoding_time_seconds"]
        if (type(seconds) not in (int, float)
                or not math.isfinite(seconds) or seconds <= 0):
            raise ValueError("Set a measured positive encoding_time_seconds")
        super().__init__()
        self.sdk = sdk
        self.role_name = role_name
        self.plugin_directory = plugin_config.pluginDirectory
        self.logging_directory = plugin_config.loggingDirectory
        self.metadata = metadata
        self.save_encoded_output_handle = None
        self.save_encoded_outputs = False
        self.encoding_params_handle = None
        self.encoding_params = {}
        self.encoding_time_handle = None
        self.encoding_time_override = None

        # Request save-encoded-output parameter
        response = self.sdk.requestPluginUserInput(
            "save-encoded-output",
            "Write encoded outputs to the logging directory for offline analysis? (true/false; default false)",
            False,
        )
        if response.status == bindings.CM_OK:
            self.save_encoded_output_handle = response.handle

        # Request encoding parameters (context, input_key, sample_seed_prefix, input_nonce)
        response = self.sdk.requestPluginUserInput(
            "encoding-params",
            "JSON encoding parameters for meteor steganography",
            False,
        )
        if response.status == bindings.CM_OK:
            self.encoding_params_handle = response.handle

        # Request encoding time override parameter (allows user to specify max encoding time
        # based on benchmark results for their specific hardware - GPU vs CPU)
        response = self.sdk.requestPluginUserInput(
            "encoding-time-seconds",
            "Maximum encoding time in seconds (overrides metadata.json default; use benchmark_encoding.py to determine)",
            False,
        )
        if response.status == bindings.CM_OK:
            self.encoding_time_handle = response.handle

        self.sdk.updateState(bindings.COMPONENT_STATE_STARTED)

    def onUserInputReceived(self, handle, answered, response):
        # Handle save-encoded-output parameter
        if handle == self.save_encoded_output_handle and self.save_encoded_output_handle is not None:
            self.save_encoded_output_handle = None
            if not answered:
                self.save_encoded_outputs = False
                return bindings.COMPONENT_OK
            normalized_response = response.strip().lower()
            if normalized_response in ("true", "1"):
                self.save_encoded_outputs = True
                return bindings.COMPONENT_OK
            if normalized_response in ("false", "0"):
                self.save_encoded_outputs = False
                return bindings.COMPONENT_OK
            self.save_encoded_outputs = False
            return bindings.COMPONENT_ERROR

        # Handle encoding parameters
        if handle == self.encoding_params_handle and self.encoding_params_handle is not None:
            self.encoding_params_handle = None
            if not answered:
                return bindings.COMPONENT_ERROR
            try:
                self.encoding_params = json.loads(response)
                return bindings.COMPONENT_OK
            except json.JSONDecodeError:
                return bindings.COMPONENT_ERROR

        # Handle encoding time override parameter
        if handle == self.encoding_time_handle and self.encoding_time_handle is not None:
            self.encoding_time_handle = None
            if not answered:
                # Use default from metadata
                self.encoding_time_override = None
                return bindings.COMPONENT_OK
            try:
                override_value = float(response.strip())
                if override_value <= 0:
                    self._log_error(f"Invalid encoding-time-seconds value: {override_value} (must be positive)")
                    self.encoding_time_override = None
                else:
                    self.encoding_time_override = override_value
                return bindings.COMPONENT_OK
            except ValueError:
                self._log_error(f"Invalid encoding-time-seconds value: {response} (must be a number)")
                self.encoding_time_override = None
                return bindings.COMPONENT_ERROR

        return bindings.COMPONENT_ERROR

    def _persist_encoded_output(self, handle, encoded):
        if not self.save_encoded_outputs:
            return
        if not self.logging_directory:
            raise OSError("Logging directory is unavailable")
        output_directory = Path(self.logging_directory) / "encoding-outputs"
        output_directory.mkdir(parents=True, exist_ok=True)
        output_path = output_directory / "encoded-output-{}-{}.bin".format(
            handle, uuid.uuid4().hex
        )
        output_path.write_bytes(encoded)

    def _log_error(self, message):
        """Write error to a file in the logging directory for debugging."""
        if self.logging_directory:
            try:
                error_dir = Path(self.logging_directory) / "encoding-errors"
                error_dir.mkdir(parents=True, exist_ok=True)
                error_path = error_dir / "encode-error-{}.txt".format(uuid.uuid4().hex)
                error_path.write_text(message)
            except Exception:
                pass  # Best effort logging

    def getEncodingProperties(self):
        properties = bindings.EncodingProperties()
        properties.type = self.metadata["mime_type"]
        # Use user-specified override if provided, otherwise use metadata default
        if self.encoding_time_override is not None:
            properties.encodingTime = self.encoding_time_override
        else:
            properties.encodingTime = self.metadata["encoding_time_seconds"]
        return properties

    def getEncodingPropertiesForParameters(self, params):
        properties = bindings.SpecificEncodingProperties()
        properties.maxBytes = self.metadata["max_bytes"]
        return properties

    def encodeBytes(self, handle, params, content):
        try:
            payload = bytes(content)
            if len(payload) > self.metadata["max_bytes"]:
                raise ValueError("Payload exceeds maxBytes")

            # Use stored encoding parameters (from requestPluginUserInput) instead of params.json
            params_json = json.dumps(self.encoding_params)
            
            # Debug log
            import os
            log_dir = os.environ.get("RACEBOAT_LOG_DIR", "/tmp")
            debug_log = open(os.path.join(log_dir, "meteor_worker_debug.log"), "a")
            debug_log.write(f"[{__import__('datetime').datetime.now()}] encodeBytes: handle={handle}, payload_len={len(payload)}, encoding_params_keys={list(self.encoding_params.keys())}\n")
            debug_log.flush()

            # Call worker subprocess for encoding
            import base64
            payload_b64 = base64.b64encode(payload).decode()
            encoded = _call_worker('encode', payload=payload_b64, params_json=params_json)

            if not isinstance(encoded, bytes):
                raise TypeError("worker encode must return bytes")
            self._persist_encoded_output(handle, encoded)
            output = bindings.ByteVector(encoded)
        except Exception as e:
            import traceback
            error_msg = "encodeBytes failed: {}: {}\n{}".format(type(e).__name__, e, traceback.format_exc())
            self._log_error(error_msg)
            self.sdk.onBytesEncoded(handle, bindings.ByteVector(), bindings.ENCODE_FAILED)
            return bindings.COMPONENT_ERROR
        self.sdk.onBytesEncoded(handle, output, bindings.ENCODE_OK)
        return bindings.COMPONENT_OK

    def decodeBytes(self, handle, params, content):
        try:
            # Zero-message decoding: empty stegotext -> empty payload (no worker call)
            if not content:
                self.sdk.onBytesDecoded(handle, bindings.ByteVector(), bindings.ENCODE_OK)
                return bindings.COMPONENT_OK

            # Use stored encoding parameters (from requestPluginUserInput) instead of params.json
            params_json = json.dumps(self.encoding_params)

            # Call worker subprocess for decoding
            import base64
            encoded_b64 = base64.b64encode(bytes(content)).decode()
            decoded = _call_worker('decode', encoded=encoded_b64, params_json=params_json)

            if not isinstance(decoded, bytes):
                raise TypeError("worker decode must return bytes")
            if len(decoded) > self.metadata["max_bytes"]:
                raise ValueError("Decoded payload exceeds maxBytes")
            output = bindings.ByteVector(decoded)
        except Exception as e:
            import traceback
            error_msg = "decodeBytes failed: {}: {}\n{}".format(type(e).__name__, e, traceback.format_exc())
            self._log_error(error_msg)
            self.sdk.onBytesDecoded(handle, bindings.ByteVector(), bindings.ENCODE_FAILED)
            return bindings.COMPONENT_ERROR
        self.sdk.onBytesDecoded(handle, output, bindings.ENCODE_OK)
        return bindings.COMPONENT_OK


def createEncoding(name, sdk, roleName, pluginConfig):
    if name != "meteor" or sdk is None:
        return None
    return PluginMeteorEncoding(sdk, roleName, pluginConfig)