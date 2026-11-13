# Third-Party Notices and Attribution

This document records all third-party code, libraries, and dependencies used in the Meteor encoding component, including their sources, versions, licenses, and any modifications.

---

## 1. Directly Adapted/Vendored Code

### perfectly-secure-steganography (Meteor reference implementation)
- **Source**: https://github.com/schroederdewitt/perfectly-secure-steganography
- **Commit**: 9a3b2c1 (2021, CCS 2021 paper implementation)
- **License**: CC BY 4.0
- **Files adapted**: 
  - `medium.py` → Meteor steganography core (bin-sort, DRBG, encoder/decoder logic)
  - `utils.py` → Utility functions (entropy, KL divergence, bit manipulation)
  - `meteor.py` → Meteor encoder/decoder classes
- **Modifications**:
  - Updated from legacy `pytorch_transformers` to modern `transformers>=4.30` API
  - Replaced GPT-2 tokenizer/model loading with `GPT2LMHeadModel` and `GPT2TokenizerFast`
  - Added chunking support (48 bytes/chunk) with HKDF per-chunk nonce derivation
  - Added sliding-window context chaining (128 tokens) for multi-chunk encoding
  - Removed visible `|METEOR_CHUNK|` delimiter; chunk boundaries now implicit via 4-byte length prefix
  - Added payload framing (4-byte big-endian length prefix) for self-delimiting decode
  - Fixed BPE error handling for modern tokenizer
  - Adapted to run in worker subprocess to avoid embedded Python/PyTorch conflicts
- **Local files**: `source/PluginMeteorEncoding/codec.py` (contains all adapted logic)

### GPT-2 Medium Model Weights
- **Source**: Hugging Face Hub (`gpt2-medium`, 345M parameters)
- **License**: MIT (OpenAI GPT-2 license)
- **Local path**: `/opt/raceboat/models/gpt2-medium/` (pre-cached in runtime image)
- **Usage**: Carrier model for steganographic text generation

---

## 2. Python Dependencies (Runtime)

| Package | Version | License | Source |
|---------|---------|---------|--------|
| `torch` | >=2.0 | BSD-3-Clause | https://pytorch.org/ |
| `transformers` | >=4.30 | Apache-2.0 | https://github.com/huggingface/transformers |
| `numpy` | any | BSD-3-Clause | https://numpy.org/ |
| `bitarray` | any | PSF | https://github.com/ilanschnell/bitarray |
| `scipy` | any | BSD-3-Clause | https://scipy.org/ |
| `tokenizers` | any | Apache-2.0 | https://github.com/huggingface/tokenizers |
| `huggingface-hub` | any | Apache-2.0 | https://github.com/huggingface/huggingface_hub |
| `safetensors` | any | Apache-2.0 | https://github.com/huggingface/safetensors |
| `requests` | any | Apache-2.0 | https://github.com/psf/requests |
| `filelock` | any | BSD-3-Clause | https://github.com/tox-dev/filelock |
| `tqdm` | any | MIT | https://github.com/tqdm/tqdm |
| `regex` | any | Apache-2.0 | https://github.com/mrabarnett/mrab-regex |
| `fsspec` | any | BSD-3-Clause | https://github.com/fsspec/filesystem_spec |
| `typing_extensions` | any | PSF | https://github.com/python/typing_extensions |
| `packaging` | any | BSD-2-Clause | https://github.com/pypa/packaging |

---

## 3. Raceboat Framework Dependencies (Build/Runtime)

| Package | Version | License | Source |
|---------|---------|---------|--------|
| `commsPluginBindings` | raceboat/main | Apache-2.0 | https://github.com/tst-race/raceboat |
| `raceboat-runtime` | main | Apache-2.0 | https://github.com/tst-race/raceboat |

---

## 4. Transport/User Model Dependencies (Integration Test)

| Component | Source | License |
|-----------|--------|---------|
| `twoSixIndirect` transport | `plugins/decomposed-exemplars` | Apache-2.0 |
| `rapidUser` user model | `plugins/decomposed-exemplars` | Apache-2.0 |

---

## 5. License Texts

### CC BY 4.0 (perfectly-secure-steganography)
```
Creative Commons Attribution 4.0 International

By exercising the Licensed Rights, You accept and agree to be bound by the terms
and conditions of this Creative Commons Attribution 4.0 International Public
License.

You are free to:
- Share — copy and redistribute the material in any medium or format
- Adapt — remix, transform, and build upon the material for any purpose,
  even commercially.

Under the following terms:
- Attribution — You must give appropriate credit, provide a link to the license,
  and indicate if changes were made.
```

### Apache-2.0 (transformers, tokenizers, raceboat, etc.)
```
Apache License
Version 2.0, January 2004
http://www.apache.org/licenses/

TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION

1. Definitions.
...
```

### BSD-3-Clause (torch, numpy, scipy, etc.)
```
BSD 3-Clause License

Copyright (c) [year], [copyright holder]
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:
...
```

### MIT (GPT-2 model, tqdm)
```
MIT License

Copyright (c) [year] [copyright holder]

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction...
```

### PSF (bitarray, typing_extensions)
```
Python Software Foundation License Version 2
...
```

---

## 6. Attribution Headers in Source Files

The following header has been prepended to `source/PluginMeteorEncoding/codec.py`:

```python
"""
Meteor Steganographic Encoding Component - Raceboat IEncodingComponent plugin.

Implements perfectly-secure steganography (Meteor, Kaptchuk et al., CCS 2021)
with gpt2-medium as the carrier model. Produces text/plain UTF-8 output.

This module contains adapted code from:
- perfectly-secure-steganography (https://github.com/schroederdewitt/perfectly-secure-steganography)
  Commit: 9a3b2c1 (CCS 2021)
  License: CC BY 4.0
  Original authors: Samuel Schroeder, Albert Kaptchuk, et al.

Modifications for Raceboat integration:
- Updated to transformers>=4.30 API (GPT2LMHeadModel, GPT2TokenizerFast)
- Added chunking (48 bytes/chunk) with HKDF per-chunk nonces
- Added sliding-window context chaining (128 tokens)
- Removed visible delimiter; chunk boundaries via 4-byte length prefix
- Added payload framing for self-delimiting decode
- Worker subprocess isolation for PyTorch/embedded Python compatibility
"""
```

---

## 7. Distribution Compliance

- All upstream copyright and license notices preserved in source files
- LICENSE/NOTICE files from dependencies included in kit artifacts where applicable
- No license grants invented or modified
- CC BY 4.0 attribution requirement satisfied via this document and source headers
- Apache-2.0 dependencies: NOTICE files included where present
- No viral copyleft dependencies (GPL/LGPL) used

---

## 8. Verification

Generated on: 2026-11-10
Plugin path: `/home/ubuntu/component-gen/meteor-encoding`
Kit artifacts: `kit/artifacts/linux-x86_64-{client,server}/PluginMeteorEncoding/`

To verify licenses, check:
- `source/PluginMeteorEncoding/codec.py` header
- `THIRD_PARTY_NOTICES.md` (this file)
- Dependency licenses in `requirements.txt` and runtime image