"""
Meteor Steganographic Encoding Component - Raceboat IEncodingComponent plugin.

Implements perfectly-secure steganography (Meteor, Kaptchuk et al., CCS 2021)
with gpt2-medium as the carrier model. Produces text/plain UTF-8 output.

This module combines all Phase 3 PoC components into a single codec file
compatible with the python-encoding-stub pattern.
"""

import json
import base64
import hashlib
import hmac
import time
import os
import bitarray
import numpy as np
import torch as th
import torch.nn.functional as F
from collections import defaultdict
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional

# =============================================================================
# Constants
# =============================================================================

CHUNK_SIZE_BYTES = 48  # 48 bytes = 384 bits per chunk
CONTEXT_WINDOW_TOKENS = 128  # Tokens to carry forward as context for next chunk
DEFAULT_MODEL_PATH = "/opt/raceboat/models/gpt2-medium"  # Pre-cached in runtime image
MODEL_PATH = os.environ.get("METEOR_MODEL_PATH", DEFAULT_MODEL_PATH)
PAYLOAD_LENGTH_BYTES = 4  # 4-byte big-endian length prefix

# =============================================================================
# HKDF for per-chunk nonce derivation
# =============================================================================

def hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    """HKDF-Extract: PRK = HMAC-Hash(salt, IKM)"""
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    """HKDF-Expand: Expand PRK to desired length."""
    t = b""
    okm = b""
    i = 0
    while len(okm) < length:
        i += 1
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        okm += t
    return okm[:length]


def hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    """Full HKDF: Extract then Expand."""
    prk = hkdf_extract(salt, ikm)
    return hkdf_expand(prk, info, length)


def derive_chunk_nonce(base_nonce: bytes, chunk_index: int) -> bytes:
    """Derive per-chunk nonce using HKDF."""
    info = f"meteor-chunk-{chunk_index}".encode()
    return hkdf(b"", base_nonce, info, 64)  # 512-bit nonce


# =============================================================================
# Parameter parsing and validation
# =============================================================================

