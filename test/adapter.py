#!/usr/bin/env python3
"""
Test adapter for meteor-encoding plugin.

This adapter handles the meteorComposition which uses the twoSixIndirect
transport from decomposed-exemplars with the meteor encoding.
"""

import base64
import json
import platform
import sys
import uuid
from pathlib import Path

try:
    from adapter_types import NodeContribution, NodeRequest
except ImportError:
    _integration_dir = Path(__file__).resolve().parents[2] / "raceboat" / "test" / "integration"
    if not _integration_dir.is_dir():
        raise ImportError(
            "meteor-encoding adapter: could not import adapter_types"
        ) from None
    sys.path.insert(0, str(_integration_dir))
    from adapter_types import NodeContribution, NodeRequest  # noqa: E402


COMPOSITION = "meteorComposition"
KIT_NAME = "PluginMeteorEncoding"

# Fixed test encoding parameters for integration test (deterministic for reproducibility)
# These are base64-encoded as required by the codec
TEST_CONTEXT = "integration test context"
TEST_INPUT_KEY = "lY/yPjucp3eibJefSDVI3HXfhYuBSebMDIp9YtqbEPGv0mbfUGzpHKfnVpej1iAuAFrWgK9oOsYyHbi75AAveA=="  # 512 bits = 64 bytes
TEST_SAMPLE_SEED_PREFIX = "/2IJ/iZ0"  # 48 bits = 6 bytes
TEST_INPUT_NONCE = "NCBu0y0/bGyoEU8ksAlqdH1TAZkHub6AOko2ma3EUeod2MysjUnaxRjwtBL4XZHGkghZZoxKRW9kttO3LzgWBA=="  # 512 bits = 64 bytes

# twoSixIndirect is backed by a shared whiteboard (see
# decomposed-exemplars/scripts/docker-compose.yml); required as a sidecar
# service any time this composition is used.
WHITEBOARD_HOSTNAME = "twosix-whiteboard"
WHITEBOARD_PORT = 5000
SIDECAR_SERVICES = {
    "twosix-redis": {
        "image": "redis:6.0.6",
        "healthcheck": {
            "test": ["CMD", "redis-cli", "ping"],
            "interval": "2s",
            "timeout": "2s",
            "retries": 15,
        },
    },
    "twosix-whiteboard": {
        "image": "ghcr.io/tst-race/race-core/twosix-whiteboard:main",
        "command": "-w 8",
        "hostname": WHITEBOARD_HOSTNAME,
        "depends_on": {"twosix-redis": {"condition": "service_healthy"}},
        "environment": {"REDIS_HOSTNAME": "twosix-redis"},
        "healthcheck": {
            "test": [
                "CMD",
                "python3",
                "-c",
                "import socket; socket.create_connection(('127.0.0.1', 5000), 2).close()",
            ],
            "interval": "2s",
            "timeout": "2s",
            "retries": 15,
        },
    },
}


def _detect_host_architecture() -> str:
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "x86_64"
    if machine in {"arm64", "aarch64"}:
        return "arm64-v8a"
    raise ValueError(
        f"Unsupported host architecture for Docker builds: {platform.machine()}"
    )


def kit_dir(role: str) -> Path:
    plugin_root = Path(__file__).resolve().parents[1]
    return (
        plugin_root
        / "kit"
        / "artifacts"
        / f"linux-{_detect_host_architecture()}-{'client' if role == 'connector' else 'server'}"
        / KIT_NAME
    )


