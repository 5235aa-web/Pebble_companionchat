# Third-Party Notices

Pebble Companion Chat (P.C.C) is licensed under the Apache License 2.0; see [LICENSE](LICENSE).
The components listed below ship inside this repository and stay under their own
licenses. The Apache License grants no rights to them, and this project's copyright
does not cover them.

Python and npm dependencies installed from `requirements.txt`,
`backend/Inochi2D/requirements.txt`, and `frontendV2/package-lock.json` are not
redistributed from here; they are fetched at build time and carry their own
licenses. Model weights are never committed either: they are downloaded
separately and are subject to the terms of their publishers.

---

## 1. LXGW WenKai (霞鹜文楷)

Files: `frontendV2/src/assets/fonts/LXGWWenKai-Regular.ttf`, `LXGWWenKai-Regular.woff2`,
`LXGWWenKai-Medium.ttf`, `LXGWWenKai-Medium.woff2`

License: SIL Open Font License 1.1. The full license text is shipped next to the
fonts in `frontendV2/src/assets/fonts/OFL.txt`.

Copyright 2021-2026 LXGW (https://github.com/lxgw/LxgwWenKai), with Reserved Font
Name '霞鹜', '霞鶩', '落霞孤鹜', '落霞孤鶩' and 'LXGW'. The Latin and Japanese glyphs
derive from Klee One, Copyright 2020 The Klee Project Authors
(https://github.com/fontworks-fonts/Klee).

The font files here are unmodified copies, and none of the Reserved Font Names is
used as the name of a modified version.

## 2. ONNX Runtime Web

Files: `frontendV2/public/ort/ort-wasm-simd-threaded.mjs`,
`frontendV2/public/ort/ort-wasm-simd-threaded.wasm`

License: MIT. Copyright (c) Microsoft Corporation.

## 3. Silero VAD

Files: `frontendV2/public/models/silero_vad.onnx`

License: MIT. Copyright (c) Silero Team.

## 4. audio.cpp runtime

Files: the whole `backend/audio-cpp/` directory, including `audiocpp_server.exe`,
the MSVC runtime DLLs it links against, and `model_specs/`.

License: Apache License 2.0. Copyright 2026 ShugoAI LLC. The license text ships with
the runtime as `backend/audio-cpp/LICENSE`; `backend/audio-cpp/NOTES.md` documents
how the directory is used.

## 5. See-through (Optional Live2D Layer Decomposition)

Files: the `backend/Inochi2D/see-through/` directory (and the `Inochi2D/see-through/`
headless inference subset bundled into desktop builds via `pebble-backend.spec`).

License: Apache License 2.0. Upstream project: `https://github.com/shitagaki-lab/see-through`
(Jian Lin, Chengze Li, Haoyun Qin, Kwun Wang Chan, Yanghua Jin, Hanyuan Liu,
Stephen Chun Wang Choy, and Xueting Liu, *See-through: Single-image Layer Decomposition
for Anime Characters*, SIGGRAPH 2026). The full license text ships at
`backend/Inochi2D/see-through/LICENSE`.

Included upstream modules inside `backend/Inochi2D/see-through/common/modules/` retain
their original copyright and license headers, including `marigold/` (Copyright 2023-2025
Marigold Team, ETH Zürich, Apache License 2.0), `layerdiffuse/` (`layerdiff3d.py`
Copyright 2025 The HuggingFace Team, Apache License 2.0), and `sam/` (Copyright Meta
Platforms, Inc. and affiliates; `tiny_vit_sam.py` Copyright 2022 Microsoft).

Note: `seethroughv0.0.2_layerdiff3d` and `seethroughv0.0.1_marigold` model weights and
optional PyTorch/diffusers packages from `backend/Inochi2D/requirements.txt` are not
bundled or redistributed by this repository.

---

## MIT license text

Applies to items 2 and 3 above.

```
MIT License

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