def parse_params(params_json: str) -> Dict[str, Any]:
    """Parse and validate encoding parameters from JSON string."""
    try:
        params = json.loads(params_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in params: {e}")

    required = ["context", "input_key", "sample_seed_prefix", "input_nonce"]
    for field in required:
        if field not in params:
            raise ValueError(f"Missing required parameter: {field}")

    # Decode base64 fields
    try:
        context = params["context"]
        input_key = base64.b64decode(params["input_key"])
        sample_seed_prefix = base64.b64decode(params["sample_seed_prefix"])
        input_nonce = base64.b64decode(params["input_nonce"])
    except Exception as e:
        raise ValueError(f"Base64 decode error: {e}")

    # Validate lengths
    if len(input_key) != 64:
        raise ValueError(f"input_key must be 512 bits (64 bytes), got {len(input_key)}")
    if len(sample_seed_prefix) != 6:
        raise ValueError(f"sample_seed_prefix must be 48 bits (6 bytes), got {len(sample_seed_prefix)}")
    if len(input_nonce) != 64:
        raise ValueError(f"input_nonce must be 512 bits (64 bytes), got {len(input_nonce)}")

    return {
        "context": context,
        "input_key": input_key,
        "sample_seed_prefix": sample_seed_prefix,
        "input_nonce": input_nonce
    }


# =============================================================================
# Payload framing
# =============================================================================

def frame_payload(payload: bytes) -> bytes:
    """Prefix payload with its length (4 bytes big-endian)."""
    length = len(payload)
    if length >= (1 << 32):
        raise ValueError(f"Payload too large: {length} bytes (max 4GB)")
    return length.to_bytes(PAYLOAD_LENGTH_BYTES, 'big') + payload


def unframe_payload(framed: bytes) -> bytes:
    """Extract payload from framed data."""
    if len(framed) < PAYLOAD_LENGTH_BYTES:
        raise ValueError(f"Framed data too short: {len(framed)} bytes")
    length = int.from_bytes(framed[:PAYLOAD_LENGTH_BYTES], 'big')
    payload = framed[PAYLOAD_LENGTH_BYTES:PAYLOAD_LENGTH_BYTES + length]
    if len(payload) != length:
        raise ValueError(f"Payload truncated: expected {length} bytes, got {len(payload)}")
    return payload


def payload_to_bitarray(payload: bytes) -> bitarray.bitarray:
    """Convert bytes to bitarray (MSB first)."""
    ba = bitarray.bitarray()
    ba.frombytes(payload)
    return ba


def bitarray_to_payload(ba: bitarray.bitarray) -> bytes:
    """Convert bitarray to bytes."""
    return ba.tobytes()


# =============================================================================
# Utility functions (from utils.py)
# =============================================================================

def _to_numpy(arr):
    """Convert torch tensor or numpy array to numpy array."""
    if hasattr(arr, 'detach'):
        return arr.detach().cpu().numpy()
    return np.asarray(arr)


def kl2(q: np.ndarray, p: np.ndarray, prec: int = 18) -> float:
    """KL divergence q||p with numerical stability."""
    q = _to_numpy(q)
    p = _to_numpy(p)
    # Compute log2 only on positive values to avoid 0 * log2(0) = nan
    mask_q = q > 0
    mask_p = p > 0
    log_q = np.zeros_like(q)
    log_p = np.zeros_like(p)
    if mask_q.any():
        log_q[mask_q] = np.log2(q[mask_q])
    if mask_p.any():
        log_p[mask_p] = np.log2(p[mask_p])
    res = q * (log_q - log_p)
    ressum = res.sum()
    return np.around(ressum, decimals=prec)


def entropy2(q: np.ndarray, prec: int = 18) -> float:
    """Entropy with numerical stability."""
    q = _to_numpy(q)
    # Compute log2 only on positive values to avoid 0 * log2(0) = nan
    mask = q > 0
    log_q = np.zeros_like(q)
    if mask.any():
        log_q[mask] = np.log2(q[mask])
    res = q * log_q
    ressum = res.sum()
    return -np.around(ressum, decimals=prec)


def bits2int(bits: List[int]) -> int:
    """Convert list of bits (MSB first) to integer."""
    res = 0
    for i, bit in enumerate(bits):
        res += bit * (2 ** i)
    return res


def int2bits(inp: int, num_bits: int) -> List[int]:
    """Convert integer to list of bits (MSB first)."""
    if num_bits == 0:
        return []
    strlist = ('{0:0%db}' % num_bits).format(inp)
    return [int(strval) for strval in reversed(strlist)]


def num_same_from_beg(bits1: List[int], bits2: List[int]) -> int:
    """Count number of matching bits from the beginning."""
    assert len(bits1) == len(bits2)
    for i in range(len(bits1)):
        if bits1[i] != bits2[i]:
            break
    return i


def is_sent_finish(token_idx: int, enc) -> bool:
    """Check if token ends a sentence."""
    try:
        token = enc.decode([token_idx])
    except Exception:
        token = enc.convert_ids_to_tokens(token_idx)
        if isinstance(token, list):
            token = token[0] if token else ""
    return '.' in token or '!' in token or '?' in token


def encode_context(raw_text: str, enc) -> List[int]:
    """Encode context text to token IDs with BOS (EOS token for GPT-2)."""
    if hasattr(enc, 'encoder'):
        # Legacy pytorch_transformers API
        bos_token_id = enc.encoder['<|endoftext|>']
    else:
        # Modern transformers API (GPT2TokenizerFast)
        bos_token_id = enc.convert_tokens_to_ids('<|endoftext|>')
        if bos_token_id is None:
            bos_token_id = enc.eos_token_id
    context_tokens = [bos_token_id]
    context_tokens += enc.encode(raw_text)
    return context_tokens


# =============================================================================
# DRBG - Deterministic Random Bit Generator using HMAC-SHA512
# =============================================================================

class DRBG:
    """Deterministic Random Bit Generator using HMAC-SHA512."""

    def __init__(self, key: bytes, seed: bytes):
        self.key = key
        self.val = b'\x01' * 64
        self.reseed(seed)
        self.byte_index = 0
        self.bit_index = 0

    def hmac(self, key: bytes, val: bytes) -> bytes:
        return hmac.new(key, val, hashlib.sha512).digest()

    def reseed(self, data: bytes = b''):
        self.key = self.hmac(self.key, self.val + b'\x00' + data)
        self.val = self.hmac(self.key, self.val)
        if data:
            self.key = self.hmac(self.key, self.val + b'\x01' + data)
            self.val = self.hmac(self.key, self.val)

    def generate_bits(self, n: int) -> np.ndarray:
        xs = np.zeros(n, dtype=bool)
        for i in range(n):
            xs[i] = (self.val[self.byte_index] >> (7 - self.bit_index)) & 1
            self.bit_index += 1
            if self.bit_index >= 8:
                self.bit_index = 0
                self.byte_index += 1
            if self.byte_index >= 8:
                self.byte_index = 0
                self.val = self.hmac(self.key, self.val)
        self.reseed()
        return xs


# =============================================================================
# Bin sort for Meteor
# =============================================================================

def bin_sort(l: th.Tensor, token_indices: th.Tensor, total: int, entropy: float, device: str) -> Tuple[th.Tensor, th.Tensor]:
    """Bin sort for Meteor - reorders probability mass to maximize shared prefix bits."""
    num_bins = 2 ** int(entropy + 1)
    bucket_size = total / num_bins

    bins = [th.empty(0, dtype=th.long, device=device) for _ in range(num_bins)]
    value_in_bins = [0] * num_bins
    space_left_after = [total - i * bucket_size for i in range(num_bins)]

    token_bins = [th.empty(0, dtype=th.long, device=device) for _ in range(num_bins)]

    step_size = num_bins / 4
    search_order = []
    priorities = [0] * num_bins
    priority = 0
    search_order.append(int(num_bins / 2))
    search_order.append(0)
    priorities[int(num_bins / 2)] = 0
    priorities[0] = 0
    while step_size >= 1:
        priority += 1
        for x in range(num_bins - int(step_size), -1, -int(step_size * 2)):
            search_order.append(x)
            priorities[x] = priority
        step_size = step_size / 2

    for (item, token_index) in zip(l.tolist(), token_indices.tolist()):
        found_single_bucket_fit = False
        single_bucket_index = -1
        single_bucket_value = bucket_size

        found_multi_bucket_bumpless_fit = False
        multi_bucket_bumpless_index = -1
        multi_bucket_bumpless_value = total

        found_multi_bucket_bumping_fit = False
        multi_bucket_bumping_index = -1
        multi_bucket_bumping_value = total

        for i in search_order:
            if (item > space_left_after[i]):
                continue
            if (value_in_bins[i] >= bucket_size):
                continue

            if (value_in_bins[i] + item > bucket_size):
                space_before_next_block = bucket_size - value_in_bins[i]
                for j in range(i + 1, len(bins)):
                    if (value_in_bins[j] > 0):
                        space_before_next_block = space_before_next_block + (bucket_size - value_in_bins[i])
                        break
                    else:
                        space_before_next_block = space_before_next_block + bucket_size

                if ((not found_multi_bucket_bumpless_fit) or (
                        found_multi_bucket_bumpless_fit and priorities[i] <= priorities[multi_bucket_bumpless_index])):
                    if (space_before_next_block > item and space_before_next_block < multi_bucket_bumpless_value):
                        found_multi_bucket_bumpless_fit = True
                        multi_bucket_bumpless_index = i
                        multi_bucket_bumpless_value = space_before_next_block

                if (item - space_before_next_block < multi_bucket_bumping_value):
                    found_multi_bucket_bumping_fit = True
                    multi_bucket_bumping_index = i
                    multi_bucket_bumping_value = item - space_before_next_block

            if (value_in_bins[i] + item <= bucket_size):
                if (single_bucket_value > value_in_bins[i]):
                    found_single_bucket_fit = True
                    single_bucket_value = value_in_bins[i]
                    single_bucket_index = i

        if (single_bucket_index == multi_bucket_bumpless_index == multi_bucket_bumping_index == -1):
            bins[0] = th.cat((th.tensor([item], device=device), bins[0]), 0)
            token_bins[0] = th.cat((th.tensor([token_index], device=device), token_bins[0]), 0)
            continue

        if found_single_bucket_fit:
            bins[single_bucket_index] = th.cat((bins[single_bucket_index], th.tensor([item], device=device)), 0)
            token_bins[single_bucket_index] = th.cat(
                (token_bins[single_bucket_index], th.tensor([token_index], device=device)), 0)
            value_in_bins[single_bucket_index] += item
            for i in range(0, single_bucket_index + 1):
                space_left_after[i] -= item

        elif found_multi_bucket_bumpless_fit:
            part_in_bucket = bucket_size - value_in_bins[multi_bucket_bumpless_index]
            part_overflow = item - part_in_bucket
            bins[multi_bucket_bumpless_index] = th.cat(
                (bins[multi_bucket_bumpless_index], th.tensor([item], device=device)), 0)
            token_bins[multi_bucket_bumpless_index] = th.cat(
                (token_bins[multi_bucket_bumpless_index], th.tensor([token_index], device=device)), 0)
            value_in_bins[multi_bucket_bumpless_index] = bucket_size

            j = multi_bucket_bumpless_index + 1
            for i in range(0, j):
                space_left_after[i] -= item

            while (part_overflow > 0):
                new_part_overflow = (value_in_bins[j] + part_overflow) - bucket_size
                value_in_bins[j] = min(bucket_size, part_overflow + value_in_bins[j])
                space_left_after[j] -= part_overflow
                part_overflow = new_part_overflow
                j += 1

        else:
            part_in_bucket = bucket_size - value_in_bins[multi_bucket_bumping_index]
            part_overflow = item - part_in_bucket
            bins[multi_bucket_bumping_index] = th.cat(
                (bins[multi_bucket_bumping_index], th.tensor([item], device=device)), 0)
            token_bins[multi_bucket_bumping_index] = th.cat(
                (token_bins[multi_bucket_bumping_index], th.tensor([token_index], device=device)), 0)
            value_in_bins[multi_bucket_bumping_index] = bucket_size

            j = multi_bucket_bumping_index + 1
            for i in range(0, j):
                space_left_after[i] -= item
            while (part_overflow > 0):
                new_part_overflow = (value_in_bins[j] + part_overflow) - bucket_size
                value_in_bins[j] = min(bucket_size, part_overflow + value_in_bins[j])
                space_left_after[j] -= part_overflow
                part_overflow = new_part_overflow
                j += 1

    sorted_tensor = th.cat(bins, 0)
    sorted_tokens = th.cat(token_bins, 0)

    return sorted_tensor, sorted_tokens


# =============================================================================
# METEOR Medium
# =============================================================================

class METEORMedium:
    """Meteor medium using GPT-2 as the generative model."""

    def __init__(
        self,
        model_name: str = 'gpt2-medium',
        device: str = 'cpu',
        precision: int = 32,
        entropy_loss_threshold: float = 0.9,
        probs_top_k: int = 0,
        temp: float = 1.0,
        finish_sent: bool = True,
        local_path: str = None,
        seed: int = None,
        model=None,
        tokenizer=None
    ):
        self.device = device
        self.precision = precision
        self.entropy_loss_threshold = entropy_loss_threshold
        self.probs_top_k = probs_top_k
        self.temp = temp
        self.finish_sent = finish_sent
        self.model_name = model_name

        if seed is None:
            import os
            random_data = os.urandom(4)
            self.seed = int.from_bytes(random_data, byteorder="big")
        else:
            self.seed = seed

        if model is not None and tokenizer is not None:
            self.model = model
            self.enc = tokenizer
        else:
            self.enc, self.model = self._load_model(local_path)

        self.past = None
        self.prev = None
        self.output = []
        self.info = {}
        self.action_labels = None
        self.probs = None

    def _load_model(self, local_path: str = None):
        if local_path:
            from transformers import GPT2LMHeadModel, GPT2TokenizerFast
            model = GPT2LMHeadModel.from_pretrained(local_path, local_files_only=True)
            tokenizer = GPT2TokenizerFast.from_pretrained(local_path, local_files_only=True)
        else:
            from transformers import GPT2LMHeadModel, GPT2TokenizerFast
            model = GPT2LMHeadModel.from_pretrained(self.model_name)
            tokenizer = GPT2TokenizerFast.from_pretrained(self.model_name)

        model.to(self.device)
        model.eval()

        # Add special token IDs
        tokenizer.unk_token = None
        tokenizer.bos_token = None
        tokenizer.eos_token = None

        return tokenizer, model

    def encode_context(self, raw_text: str) -> List[int]:
        return encode_context(raw_text, self.enc)

    def reset(self, context: str = None) -> Tuple[np.ndarray, Dict[str, Any]]:
        from transformers import DynamicCache
        self.past = DynamicCache()
        self.output = []
        if context is None:
            context = ""
        context_tokens = self.encode_context(context)
        context_tensor = th.tensor(context_tokens[-1022:], device=self.device, dtype=th.long)
        self.prev = context_tensor
        return self.step(None)

    def step(self, action: Optional[th.Tensor]) -> Tuple[np.ndarray, Dict[str, Any]]:
        info = {}

        if action is not None:
            if action.item() == -1:
                raise Exception("Action -1 should never be selected!")
            self.prev = th.zeros(1, device="cpu", dtype=th.int32) + action.to("cpu")
            self.output.append(action.item())
            info = {
                "end_of_text": False,
                "end_of_context": False,
                "end_of_line": is_sent_finish(action.item(), self.enc)
            }

        with th.no_grad():
            outputs = self.model(
                self.prev.unsqueeze(0).to(self.device),
                past_key_values=self.past,
                use_cache=True
            )
            logits = outputs.logits
            self.past = outputs.past_key_values

            # Truncate cache to 1022 tokens to stay within GPT-2's 1024 position limit
            if self.past is not None:
                seq_len = self.past.get_seq_length()
                if seq_len > 1022:
                    # Crop removes tokens from the END; use negative to remove from start
                    # We want to keep last 1022, so remove (seq_len - 1022) from start
                    self.past.crop(-(seq_len - 1022))

            # Block endoftext and double newline tokens
            logits[0, -1, -1] = -1e20  # endoftext
            logits[0, -1, 628] = -1e20  # double newline

            # Numerical stability: replace NaN/Inf with large negative values
            logits = th.nan_to_num(logits, nan=-1e20, posinf=-1e20, neginf=-1e20)

            # Ensure contiguous memory layout for sort
            last_logits = logits[0, -1, :].contiguous()
            logits, indices = last_logits.sort(descending=True)
            logits = logits.double()

            probs_all = F.softmax(logits / self.temp, dim=0)
            # Compute entropy in torch to avoid numpy conversion issues
            probs_safe = probs_all.clamp_min(1e-20)
            orig_entropy = -(probs_all * th.log2(probs_safe)).sum().item()

            criteria = []

            if self.entropy_loss_threshold:
                # Numerical stability: avoid log2(0) = -inf
                probs_safe = probs_all.clamp_min(1e-20)
                ent = -th.log2(probs_safe) * probs_all
                ent_full = ent.sum()
                cum_ent = th.cumsum(ent, dim=0)
                i = 0
                while i < cum_ent.shape[0]:
                    if cum_ent[i] / ent_full >= self.entropy_loss_threshold:
                        break
                    i += 1
                criteria.append(i)
                info["k_elt"] = i

            if self.probs_top_k:
                criteria.append(self.probs_top_k)

            if self.precision:
                max_val = 2 ** self.precision
                cur_interval = [0, max_val]
                cur_int_range = cur_interval[1] - cur_interval[0]
                cur_threshold = 1 / cur_int_range
                probs_prec = F.softmax(logits / self.temp, dim=0)
                # Numerical stability: avoid empty nonzero
                nonzero_indices = (probs_prec < cur_threshold).nonzero()
                if nonzero_indices.numel() > 0:
                    k = max(2, nonzero_indices[0].item())
                else:
                    k = max(2, probs_prec.shape[0] - 1)
                criteria.append(k)
                info["k_at"] = k

            k = min(criteria) if criteria else None
            info["k"] = k

            logits = logits[:k]
            indices = indices[:k]
            self.probs = F.softmax(logits / self.temp, dim=0)

            info["logits"] = logits.cpu().numpy().astype(np.longdouble)
            info["log_probs"] = F.log_softmax(logits / self.temp, dim=0).cpu().numpy().astype(np.longdouble)
            info["log_probs_T=1"] = F.log_softmax(logits, dim=0).cpu().numpy().astype(np.longdouble)
            info["indices"] = indices.cpu().numpy()
            self.action_labels = indices

            # Compute entropy in torch to avoid numpy conversion issues
            probs_safe = self.probs.clamp_min(1e-20)
            trunc_entropy = -(self.probs * th.log2(probs_safe)).sum().item()
            info["medium_entropy_raw"] = orig_entropy
            info["medium_entropy"] = trunc_entropy

        self.info = info
        return self.probs.cpu().numpy().astype(np.longdouble), info

    def get_output(self, clean_up: bool = False) -> List[int]:
        if clean_up:
            for i, o in enumerate(reversed(self.output)):
                if is_sent_finish(o, self.enc):
                    self.output = self.output[:-i]
                    break
        return self.output

    def humanify(self, output: List[int]) -> str:
        return "".join([self.enc.decode([o]) for o in output])


# =============================================================================
# Meteor Encoder
# =============================================================================

class METEOREncoder:
    """Meteor encoder - encodes payload bits into stegotext."""

    def __init__(
        self,
        medium: METEORMedium,
        input_key: bytes,
        sample_seed_prefix: bytes,
        input_nonce: bytes,
        precision: int = 32,
        is_sort: int = 1,
        finish_sent: bool = True,
        seed: Optional[int] = None
    ):
        self.medium = medium
        self.context = None
        self.finish_sent = finish_sent
        self.precision = precision
        self.is_sort = is_sort
        self.input_key = input_key
        self.sample_seed_prefix = sample_seed_prefix
        self.input_nonce = input_nonce
        self.seed = seed

    def encode(
        self,
        private_message_bit: bitarray.bitarray,
        context: str = None,
        verbose: bool = False
    ) -> Tuple[str, List[int], Dict[str, Any]]:
        """
        Encode a bitarray payload into stegotext.

        Returns:
            (stegotext, token_ids, stats)
        """
        message = private_message_bit
        enc = self.medium.enc
        device = self.medium.device
        topk = self.medium.probs_top_k
        precision = self.precision
        is_sort = self.is_sort
        finish_sent = self.finish_sent

        if verbose:
            print("Starting encoding...")

        max_val = 2 ** precision
        cur_interval = [0, max_val]  # bottom inclusive, top exclusive

        prev = context
        enc_context = th.LongTensor(self.medium.encode_context(context))
        output = enc_context
        past = None

        total_num = 0
        total_num_for_stats = 0
        total_log_probs = 0
        total_kl = 0.0
        total_entropy_ptau = 0.0
        total_num_sents = 0

        mask_generator = DRBG(self.input_key, self.sample_seed_prefix + self.input_nonce)

        stats = {
            "message_len_bits": len(message),
            "loop_error": 0.0
        }
        stats_traj = defaultdict(list)

        with th.no_grad():
            i = 0
            j = 0
            sent_finish = False

            while i < len(message) or (finish_sent and not sent_finish):
                if j == 0:
                    probs, info = self.medium.reset(context=context)
                else:
                    probs, info = self.medium.step(prev)
                j += 1

                probs = th.from_numpy(probs.astype(np.float64))

                if "kl(sampled|true)" in info:
                    stats_traj["kl(sampled|true)"].append(info["kl(sampled|true)"])
                    stats_traj["kl(uniform|true)"].append(info["kl(uniform|true)"])
                    stats_traj["kl(sampled|uniform)"].append(info["kl(sampled|uniform)"])
                    stats_traj["kl(uniform|sampled)"].append(info["kl(uniform|sampled)"])
                    stats_traj["chisquare_p(sampled|true)"].append(info["chisquare_p(sampled|true)"])
                    stats_traj["chisquare_p(uniform|true)"].append(info["chisquare_p(uniform|true)"])

                stats_traj["medium_entropy_raw"].append(info["medium_entropy_raw"])
                stats_traj["medium_entropy"].append(info["medium_entropy"])
                stats_traj["medium_entropy_over_raw"].append(
                    info["medium_entropy"] / info["medium_entropy_raw"]
                )
                stats_traj["medium_logit_dim"].append(probs.shape[0])

                probs_temp = probs
                log_probs_temp = th.from_numpy(info["log_probs"].astype(np.float64))
                log_probs = th.from_numpy(info["log_probs_T=1"].astype(np.float64))
                probs_temp_int = probs_temp
                indices = th.from_numpy(info["indices"])

                if i >= len(message):
                    selection = 0
                    sent_finish = is_sent_finish(indices[selection].item(), enc)
                    if verbose:
                        print(f"Is Finished is true for i={i}!")
                else:
                    cur_int_range = cur_interval[1] - cur_interval[0]
                    cur_threshold = 1 / cur_int_range

                    probs_temp_int = probs_temp_int / probs_temp_int.sum() * cur_int_range
                    entropy_in_this_distribution = entropy2(probs_temp)
                    probs_temp_int = probs_temp_int.round().long()

                    if is_sort:
                        probs_temp_int, indices = bin_sort(
                            probs_temp_int, indices, cur_int_range,
                            entropy_in_this_distribution, device="cpu"
                        )

                    cum_probs = probs_temp_int.cumsum(0)

                    overfill_index = (cum_probs > cur_int_range).nonzero()
                    if len(overfill_index) > 0:
                        cum_probs = cum_probs[:overfill_index[0]]

                    cum_probs += cur_int_range - cum_probs[-1]

                    probs_final = cum_probs.clone()
                    probs_final[1:] = cum_probs[1:] - cum_probs[:-1]

                    cum_probs += cur_interval[0]

                    message_bits = message[i:i + precision]
                    if i + precision > len(message):
                        message_bits = message_bits + bitarray.bitarray([0] * (i + precision - len(message)))

                    mask_bits = mask_generator.generate_bits(precision)
                    for b in range(len(message_bits)):
                        message_bits[b] = message_bits[b] ^ mask_bits[b]

                    message_idx = bits2int(reversed(message_bits.tolist()))
                    selection = (cum_probs > message_idx).nonzero()[0].item()

                    new_int_bottom = cum_probs[selection - 1] if selection > 0 else cur_interval[0]
                    new_int_top = cum_probs[selection]

                    new_int_bottom_bits_inc = list(reversed(int2bits(new_int_bottom, precision)))
                    new_int_top_bits_inc = list(reversed(int2bits(new_int_top - 1, precision)))

                    num_bits_encoded = num_same_from_beg(new_int_bottom_bits_inc, new_int_top_bits_inc)
                    i += num_bits_encoded

                    total_log_probs += log_probs[selection].item()

                    q = probs_final.double() / probs_final.sum()
                    logq = q.log()
                    total_kl += kl2(q.numpy(), probs[:len(q)].numpy())
                    total_entropy_ptau += entropy_in_this_distribution
                    total_num_for_stats += 1

                    if j > len(private_message_bit) * 100:
                        stats["loop_error"] = 1.0
                        break

                prev = indices[selection].view(1)
                output = th.cat((output, prev))
                total_num += 1

                partial = enc.decode(output[len(enc_context):].tolist())
                if '<eos>' in partial:
                    break

        avg_NLL = -total_log_probs / total_num_for_stats if total_num_for_stats > 0 else 0
        avg_KL = total_kl / total_num_for_stats if total_num_for_stats > 0 else 0
        avg_Hq = total_entropy_ptau / total_num_for_stats if total_num_for_stats > 0 else 0
        words_per_bit = total_num_for_stats / i if i > 0 else 0

        stats["avg_NLL"] = avg_NLL
        stats["avg_KL"] = avg_KL
        stats["avg_Hq"] = avg_Hq
        stats["words_per_bit"] = words_per_bit

        for k, v in stats_traj.items():
            if k in ["kl(sampled|true)", "kl(uniform|true)", "kl(sampled|uniform)", "kl(uniform|sampled)"]:
                continue
            arr = np.array(v)
            stats[k + "/mean"] = arr.mean()
            stats[k + "/std"] = arr.std()
            stats[k + "/80"] = np.sort(arr)[int(len(arr) * 0.8)]
            stats[k + "/20"] = np.sort(arr)[int(len(arr) * 0.2)]
            stats[k + "/95"] = np.sort(arr)[int(len(arr) * 0.95)]
            stats[k + "/5"] = np.sort(arr)[int(len(arr) * 0.05)]

        if "kl(sampled|true)" in stats_traj:
            idx = 0
            while idx < len(stats_traj["kl(sampled|true)"]):
                stats[f"kl(sampled|true)_it{idx}"] = stats_traj["kl(sampled|true)"][idx]
                stats[f"kl(uniform|true)_it{idx}"] = stats_traj["kl(uniform|true)"][idx]
                stats[f"kl(uniform|sampled)_it{idx}"] = stats_traj["kl(uniform|sampled)"][idx]
                stats[f"kl(sampled|uniform)_it{idx}"] = stats_traj["kl(sampled|uniform)"][idx]
                stats[f"chisquare_p(sampled|true)_it{idx}"] = stats_traj["chisquare_p(sampled|true)"][idx]
                stats[f"chisquare_p(uniform|true)_it{idx}"] = stats_traj["chisquare_p(uniform|true)"][idx]
                idx += 100

        stats["n_steps"] = j
        stats["bits_per_step"] = len(private_message_bit) / float(j) if j > 0 else 0
        stats["steps_per_bit"] = j / float(len(private_message_bit)) if len(private_message_bit) > 0 else 0
        stats["eff"] = len(private_message_bit) / sum(stats_traj["medium_entropy"]) if stats_traj["medium_entropy"] else 0
        stats["eff_raw"] = len(private_message_bit) / sum(stats_traj["medium_entropy_raw"]) if stats_traj["medium_entropy_raw"] else 0
        stats["eff_output"] = len(private_message_bit) / len(output[len(enc_context):]) if len(output) > len(enc_context) else 0

        token_ids = output[len(enc_context):].tolist()
        stegotext = enc.decode(token_ids)

        return stegotext, token_ids, stats


# =============================================================================
# Meteor Decoder
# =============================================================================

class METEORDecoder:
    """Meteor decoder - decodes stegotext back to payload bits."""

    def __init__(
        self,
        medium: METEORMedium,
        input_key: bytes,
        sample_seed_prefix: bytes,
        input_nonce: bytes,
        precision: int = 32,
        is_sort: int = 1,
        finish_sent: bool = True,
        seed: Optional[int] = None
    ):
        self.medium = medium
        self.context = None
        self.finish_sent = finish_sent
        self.precision = precision
        self.is_sort = is_sort
        self.input_key = input_key
        self.sample_seed_prefix = sample_seed_prefix
        self.input_nonce = input_nonce
        self.seed = seed

    def decode(
        self,
        public_message_token: List[int],
        private_message_bitlen: int,
        context: str = None,
        verbose: bool = False,
        **kwargs
    ) -> Tuple[bitarray.bitarray, Dict[str, Any]]:
        """
        Decode stegotext tokens back to payload bits.

        Returns:
            (recovered_bitarray, stats)
        """
        inp = public_message_token
        enc = self.medium.enc
        device = self.medium.device
        precision = self.precision
        is_sort = self.is_sort
        finish_sent = self.finish_sent
        max_val = 2 ** precision
        cur_interval = [0, max_val]

        mask_generator = DRBG(self.input_key, self.sample_seed_prefix + self.input_nonce)

        stats_traj = defaultdict(list)
        stats = {"public_message_len": len(public_message_token)}

        enc_context = self.medium.encode_context(context)
        enc_context = th.tensor(enc_context[-1022:], device=device, dtype=th.long)
        prev = enc_context
        message = []

        with th.no_grad():
            i = 0
            j = 0
            t_iter_1 = None

            while i < len(inp):
                if t_iter_1 is not None:
                    delta_t_step_no_medium = time.time() - t_iter_1
                    stats_traj["dec_t_step_no_medium"].append(delta_t_step_no_medium)

                t_medium_1 = time.time()

                if j == 0:
                    probs, info = self.medium.reset(context=context)
                else:
                    probs, info = self.medium.step(prev)

                j += 1
                probs = th.from_numpy(probs.astype(np.float64))

                delta_t_medium = time.time() - t_medium_1
                stats_traj["dec_t_medium_per_step"].append(delta_t_medium)
                t_iter_1 = time.time()

                probs_temp = probs
                indices = th.from_numpy(info["indices"])
                probs_temp_int = probs_temp

                cur_int_range = cur_interval[1] - cur_interval[0]
                k = len(probs)

                probs_temp_int = probs_temp_int / probs_temp_int.sum() * cur_int_range
                entropy_in_this_distribution = entropy2(probs_temp)

                probs_temp_int = probs_temp_int.round().long()

                if is_sort:
                    probs_temp_int, indices = bin_sort(
                        probs_temp_int, indices, cur_int_range,
                        entropy_in_this_distribution, device="cpu"
                    )

                cum_probs = probs_temp_int.cumsum(0)

                overfill_index = (cum_probs > cur_int_range).nonzero()
                if len(overfill_index) > 0:
                    cum_probs = cum_probs[:overfill_index[0]]
                    k = overfill_index[0].item()

                cum_probs += cur_int_range - cum_probs[-1]
                cum_probs += cur_interval[0]

                rank = (indices == inp[i]).nonzero().item()

                is_bpe_error = 0
                if rank >= k:
                    true_token_text = enc.decode([inp[i]])
                    for rank_idx in range(k):
                        prop_token_text = enc.decode([indices[rank_idx].item()])

                        if inp[i] == 128 and indices[rank_idx] == 198:
                            rank = rank_idx
                            inp[i] = indices[rank_idx].item()
                            break

                        if len(prop_token_text) <= len(true_token_text) and \
                                prop_token_text == true_token_text[:len(prop_token_text)]:
                            rank = rank_idx
                            suffix = true_token_text[len(prop_token_text):]
                            suffix_tokens = enc.encode(suffix)
                            inp[i] = indices[rank_idx].item()
                            inp[i + 1:i + 1] = suffix_tokens
                            break

                        elif len(prop_token_text) > len(true_token_text) and \
                                true_token_text == prop_token_text[:len(true_token_text)]:
                            whole_text = true_token_text
                            num_extra = 1
                            while len(whole_text) < len(prop_token_text):
                                whole_text += enc.decode([inp[i + num_extra]])
                                num_extra += 1
                            if prop_token_text == whole_text[:len(prop_token_text)]:
                                rank = rank_idx
                                inp[i] = indices[rank_idx].item()
                                for j in range(1, num_extra):
                                    del inp[i + j]
                                if len(whole_text) > len(prop_token_text):
                                    suffix = whole_text[len(prop_token_text):]
                                    suffix_tokens = enc.encode(suffix)
                                    inp[i + 1:i + 1] = suffix_tokens
                                break
                    else:
                        if verbose:
                            print(f'Unable to fix BPE error: token received: {true_token_text}={inp[i]}')
                        rank = 0
                        is_bpe_error = 1

                stats_traj["BPE_ERROR"].append(is_bpe_error)

                selection = rank

                new_int_bottom = cum_probs[selection - 1] if selection > 0 else cur_interval[0]
                new_int_top = cum_probs[selection]

                new_int_bottom_bits_inc = list(reversed(int2bits(new_int_bottom, precision)))
                new_int_top_bits_inc = list(reversed(int2bits(new_int_top - 1, precision)))

                num_bits_encoded = num_same_from_beg(new_int_bottom_bits_inc, new_int_top_bits_inc)

                if i == len(inp) - 1:
                    new_bits = new_int_bottom_bits_inc
                else:
                    new_bits = new_int_top_bits_inc[:num_bits_encoded]

                mask_bits = mask_generator.generate_bits(precision)
                for b in range(len(new_bits)):
                    new_bits[b] = new_bits[b] ^ mask_bits[b]
                message += new_bits

                prev = th.tensor([inp[i]], device=device, dtype=th.long)
                i += 1

        for k, v in stats_traj.items():
            arr = np.array(v)
            stats[k + "/mean"] = arr.mean()
            stats[k + "/std"] = arr.std()

        output = bitarray.bitarray(message)
        if private_message_bitlen is not None:
            output = output[:private_message_bitlen]

        return output, stats


# =============================================================================
# Main Codec Class with Chunking Support
# =============================================================================

class MeteorCodec:
    """Main codec class with chunking support for payloads > 48 bytes."""

    def __init__(self, model_path: str = MODEL_PATH, device: str = "cpu"):
        self.model_path = model_path
        self.device = device
        self._medium = None
        self._shared_model = None
        self._shared_tokenizer = None

    def _get_fresh_medium(self) -> METEORMedium:
        """Get a fresh medium instance with shared model/tokenizer but clean state."""
        # Load shared model/tokenizer once
        if self._shared_model is None or self._shared_tokenizer is None:
            from transformers import GPT2LMHeadModel, GPT2TokenizerFast
            self._shared_model = GPT2LMHeadModel.from_pretrained(self.model_path, local_files_only=True)
            self._shared_model.to(self.device)
            self._shared_model.eval()
            self._shared_tokenizer = GPT2TokenizerFast.from_pretrained(self.model_path, local_files_only=True)
            self._shared_tokenizer.unk_token = None
            self._shared_tokenizer.bos_token = None
            self._shared_tokenizer.eos_token = None

        # Create new medium with shared model/tokenizer (passed to constructor to avoid reload)
        medium = METEORMedium(
            model_name="gpt2-medium",
            device=self.device,
            precision=32,
            entropy_loss_threshold=0.9,
            probs_top_k=0,
            temp=1.0,
            finish_sent=True,
            local_path=self.model_path,
            model=self._shared_model,
            tokenizer=self._shared_tokenizer
        )
        return medium

    def encode(self, payload: bytes, params_json: str) -> bytes:
        """
        Encode payload bytes to stegotext (UTF-8 bytes).

        Handles chunking for payloads > 48 bytes.
        Frames payload with 4-byte length prefix for self-delimiting decode.
        Zero-message encoding: empty payload produces valid stegotext encoding
        of the 4-byte length prefix (0), which decodes back to empty bytes.
        """
        # Frame payload with length prefix (always, including empty payload)
        # Empty payload -> framed_payload = b'\x00\x00\x00\x00' (length=0)
        framed_payload = frame_payload(payload)

        params = parse_params(params_json)
        context = params["context"]
        input_key = params["input_key"]
        sample_seed_prefix = params["sample_seed_prefix"]
        base_nonce = params["input_nonce"]

        # Convert framed payload to bitarray
        payload_bits = payload_to_bitarray(framed_payload)

        # Split into chunks
        chunks = []
        for i in range(0, len(payload_bits), CHUNK_SIZE_BYTES * 8):
            chunk_bits = payload_bits[i:i + CHUNK_SIZE_BYTES * 8]
            chunks.append(chunk_bits)

        # If single chunk, encode directly
        if len(chunks) == 1:
            medium = self._get_fresh_medium()
            chunk_nonce = derive_chunk_nonce(base_nonce, 0)
            encoder = METEOREncoder(
                medium=medium,
                input_key=input_key,
                sample_seed_prefix=sample_seed_prefix,
                input_nonce=chunk_nonce,
                precision=32,
                is_sort=1,
                finish_sent=True
            )
            stegotext, _, _ = encoder.encode(chunks[0], context=context)
            return stegotext.encode('utf-8')

        # Multi-chunk encoding
        stegotext_parts = []
        current_context = context

        for chunk_idx, chunk_bits in enumerate(chunks):
            # Derive per-chunk nonce
            chunk_nonce = derive_chunk_nonce(base_nonce, chunk_idx)

            medium = self._get_fresh_medium()
            encoder = METEOREncoder(
                medium=medium,
                input_key=input_key,
                sample_seed_prefix=sample_seed_prefix,
                input_nonce=chunk_nonce,
                precision=32,
                is_sort=1,
                finish_sent=True
            )

            stegotext, token_ids, _ = encoder.encode(chunk_bits, context=current_context)
            stegotext_parts.append(stegotext)

            # Update context for next chunk: last CONTEXT_WINDOW_TOKENS tokens
            if chunk_idx < len(chunks) - 1:
                token_ids_to_use = token_ids[-CONTEXT_WINDOW_TOKENS:]
                current_context = medium.enc.decode(token_ids_to_use)

        # Concatenate chunk stegotexts directly (NO DELIMITER)
        # The decoder uses length prefix to determine chunk boundaries
        full_stegotext = "".join(stegotext_parts)
        return full_stegotext.encode('utf-8')

    def decode(self, stegotext_bytes: bytes, params_json: str) -> bytes:
        """
        Decode stegotext (UTF-8 bytes) back to payload bytes.

        Chunk boundaries are implicit via length prefix (no delimiter in stegotext).
        Zero-message decoding: empty stegotext returns empty payload.
        """
        import time  # local import to avoid circular dependency issues

        # Zero-message decoding: empty stegotext -> empty payload
        if not stegotext_bytes:
            return b""

        stegotext = stegotext_bytes.decode('utf-8')
        params = parse_params(params_json)
        context = params["context"]
        input_key = params["input_key"]
        sample_seed_prefix = params["sample_seed_prefix"]
        base_nonce = params["input_nonce"]

        medium = self._get_fresh_medium()
        token_ids = medium.enc.encode(stegotext)

        # Step 1: Decode first chunk to get framed payload length
        first_chunk_nonce = derive_chunk_nonce(base_nonce, 0)
        decoder = METEORDecoder(
            medium=medium,
            input_key=input_key,
            sample_seed_prefix=sample_seed_prefix,
            input_nonce=first_chunk_nonce,
            precision=32,
            is_sort=1,
            finish_sent=True
        )

        first_chunk_bitlen = CHUNK_SIZE_BYTES * 8  # 384 bits
        first_chunk_bits, _ = decoder.decode(
            token_ids,
            private_message_bitlen=first_chunk_bitlen,
            context=context
        )

        # Extract framed payload length from first 32 bits
        first_chunk_bytes = bitarray_to_payload(first_chunk_bits)
        if len(first_chunk_bytes) < PAYLOAD_LENGTH_BYTES:
            raise ValueError(f"First chunk too short for length prefix: {len(first_chunk_bytes)} bytes")
        payload_length = int.from_bytes(first_chunk_bytes[:PAYLOAD_LENGTH_BYTES], 'big')
        expected_framed_length = PAYLOAD_LENGTH_BYTES + payload_length

        # Calculate total chunks needed
        num_chunks = (expected_framed_length + CHUNK_SIZE_BYTES - 1) // CHUNK_SIZE_BYTES

        # If only 1 chunk, we're done
        if num_chunks == 1:
            framed_payload = first_chunk_bytes[:expected_framed_length]
            return unframe_payload(framed_payload)

        # Step 2: Continue decoding remaining chunks
        all_payload_bits = self._decode_all_chunks(
            token_ids=token_ids,
            context=context,
            input_key=input_key,
            sample_seed_prefix=sample_seed_prefix,
            base_nonce=base_nonce,
            num_chunks=num_chunks,
            medium=medium
        )

        # Convert back to bytes and unframe
        payload_bits = bitarray.bitarray(all_payload_bits)
        framed_payload = bitarray_to_payload(payload_bits)

        if len(framed_payload) < expected_framed_length:
            raise ValueError(f"Decoded data truncated: expected {expected_framed_length} bytes, got {len(framed_payload)}")

        valid_framed = framed_payload[:expected_framed_length]
        return unframe_payload(valid_framed)

    def _decode_all_chunks(self, token_ids: list, context: str, input_key: bytes,
                           sample_seed_prefix: bytes, base_nonce: bytes,
                           num_chunks: int, medium: METEORMedium) -> list:
        """
        Decode all chunks by manually stepping through tokens and switching nonces
        at chunk boundaries (every 384 bits).
        """
        device = medium.device
        precision = 32
        is_sort = 1
        finish_sent = True
        max_val = 2 ** precision
        chunk_bitlen = CHUNK_SIZE_BYTES * 8  # 384

        enc = medium.enc

        # Reset medium
        medium.past = None
        medium.output = []
        enc_context = medium.encode_context(context)
        enc_context = th.tensor(enc_context[-1022:], device=device, dtype=th.long)
        prev = enc_context

        all_message_bits = []
        bits_recovered_in_current_chunk = 0
        current_chunk = 0
        chunk_nonce = derive_chunk_nonce(base_nonce, current_chunk)

        mask_generator = DRBG(input_key, sample_seed_prefix + chunk_nonce)

        cur_interval = [0, max_val]

        with th.no_grad():
            for token_idx, token in enumerate(token_ids):
                # Check if we need to switch to next chunk
                if bits_recovered_in_current_chunk >= chunk_bitlen and current_chunk < num_chunks - 1:
                    current_chunk += 1
                    bits_recovered_in_current_chunk = 0
                    chunk_nonce = derive_chunk_nonce(base_nonce, current_chunk)
                    mask_generator = DRBG(input_key, sample_seed_prefix + chunk_nonce)

                # Run medium step (same as decoder)
                if token_idx == 0:
                    probs, info = medium.reset(context=context)
                else:
                    probs, info = medium.step(prev)

                probs = th.from_numpy(probs.astype(np.float64))
                probs_temp = probs
                indices = th.from_numpy(info["indices"])
                probs_temp_int = probs_temp

                cur_int_range = cur_interval[1] - cur_interval[0]

                probs_temp_int = probs_temp_int / probs_temp_int.sum() * cur_int_range
                entropy_in_this_distribution = entropy2(probs_temp)
                probs_temp_int = probs_temp_int.round().long()

                if is_sort:
                    probs_temp_int, indices = bin_sort(
                        probs_temp_int, indices, cur_int_range,
                        entropy_in_this_distribution, device="cpu"
                    )

                cum_probs = probs_temp_int.cumsum(0)
                overfill_index = (cum_probs > cur_int_range).nonzero()
                if len(overfill_index) > 0:
                    cum_probs = cum_probs[:overfill_index[0]]

                cum_probs += cur_int_range - cum_probs[-1]
                cum_probs += cur_interval[0]

                # Find rank of current token
                rank_matches = (indices == token).nonzero()
                if len(rank_matches) == 0:
                    # Token not in top-k - this shouldn't happen with correct encoding
                    rank = 0
                else:
                    rank = rank_matches.item()

                selection = rank
                new_int_bottom = cum_probs[selection - 1] if selection > 0 else cur_interval[0]
                new_int_top = cum_probs[selection]

                # Convert to bits (same as encoder/decoder)
                new_int_bottom_bits = [(new_int_bottom >> b) & 1 for b in range(precision)]
                new_int_top_bits = [((new_int_top - 1) >> b) & 1 for b in range(precision)]

                # Find common prefix
                num_bits = 0
                for b in range(precision):
                    if new_int_bottom_bits[b] == new_int_top_bits[b]:
                        num_bits += 1
                    else:
                        break

                # Use top bits (excluding last token special case)
                if token_idx == len(token_ids) - 1:
                    new_bits = new_int_bottom_bits
                else:
                    new_bits = new_int_top_bits[:num_bits]

                # Apply mask
                mask_bits = mask_generator.generate_bits(precision)
                for b in range(len(new_bits)):
                    new_bits[b] = new_bits[b] ^ mask_bits[b]

                all_message_bits.extend(new_bits)
                bits_recovered_in_current_chunk += len(new_bits)

                # Update interval
                cur_interval = [new_int_bottom, new_int_top]
                prev = th.tensor([token], device=device, dtype=th.long)

                # Stop if we've recovered enough bits for all chunks
                if len(all_message_bits) >= num_chunks * chunk_bitlen:
                    break

        return all_message_bits


# =============================================================================
# Module-level functions for python-encoding-stub pattern
# =============================================================================

_codec_instance = None


def get_codec() -> MeteorCodec:
    """Get or create singleton codec instance."""
    global _codec_instance
    if _codec_instance is None:
        _codec_instance = MeteorCodec()
    return _codec_instance


def encode(payload: bytes, parameters: str) -> bytes:
    """Encode payload using Meteor steganography."""
    codec = get_codec()
    return codec.encode(payload, parameters)


def decode(content: bytes, parameters: str) -> bytes:
    """Decode stegotext back to payload."""
    codec = get_codec()
    return codec.decode(content, parameters)