def generate_node_contribution(request: NodeRequest) -> NodeContribution:
    composition = request.composition_name or COMPOSITION
    contribution = NodeContribution(
        params={},
        channel_name=composition,
        kit_dir=kit_dir(request.role),
        sidecar_services=dict(SIDECAR_SERVICES),
        environment={"PYTHONPATH": "/linux/x86_64/lib/python"},
    )

    # Enable save-encoded-output for integration test (AGENTS.md requirement)
    param_key = f"{composition}.save-encoded-output"
    contribution.params[param_key] = "true"

    # Provide action file for rapidUser user model (decomposed-exemplars)
    # Select action file based on mode and role (like transport-stub adapter)
    mode = request.scenario_mode
    if mode == "send":
        # send-only: connector POSTs, listener FETCHes
        action_file = "actions-send-only.json" if request.role == "connector" else "actions.json"
    else:
        # send-recv, client-connect: both directions
        action_file = "actions-combined.json"
    contribution.params[f"{composition}.actionFile"] = f"/kits/{KIT_NAME}/config/{action_file}"

    # Provide required meteor encoding parameters
    encoding_params = {
        "context": TEST_CONTEXT,
        "input_key": TEST_INPUT_KEY,
        "sample_seed_prefix": TEST_SAMPLE_SEED_PREFIX,
        "input_nonce": TEST_INPUT_NONCE,
    }
    contribution.params[f"{composition}.encoding-params"] = json.dumps(encoding_params)

    # For the meteorComposition using twoSixIndirect transport,
    # we delegate address generation to the same logic as decomposed-exemplars.
    # Since this adapter owns the composition, we generate addresses here.
    is_bootstrap_slot = request.slot in ("initial", "final")
    is_dynamic_direction = False
    if is_bootstrap_slot and request.direction:
        link_direction = request.effective_link_direction or "LD_BIDI"
        if request.role == "listener" and request.direction == "send":
            is_dynamic_direction = link_direction in ("LD_BIDI", "LD_LOADER_TO_CREATOR")
        elif request.role == "connector" and request.direction == "recv":
            is_dynamic_direction = link_direction in ("LD_BIDI", "LD_LOADER_TO_CREATOR")
    if is_dynamic_direction:
        return contribution

    if request.role == "listener":
        is_bootstrap_slot = request.slot in ("initial", "final")
        link_direction = request.effective_link_direction or "LD_BIDI"
        listener_send_creates = not (
            is_bootstrap_slot
            and request.direction == "send"
            and link_direction == "LD_LOADER_TO_CREATOR"
        )
        listener_recv_creates = True

        if request.direction == "recv" and listener_recv_creates:
            recv_address = {
                "hashtag": f"twoSixIndirect_{uuid.uuid4().hex[:12]}",
                "hostname": WHITEBOARD_HOSTNAME,
                "port": WHITEBOARD_PORT,
            }
            contribution.address_output = {"recv": recv_address}
            contribution.cli_flags["recv-address"] = json.dumps(recv_address)
        elif request.direction == "send":
            if listener_send_creates:
                send_address = {
                    "hashtag": f"twoSixIndirect_{uuid.uuid4().hex[:12]}",
                    "hostname": WHITEBOARD_HOSTNAME,
                    "port": WHITEBOARD_PORT,
                }
                contribution.address_output = {"send": send_address}
                contribution.cli_flags["send-address"] = json.dumps(send_address)
            else:
                contribution.needs_peer_address = True
                connector_addresses = next(iter(request.peer_context.values()), None)
                if connector_addresses is None:
                    raise ValueError(
                        "meteor-encoding adapter: listener node requires a "
                        "connector's address_output in peer_context for LD_LOADER_TO_CREATOR, "
                        "but none was provided"
                    )
                if isinstance(connector_addresses, dict) and "recv" in connector_addresses:
                    contribution.cli_flags["send-address"] = json.dumps(connector_addresses["recv"])
        else:
            # Simple mode: both directions
            recv_address = {
                "hashtag": f"twoSixIndirect_{uuid.uuid4().hex[:12]}",
                "hostname": WHITEBOARD_HOSTNAME,
                "port": WHITEBOARD_PORT,
            }
            send_address = {
                "hashtag": f"twoSixIndirect_{uuid.uuid4().hex[:12]}",
                "hostname": WHITEBOARD_HOSTNAME,
                "port": WHITEBOARD_PORT,
            }
            contribution.address_output = {"recv": recv_address, "send": send_address}
            contribution.cli_flags["recv-address"] = json.dumps(recv_address)
            contribution.cli_flags["send-address"] = json.dumps(send_address)
    elif request.role == "connector":
        is_bootstrap_slot = request.slot in ("initial", "final")
        link_direction = request.effective_link_direction or "LD_BIDI"
        connector_recv_creates = (
            is_bootstrap_slot
            and request.direction == "recv"
            and link_direction == "LD_LOADER_TO_CREATOR"
        )

        if connector_recv_creates:
            recv_address = {
                "hashtag": f"twoSixIndirect_{uuid.uuid4().hex[:12]}",
                "hostname": WHITEBOARD_HOSTNAME,
                "port": WHITEBOARD_PORT,
            }
            contribution.address_output = {"recv": recv_address}
            contribution.cli_flags["recv-address"] = json.dumps(recv_address)
        else:
            contribution.needs_peer_address = True
            listener_addresses = next(iter(request.peer_context.values()), None)
            if listener_addresses is None:
                raise ValueError(
                    "meteor-encoding adapter: connector node requires a "
                    "listener's address_output in peer_context, but none was provided"
                )
            if isinstance(listener_addresses, dict) and ("recv" in listener_addresses or "send" in listener_addresses):
                if request.direction == "send":
                    if "recv" in listener_addresses:
                        contribution.cli_flags["send-address"] = json.dumps(listener_addresses["recv"])
                elif request.direction == "recv":
                    if "send" in listener_addresses:
                        contribution.cli_flags["recv-address"] = json.dumps(listener_addresses["send"])
                else:
                    if "recv" in listener_addresses:
                        contribution.cli_flags["send-address"] = json.dumps(listener_addresses["recv"])
                    if "send" in listener_addresses:
                        contribution.cli_flags["recv-address"] = json.dumps(listener_addresses["send"])
            else:
                contribution.cli_flags["send-address"] = json.dumps(listener_addresses)
    else:
        raise ValueError(f"meteor-encoding adapter: unknown role '{request.role}'")

    return contribution