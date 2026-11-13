"""Adapter contract tests with an explicit, test-only bindings double."""

import importlib
import json
import tempfile
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "source"
sys.path.insert(0, str(SOURCE))
fake_bindings = types.ModuleType("commsPluginBindings")
fake_bindings.IEncodingComponent = type("IEncodingComponent", (), {})
fake_bindings.EncodingProperties = type("EncodingProperties", (), {})
fake_bindings.SpecificEncodingProperties = type("SpecificEncodingProperties", (), {})
fake_bindings.ByteVector = list
fake_bindings.CM_OK = "CM_OK"
for constant in ("COMPONENT_OK", "COMPONENT_ERROR", "ENCODE_OK", "ENCODE_FAILED",
                 "COMPONENT_STATE_STARTED"):
    setattr(fake_bindings, constant, constant)
with patch.dict(sys.modules, {"commsPluginBindings": fake_bindings}):
    plugin = importlib.import_module("PluginMeteorEncoding.plugin")

METADATA_PATH = SOURCE / "PluginMeteorEncoding" / "metadata.json"
TEMPLATE_METADATA = {
    "encoding_id": "meteor",
    "mime_type": "text/plain",
    "max_bytes": 256,
    "encoding_time_seconds": 0.25,
}


# Valid test encoding parameters (base64 encoded as required by codec)
TEST_INPUT_KEY = "lY/yPjucp3eibJefSDVI3HXfhYuBSebMDIp9YtqbEPGv0mbfUGzpHKfnVpej1iAuAFrWgK9oOsYyHbi75AAveA=="  # 512 bits = 64 bytes
TEST_SAMPLE_SEED_PREFIX = "/2IJ/iZ0"  # 48 bits = 6 bytes
TEST_INPUT_NONCE = "NCBu0y0/bGyoEU8ksAlqdH1TAZkHub6AOko2ma3EUeod2MysjUnaxRjwtBL4XZHGkghZZoxKRW9kttO3LzgWBA=="  # 512 bits = 64 bytes
TEST_ENCODING_PARAMS = json.dumps({
    "context": "test context",
    "input_key": TEST_INPUT_KEY,
    "sample_seed_prefix": TEST_SAMPLE_SEED_PREFIX,
    "input_nonce": TEST_INPUT_NONCE,
})


def create_component(sdk, config, metadata=None, save_encoded_output_handle=43, encoding_params_handle=44,
                     answer_save_encoded_output=True, encoding_params=TEST_ENCODING_PARAMS):
    """Helper to create a component with mocked requestPluginUserInput."""
    if metadata is None:
        metadata = json.loads(METADATA_PATH.read_text())
    sdk.requestPluginUserInput.side_effect = [
        types.SimpleNamespace(status="CM_OK", handle=save_encoded_output_handle),
        types.SimpleNamespace(status="CM_OK", handle=encoding_params_handle),
    ]
    with patch.object(Path, "read_text", return_value=json.dumps(metadata)):
        component = plugin.createEncoding("meteor", sdk, "role", config)
    # Complete initialization for encoding-params (always needed)
    component.onUserInputReceived(encoding_params_handle, True, encoding_params)
    # Optionally answer save-encoded-output
    if answer_save_encoded_output:
        component.onUserInputReceived(save_encoded_output_handle, True, "false")
    return component


class ComponentTests(unittest.TestCase):
    def setUp(self):
        self.sdk = Mock()
        self.params = types.SimpleNamespace(json=TEST_ENCODING_PARAMS)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config = types.SimpleNamespace(
            pluginDirectory="/kits/example", loggingDirectory=self.temp_dir.name
        )
        self.metadata = json.loads(METADATA_PATH.read_text())
        if any(self.metadata.get(key) is None for key in
               ("mime_type", "max_bytes", "encoding_time_seconds")):
            self.metadata = TEMPLATE_METADATA
        self.component = create_component(self.sdk, self.config, self.metadata)

    def tearDown(self):
        # Clean up worker subprocess if running
        if hasattr(plugin, '_worker_process') and plugin._worker_process is not None:
            plugin._worker_process.terminate()
            plugin._worker_process.wait(timeout=5)
            plugin._worker_process = None
            plugin._worker_stdin = None
            plugin._worker_stdout = None
            plugin._worker_initialized = False
        self.temp_dir.cleanup()

    def test_factory_and_properties(self):
        # Test factory rejects wrong name
        self.assertIsNone(plugin.createEncoding("other", self.sdk, "", self.config))
        self.assertIsNone(plugin.createEncoding("meteor", None, "", self.config))
        self.sdk.updateState.assert_called_once_with("COMPONENT_STATE_STARTED")
        # Two calls: save-encoded-output and encoding-params
        self.assertEqual(self.sdk.requestPluginUserInput.call_count, 2)
        self.assertEqual(self.component.plugin_directory, "/kits/example")
        self.assertFalse(self.component.save_encoded_outputs)
        properties = self.component.getEncodingProperties()
        self.assertEqual((properties.type, properties.encodingTime),
                 (self.metadata["mime_type"], self.metadata["encoding_time_seconds"]))
        self.assertEqual(self.component.getEncodingPropertiesForParameters(self.params).maxBytes,
                 self.metadata["max_bytes"])
        # save-encoded-output handle already consumed in setUp
        self.assertEqual(self.component.onUserInputReceived(43, False, ""), "COMPONENT_ERROR")

    def test_unconfigured_template_rejects_creation(self):
        with patch.object(Path, "read_text", return_value='{"mime_type":null}'):
            with self.assertRaises(ValueError):
                plugin.createEncoding("meteor", self.sdk, "", self.config)

    def test_all_bytes_and_empty_callbacks(self):
        payload = bytes(range(min(256, self.metadata["max_bytes"])))
        for payload in (b"", payload):
            with self.subTest(size=len(payload)):
                for operation in ("encode", "decode"):
                    self.sdk.reset_mock()
                    if operation == "encode" and len(payload) == 0:
                        # Zero-message encoding: worker returns stegotext, callback gets stegotext
                        mock_stegotext = b"mock stegotext for empty payload"
                        with patch.object(plugin, "_call_worker", return_value=mock_stegotext) as hook:
                            method = getattr(self.component, operation + "Bytes")
                            self.assertEqual(method(42, self.params, list(payload)), "COMPONENT_OK")
                            hook.assert_called_once()
                            call_args = hook.call_args
                            self.assertEqual(call_args[0][0], operation)
                            callback = self.sdk.onBytesEncoded
                            callback.assert_called_once_with(42, list(mock_stegotext), "ENCODE_OK")
                    elif operation == "decode" and len(payload) == 0:
                        # Zero-message decoding: empty content returns empty payload without worker
                        with patch.object(plugin, "_call_worker") as hook:
                            method = getattr(self.component, operation + "Bytes")
                            self.assertEqual(method(42, self.params, list(payload)), "COMPONENT_OK")
                            hook.assert_not_called()
                            callback = self.sdk.onBytesDecoded
                            callback.assert_called_once_with(42, [], "ENCODE_OK")
                    else:
                        # Normal case: worker returns the payload
                        with patch.object(plugin, "_call_worker", return_value=payload) as hook:
                            method = getattr(self.component, operation + "Bytes")
                            self.assertEqual(method(42, self.params, list(payload)), "COMPONENT_OK")
                            hook.assert_called_once()
                            call_args = hook.call_args
                            self.assertEqual(call_args[0][0], operation)
                            callback = self.sdk.onBytesEncoded if operation == "encode" else self.sdk.onBytesDecoded
                            callback.assert_called_once_with(42, list(payload), "ENCODE_OK")
        self.assertFalse((Path(self.temp_dir.name) / "encoding-outputs").exists())

    def test_zero_message_encoding(self):
        """Test zero-message encoding: empty payload produces valid stegotext, decodes to empty."""
        # Test encode: empty payload should call worker and return valid stegotext
        self.sdk.reset_mock()
        # Mock worker to return some valid stegotext (non-empty)
        mock_stegotext = b"Valid stegotext output for empty payload"
        with patch.object(plugin, "_call_worker", return_value=mock_stegotext) as hook:
            status = self.component.encodeBytes(100, self.params, [])
            self.assertEqual(status, "COMPONENT_OK")
            # Worker should be called with method="encode" and payload=b"" (empty, but framed inside worker)
            hook.assert_called_once()
            call_args = hook.call_args
            self.assertEqual(call_args[0][0], "encode")
            # Verify callback gets the stegotext, not empty bytes
            self.sdk.onBytesEncoded.assert_called_once_with(100, list(mock_stegotext), "ENCODE_OK")

        # Test decode: empty stegotext should return empty payload without calling worker
        self.sdk.reset_mock()
        with patch.object(plugin, "_call_worker") as hook:
            status = self.component.decodeBytes(101, self.params, [])
            self.assertEqual(status, "COMPONENT_OK")
            # Worker should NOT be called for empty stegotext
            hook.assert_not_called()
            # Callback should get empty payload
            self.sdk.onBytesDecoded.assert_called_once_with(101, [], "ENCODE_OK")

    def test_persists_encoded_output_only_when_opted_in(self):
        # Create a fresh component without pre-answering save-encoded-output
        sdk = Mock()
        config = types.SimpleNamespace(
            pluginDirectory="/kits/example", loggingDirectory=self.temp_dir.name
        )
        component = create_component(sdk, config, self.metadata, save_encoded_output_handle=50, encoding_params_handle=51,
                                     answer_save_encoded_output=False)
        # Now enable save-encoded-output with handle 50
        self.assertEqual(component.onUserInputReceived(50, True, "true"), "COMPONENT_OK")
        self.assertTrue(component.save_encoded_outputs)
        encoded = b"valid encoded content"
        payload = b"input"[:self.metadata["max_bytes"]]
        with patch.object(plugin, "_call_worker", return_value=encoded):
            self.assertEqual(component.encodeBytes(44, self.params, payload), "COMPONENT_OK")
        output_directory = Path(self.temp_dir.name) / "encoding-outputs"
        outputs = list(output_directory.glob("encoded-output-44-*.bin"))
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0].read_bytes(), encoded)

    def test_unanswered_or_invalid_flag_keeps_persistence_disabled(self):
        # Create a fresh component without pre-answering save-encoded-output
        sdk = Mock()
        config = types.SimpleNamespace(
            pluginDirectory="/kits/example", loggingDirectory=self.temp_dir.name
        )
        component = create_component(sdk, config, self.metadata, save_encoded_output_handle=60, encoding_params_handle=61,
                                     answer_save_encoded_output=False)
        # Unanswered
        self.assertEqual(component.onUserInputReceived(60, False, ""), "COMPONENT_OK")
        self.assertFalse(component.save_encoded_outputs)
        # Invalid value
        self.assertEqual(component.onUserInputReceived(60, True, "yes"), "COMPONENT_ERROR")
        self.assertFalse(component.save_encoded_outputs)

    def test_failures_have_one_completion(self):
        failures = [ValueError("malformed"), NotImplementedError(), "not bytes"]
        if self.metadata["max_bytes"] <= 1_000_000:
            failures.append(b"x" * (self.metadata["max_bytes"] + 1))
        for operation in ("encode", "decode"):
            for result in failures:
                with self.subTest(operation=operation, result=type(result).__name__):
                    self.sdk.reset_mock()
                    kwargs = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
                    with patch.object(plugin, "_call_worker", **kwargs):
                        content = (b"x" * (self.metadata["max_bytes"] + 1)
                                   if operation == "encode" and isinstance(result, bytes)
                                   else b"x")
                        status = getattr(self.component, operation + "Bytes")(9, self.params, content)
                    self.assertEqual(status, "COMPONENT_ERROR")
                    callback = self.sdk.onBytesEncoded if operation == "encode" else self.sdk.onBytesDecoded
                    callback.assert_called_once_with(9, [], "ENCODE_FAILED")

    def test_callback_exception_is_not_reported_twice(self):
        self.sdk.onBytesEncoded.side_effect = RuntimeError("SDK failed")
        payload = b"input"[:self.metadata["max_bytes"]]
        with patch.object(plugin, "_call_worker", return_value=b"ok"):
            with self.assertRaises(RuntimeError):
                self.component.encodeBytes(1, self.params, payload)
        self.sdk.onBytesEncoded.assert_called_once()


if __name__ == "__main__":
    unittest.main()