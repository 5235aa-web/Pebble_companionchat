# Changelog

All notable releases for Pebble Companion Chat are recorded here.
Each `## x.y.z` section is used by `npm run release -- x.y.z` as both the
GitHub Release description and the in-app update panel notes.

## 1.0.3

- **Group Chat Voice Input**:
  - Added a microphone button to the group room composer to record and send voice messages directly, or convert speech into editable text in the input box before sending.
  - Supports selecting the next speaker by clicking a character card or member pill during recording, or sending directly for automatic speaker routing based on your spoken message.
  - Forwards native audio when supported by the active model (with automatic fallback to transcribed text), while always storing the transcript for room context, shared memory, and compaction.
- **Voice Call Interruption Fixes**:
  - Fixed Silero VAD runtime loading so speaking while the AI is talking reliably lowers volume, interrupts playback, and returns the call to listening.
  - Fixed interruption handling for non-streaming TTS playback and added a manual **Interrupt** button on the voice call panel while the AI is speaking.

## 1.0.2

- **CHARX Asset Tag Repair & Deduplication**:
  - Generalized `normalize_asset_tag_refs` / `normalizeAssetTagRefs` (`backend/card_presentation.py`, `frontendV2/src/utils/inlineMedia.ts`) to unwrap inline backticks around asset tags, recover space-separated or prefix-omitted unique asset names, repair standalone malformed LLM tags (`<mg(...)>`, `<firm=...>`, `<img "name">`, `<img src="typo">`, `{{mg::...}}`), and strip unresolvable or duplicate malformed tags before display and prompt replay.
- **Image-Heavy Thread Scroll & Jump Fixes**:
  - Removed `content-visibility: auto` height collapse on settled beats (`05-thread.css`) and eager-sized inline rich images (`richText.ts`), and added layout-settling scroll tracking in `MessageThread.vue`, `ThreadJumpRail.vue`, and `stores/chat.ts` so "Jump to present" and message jumps land accurately with images present.
- **Stage Backdrop & Stage HUD Cleanup**:
  - Simplified the static right-hand `ScenePanel` to always use the default Gaussian-blurred portrait backdrop (`08-scene.css`, `20-picture-studio.css`), and removed the Stage HUD `scene` picker and `scene:` prompt cue (`backend/card_hud.py`, `CardHudPanel.vue`, `stores/cardHud.ts`) while keeping Live2D stage behavior unchanged.
- **Lazy Inochi2D Imports for CI & Packaged Builds**:
  - Deferred `numpy`/`psd_tools` imports inside `backend/Inochi2D/service.py` with external-venv subprocess fallback for `.inx` imports (`inx_importer.py`, `pebble-backend.spec`), and guarded optional ML/CV tests in `test/test_inochi2d_upgrade.py` so core startup and CI pass without `numpy` or `cv2`.

## 1.0.1

- **Character Card Translation (Chinese & English)**:
  - Added isolated LLM-powered translation for character cards (`POST /companions/{id}/translate`), storing `translations`, `active_locale`, and `prompt_use_original` separately from original card columns so source cards, `raw_card_data`, and PNG/JSON exports remain untouched.
  - Translates natural-language display fields only (`description`, `personality`, `scenario`, `tags`, `first_mes`, `alternate_greetings`, `creator_notes`) while preserving the character's original `name`, prompt-override instructions (`system_prompt`, `post_history_instructions`, `advanced_prompt`, `user_info`, `mes_example`), placeholders (`{{char}}`, `{{user}}`), CBS macros, HTML/CSS tags, and `embedded://` asset paths.
  - Added an editor translation bar in **Character Editor** (`Original / 中文 / EN` switcher, one-click streaming translation with progress & cancel, manual translation editing, per-field retranslation, and read-only protection on original name/system fields) plus a quick language toggle pill in the right-hand **Scene** panel.
  - Wired active translations seamlessly across single chat, script mode, greetings, cast list, and group chat (including group roster/character prompts and persona relevance routing), with an **Always use original fields for Prompt** option when you want translated UI/greetings while keeping original prompt fields for the AI.
- **Unified `data-tip` Tooltips**:
  - Replaced all remaining browser-native `title` tooltips across the frontend (`ScenePanel`, `CardHudPanel`, `GroupStage`, `SceneRulesDock`, `ImageGallery`, `InlineVideoCard`, `BackstagePanel`, `AttachmentChipStrip`, `CharacterEditor`, `PuppetEditorPopup`, and `UpdateButton`) with the app's themed `data-tip` style.
  - Added a viewport-clamped `FloatingTip` portal layer so tooltips inside scrollable, `overflow: hidden`, truncated-ellipsis, or edge-anchored containers never clip or spill off-screen.
- **Live2D Rigging, Multi-Pose & Layer Recovery Upgrades**:
  - Upgraded 2D puppet animation and rigging (`puppet2d.ts`, `puppetActions.ts`, `PuppetRigOverlay.vue`) with Marigold depth relief, skeletal anchors for whole-body pose reactions, live FFT vowel lip-sync (`speechMonitor.ts`, `pcmPlayer.ts`), and themed editor controls (`UiSelect`, `UiSlider`, `.puppet-check`).
  - Added multi-pose slots with mood/keyword auto-matching (`pose_manager.py`, `api_routesV2.py`) alongside direct lossless layered imports from `.psd` and Inochi2D `.inx` files (`psd_importer.py`, `inx_importer.py`).
  - Added source-grounded post-decomposition repair (`layer_repair.py`) to recover missing garments/shoulders via ink-barrier segmentation, fix source-contradicting `zIndex` overlaps and eye-layer order, prune hallucinated accessory blobs, and retain detached limb parts in `part_lr_split` / `load_part`.
  - Hardened worker cleanup (`backend/Inochi2D/service.py`, `engine_shutdown.py`, `backend/main.py`) with Windows Kill-on-Job-Close binding, process-tree termination on cancel/exit, and ASGI lifespan/`atexit` shutdown hooks.

## 1.0.0

- **Conversation Branch Export**:
  - Added per-branch export directly from the top-bar branch menu (next to each branch's rename action), saving all exported files to `.pebblecompanion/outputchat`.
  - Supports four formats: **Markdown (`.md`)** for clean reading, **HTML (`.html`)** with full restoration of status bars, card CSS, inline styles, and embedded images (including RisuAI/CHARX card templates), **Pebble JSON (`.json`)** for lossless backups, and **SillyTavern JSONL (`.jsonl`)** for ecosystem compatibility.
  - Added an option to include or exclude compacted message history alongside compaction and script-mode scene summaries.
- **SillyTavern JSONL Conversation Import**:
  - Added **+ import JSONL** in the top-bar branch menu to import SillyTavern (and Pebble-exported) `.jsonl` transcripts into a new branch, preserving multi-variant swipes, timestamps, and card variables while skipping `is_system` lines by default and warning if the source character name differs.

## 0.9.0

- **Scene Rules Workshop (No-Regex Immersive Rule Builder)**:
  - Added a quick-access **Scene Rules** dock right above the chat input bar that smoothly expands upward without covering messages and automatically switches with your active character.
  - Added three effortless ways to customize immersion without writing regex: plain-language AI rule creation (with automatic sample testing), offline visual block builders, and one-click recipes for time/turn awareness, inner-thought cards, mood badges, and OOC cleanup.
  - Added one-click AI explanations for imported card rules, live macro variable controls, and an interactive preview sandbox.
- **Stage HUD Upgrades (Dual-Layer Stage & Micro-Console)**:
  - Decoupled background scenes from foreground character expressions so portrait emotion switches no longer overwrite the active stage backdrop.
  - Turned the Stage HUD into a live micro-console with custom tracked field add/remove controls, searchable inline portrait/scene/BGM switchers (plus SFX preview and volume/mute controls), a compact layout toggle, 90%-opacity readable panels, and a collapsed pill showing location, outfit, and an affection micro-bar.
  - Added an optional **Unspoken Thought** frosted veil that appears selectively on emotionally resonant or intimate moments (tap to peek) and subtle mood-reactive lighting on the HUD shimmer and stage edges.
  - Connected Stage HUD state bidirectionally with **Scene Rules Workshop** and card variables, allowing HUD-triggered rules/recipes and pinned story variables with inline `-/+` steppers on the HUD.
- **Richer CHARX Card Visuals & Audio**:
  - Added support for inline `<audio>` playback, decorative `<svg>` graphics, CSS background/asset `url(...)` references, `{{raw::...}}` media tags, and expanded styling rules so richly formatted CHARX cards display and sound as intended.
  - Stripped hidden `<head>` / `<title>` metadata from rendered cards and applied card variables and display rules to card stylesheets.
- **Expanded Immersive Macro & Card Script Support**:
  - Added support for RisuAI temporary variables, loops and asset lists, conditional logic, text helpers, and live chat time/idle/turn macros used in interactive greetings and roleplay cards.
  - Added support for card input/output rewrite scripts, context-only history cleanup rules (keep rich HTML visible on screen while stripping clutter before sending to the AI), and full script preservation when importing `.risum` modules.
- **Expression & Asset Typo Recovery**:
  - Added smart matching for minor character or emotion asset typos in AI replies and greetings, preventing broken asset messages and missing expressions.

## 0.8.0

- **Character Card & Stage HUD Isolation**:
  - Removed unsupported `.charx` export paths from the frontend (`CharacterEditor.vue`, `CastDrawer.vue`, `exportCompanion` in `frontendV2/src/api/companion.ts`, and `card.exportCharx` i18n entries), narrowing `exportCompanion` to `'json' | 'png'` and exporting active drawer cards as `.json`.
  - Filtered out the top `PNG` / `JSON` export buttons in `CharacterEditor.vue` when viewing or editing a `charx` card (`isCharxCard`).
  - Scoped the Stage HUD (`CardHudPanel` in `ScenePanel.vue`) via `showCardHud` so it only renders for `charx` cards or cards carrying media assets (`assetCount > 0` or `session.companionAssets.length > 0`), hiding it on plain PNG/JSON cards.
- **Picture Studio Cleanup**:
  - Removed the `ComfyUI` provider description note in `backend/picturesys/config.py` and updated `BackstagePanel.vue` to conditionally render `.pic-note` text only when non-empty.
- **Single-Chat & Group Compaction Panel Improvements & Timestamp Localization**:
  - Exposed `summary_full` alongside the 200-character `summary` preview in `GET /companions/{companion_id}/branches/{branch_id}/compaction-history` (`backend/api_routes.py`), and added a `more` / collapse toggle in `CompactPanel.vue` (`api/compaction.ts`, `data/compaction.ts`, `09-panels.css`, `i18n`) while keeping the preview behavior intact.
  - Switched group compaction summaries in `CompactPanel.vue` (`21-group.css`) to a 200-character preview by default (omitting excess text from the DOM until expanded) with click-on-summary toggling to view the full text directly without a separate `more` button.
  - Replaced the flat "View raw messages" list in `CompactPanel.vue` (`13-desk.css`) with a lazy-mounted **batch accordion** for both single-chat and group rooms (`getCompactedBatches` in `api/compaction.ts`, `compacted_batches()` and `batches` in `backend/group_sessions/db.py` & `routes.py`, `GroupCompactedBatch` in `api/group.ts`), keeping batches collapsed by default and mounting only the selected batch's messages.
  - Unified local-timezone timestamp formatting (`formatDateTime`) for group compaction entries in `CompactPanel.vue` and group memory entries in `MemoryPanel.vue`.

## 0.7.0

- Added an optional, standalone **Live2D / 2D Puppet** pipeline (`backend/Inochi2D` + WebGL2 stage) so users can decompose single portraits or same-pose expression/outfit packs locally with `See-through` (`LayerDiff3D` + `Marigold`) and animate them in real time without altering the core backend runtime:
  - Isolated dependencies via `backend/Inochi2D/requirements.txt` (running inside `backend/Inochi2D/.venv`, `PEBBLE_INOCHI_PYTHON`, or a user-selected `inochiPythonPath` in `app.json`) so core features work with zero PyTorch installation when Live2D is unused.
  - Added cross-GPU and cross-platform execution in `backend/Inochi2D/run_decompose.py` supporting `ROCm` (Windows/Linux with chunked math SDPA), `NVIDIA CUDA` (Ampere+ `bf16`, Turing/Pascal `fp16`), `Apple Silicon MPS`, and `CPU` (`fp32`) without editing upstream `see-through` files, plus `--offload auto` in `backend/Inochi2D/service.py` to keep `group_offload` enabled below 20 GB VRAM (fixing 12 GB VRAM stalls) and cleanup of temporary `_work` files after completion.
  - Automated anatomical post-processing in `backend/Inochi2D/puppet_builder.py` and `backend/Inochi2D/expression_extractor.py`: automatic `headTilt` alignment, `neck`/`topwear` V-neck collar occlusion separation (`_fix_neck_topwear_occlusion`), generated bilateral closed-eye `blinkLayers`, pixel-diff expression overlay splitting with bilingual (`zh`/`en`) filename normalization (`_resolve_emotion_meta`), occluded outer-eye/brow blind-spot completion (`--no_complete_eye` opt-out), and incremental outfit merging (`add_outfit_from_psd`) under `outfits/<outfit_id>/`.
  - Built a WebGL2 interactive 2D puppet stage and non-modal floating editor (`frontendV2/src/lib/puppet2d.ts`, `PuppetStage.vue`, `PuppetEditorPopup.vue`, `stores/inochi2d.ts`, `api/inochi2d.ts`, `23-inochi2d.css`) supporting shaped breathing waves, 0.26s expression crossfades, micro-blinks, spring hair/accessory physics, temple-side front-hair restraint, soft global gaze tracking, voice lip-sync via `getSpeechLevel()`, pixel `hitTest` interactions (head pat, cheek poke/pet with blush shader & shy dodge, chest tap startled recoil), and live layer/outfit switching.
  - Added multi-image pack & incremental outfit APIs (`POST /inochi2d/build-pack`, `DELETE /inochi2d/outfit` in `backend/Inochi2D/routes.py`) alongside UI workflows in `PuppetEditorPopup.vue` and `CharacterEditor.vue` (plus fixed `isCharxCard` in `frontendV2/src/data/chat.ts`) so non-CHARX PNG/JSON cards can upload expression and outfit images into `companion_assets` without changing `card_format`, and added live runtime/weight diagnostics (`inochiPythonPath`, `LayerDiff3D`, `Marigold`) in `BackstagePanel.vue`, `stores/ui.ts`, `backend/main.py`, `i18n/zh.ts`, and `i18n/en.ts`.
  - Added packaged desktop support (`pebble-backend.spec`, `inochi2d_dir()` in `backend/paths.py`, `_runtime_dir()` and persistent `inochi2d-requirements.txt` export in `backend/Inochi2D/service.py`) by bundling the worker scripts and headless `see-through/common` + `see-through/inference/scripts` (~1.2 MB) so frozen builds can run decomposition via an external Python venv without bundling PyTorch.
  - Updated `.gitignore` to track `backend/Inochi2D/see-through/` while ignoring `.venv/` and personal `juanhui/` test assets.
- Added **Video Attachment Understanding, Throttled Playback & Gallery Support** across single-chat, group rooms, and the media album without bundling an external FFmpeg binary:
  - Added FFmpeg-first probing and execution (`ffmpeg_path()` and `is_ffmpeg_usable()` in `backend/paths.py`, `GET/POST /app/config` `ffmpegPath` in `backend/main.py`, and Path settings + status in `BackstagePanel.vue`) resolving `PEBBLE_FFMPEG_PATH`, `app.json`, `DATA_ROOT/bin`, system `PATH`, and common Windows package-manager paths; automatically falls back to browser `<video>` + `<canvas>` keyframe sampling (`frontendV2/src/utils/video.ts`) via HTTP `422` negotiation when FFmpeg is absent or fails at runtime.
  - Extended `backend/attachment_parser.py`, `backend/api_routes.py`, `backend/companion.py`, `backend/companion_db.py`, and `backend/group_sessions/` (`db.py`, `routes.py`, `prompt.py`) to accept `.mp4/.m4v/.webm/.mov/.mkv/.avi/.flv/.wmv/.ts` attachments (up to 100 MB), extract dense sequential frames for continuous video understanding (2–4 fps up to `180` frames via single-pass FFmpeg or browser fallback), store poster/frame previews (cleaned up automatically on delete), inject structured video timelines + ordered frame `image_url` parts into the active turn only, and fall back to text-only context when text-only providers reject `image_url` (`_IMAGE_REJECTED`).
  - Made video speech recognition (STT) opt-in and off by default (`videoAutoStt: false` in `app.json`, toggleable under `BackstagePanel.vue` Ears tab) so visual-only uploads do not spin up ASR VRAM; added `POST /companions/attachments/video/transcribe` plus a per-chip toggle (`AttachmentChipStrip.vue`, `useAttachmentQueue.ts`) to transcribe or strip spoken transcripts before sending, alongside drag-and-drop and clipboard paste support in `ChatComposer.vue` and `GroupStage.vue`.
  - Upgraded `InlineVideoCard.vue` and `ImageGallery.vue` (with global `ui.activeVideoId` coordination in `stores/ui.ts`) for strict throttled video rendering: unplayed videos render lightweight poster thumbnails without mounting any `<video>` element or fetching full video bytes, only mount `<video>` on explicit play, allow at most one active video globally, and automatically unload `<video>` on close, item/tab switch, gallery close, or offscreen scroll; added per-session filename deduplication (`collect_gallery_media` in `backend/attachment_parser.py`, `backend/companion_db.py`, and `backend/group_sessions/db.py`) so repeated sends of the same named image/video only appear once in the gallery.

## 0.6.0

- Overhauled the generic `audio.cpp` TTS (`audiocpp_tts`) and ASR (`audiocpp`) engine mechanism so users can run any local `model_specs` family with full feature parity:
  - Updated `backend/audio_specs.py` to expand official `audio.cpp` enum `preset` definitions, parse all three option scopes (`requestOptions`, `sessionOptions`, `loadOptions`), map `model_specs` task names (`clone` → `clon`, `design` → `vdes`), infer `pathKind` (`file` / `dir` / `either`), and cover all streaming TTS families in `STREAM_SAMPLE_RATES_BY_FAMILY`.
  - Upgraded `AudioCppTtsEngine` (`backend/audiocpp_tts_engine.py`) and `backend/tts_service.py` to register all supported task routes (`tts`, `clon`, `vdes`) under `max_loaded_models: 1`, forward `load_options` and `<family>.`-prefixed `session_options`, accept both `.gguf` files and multi-file model directories, separate `voice` (reference library) from `voice_id` (`options.voice_id`), map 4xx/503 errors cleanly, and retry once after unexpected server crashes.
  - Upgraded `AudioCppSttEngine` (`backend/audiocpp_stt_engine.py`), `STTRegistry` (`backend/stt_registry.py`), and `STTEnginePayload` (`backend/api_routes.py`) to support directory-based ASR models, persist and apply `requestOptions` / `sessionOptions` / `loadOptions`, support `/v1/tasks/unload_all_models` release, and retry once on crashes.
  - Fixed character voice override (`activeFor` in `frontendV2/src/stores/tts.ts`) so `ttsVoiceRef` properly overrides the generic engine's panel `voiceName`, and passed `tts.specs` to `synthesizeStream` across callers (`BackstagePanel.vue`, `ChatBeat.vue`, `call.ts`).
  - Enhanced `PathPickerModal.vue` (`allowDir`) and `BackstagePanel.vue` to allow selecting either `.gguf` files or model directories, switch tasks (`tts` / `clon` / `vdes`), pick from the shared `voice/` clip list, render all `requestOptions` (removing the 12-item cap) alongside `loadOptions` and `sessionOptions` with path pickers and proper numeric steps, and surface preview/audition errors in the UI instead of silently swallowing them.
- Overhauled the local LLM mechanism across managed `llama-cpp` and external local OpenAI-compatible backends (`ollama`, `lmstudio`, `local-openai`):
  - Fixed `LlamaServerEngine` (`backend/llama_server_engine.py`) and `backend/paths.py` so stopped state no longer masquerades as a hardcoded built-in model, `resolve_default_model_key()` prefers ready custom GGUF models, `ensure_running()` lazily starts or switches the managed server on demand before LLM requests, OOM launch failures automatically unload local TTS/STT VRAM and retry once, quantized KV cache (`q8_0`/`q4_0`) guards against disabled FlashAttention, custom model edits support `old_key` renaming, and `llama-server` executable detection works cross-platform.
  - Improved `scan_gguf_models()` and `describe_models()` to consolidate split multi-part GGUF shards (`-00001-of-NNNNN`), sum full shard sizes, match `mmproj` and `mtp` files by stem similarity within mixed directories, and deduplicate colliding keys across subdirectories.
  - Unified provider/model lookup (`backend/llm_client.py`, `backend/companion.py`, `backend/api_routes.py`, `backend/compaction.py`, `backend/scene_compaction.py`, `backend/token_engine.py`, `backend/group_sessions/routes.py`) to read `LLM_CONFIG_PATH`, resolve `localModelKey` and per-model `modelParams`, propagate custom GGUF `ctx` and `maxContext` into token accounting and compaction budgets (with conservative limits for unknown local models), include `local-openai` in local token counters, and gracefully strip unsupported extended fields on `400/422` for strict external local servers while enabling local sampling and thinking suppression across `ollama`, `lmstudio`, and `local-openai`.
  - Updated `frontendV2` (`BackstagePanel.vue`, `stores/llm.ts`, `stores/chat.ts`, `api/llm.ts`, `api/stream.ts`, i18n) to expose MTP draft path selection and the `enableMtp` toggle, enforce KV/FlashAttention consistency in the GGUF editor, keep `requestModel` aligned with the selected local model, pass `max_context`/`enable_mtp` in requests, and display local sampling controls for `ollama`, `lmstudio`, and `local-openai`.

## 0.5.0

- Fixed Picture Studio `Auto picture in story mode` (`autoScript`): story-mode chat turns in `/companions/chat/stream` now trigger `maybe_auto(..., source="script")` alongside scene advances and HUD updates, and the frontend (`App.vue` and `useScriptStore`) now polls and refreshes the stage when `autoScript` is enabled.
- Split the text chat streaming routes (`/chat/stream`, `/chat/continue/stream`, `/chat/regenerate/stream`, `/chat/reedit/stream`) out of `backend/api_routes.py` into `backend/api_routesV2.py` mounted onto `api_routes.router`.
- Fixed `[data-tip]` tooltip clipping across the Backstage settings panel (`BackstagePanel.vue`): unified wrapped, inward-anchored tooltips for all Backstage tabs (including Voice/TTS `audio.cpp` request options and Picture Studio parameters), prevented `overflow: hidden` from clipping the loaded-model hint via `.lb-note-wrap`, and anchored `.local-bar` tooltips downward with right-edge alignment on the release button.

## 0.4.0

- Multiple improvements. It can now directly access the picture generation model and workflow within your comfyui application, and also supports the injection of prompt template.

## 0.3.0

- Added support for RisuAI shorthand `<img="...">` tags in CHARX greetings and chat messages so bundled character expressions and inline images render properly instead of being stripped.
- Deferred greeting image parsing and loading until the empty-stage greeting view is visible and switched to a greeting containing images, reducing unnecessary rendering work.
- Updated `README.md` Section 3 (Character Cards) with documentation on CHARX containers, bundled asset extraction, inline media, and the Stage HUD.
- Fixed single-chat token mismatches between the reply footer and the top context meter by persisting branch-level provider usage in SQLite, separating latest context size from cumulative continuation billing, and refreshing context counts after swipe, edit, and delete actions.
- Added CBS variable & conditional macro evaluation (`{{getvar::...}}`, `{{setvar::...}}`, `{{? ...}}`, `{{#if}}` / `{{#if_pure}}`), visual-only Display Regex support (SillyTavern `regex_scripts` & RisuAI `customScripts`), and a Shadow DOM card sandbox allowing sanitized card CSS (`backgroundHTML` / `<style>`) and interactive `<input>`/`<label>` controls, along with per-branch variable and card rule management in the character editor.
- Added RisuAI `.risum` (RPack) module support and CSS auto-frame bridging: standalone `.risum` / `module.json` files can be imported directly from the character editor (compatible with both web and Tauri via multipart/base64 JSON endpoints), `.charx` imports automatically unpack embedded `*.risum` / `module.json` modules (Display Regex, CSS, default variables, and assets), and cards with recognized asset-frame CSS automatically synthesize wrapper Display Regex rules when no `<img` regex is bundled.
- Added zero-dependency WebGL 2.5D mesh micro-motion (`SceneMotionCanvas`) for the right-hand chat scene stage, featuring automatic heuristic foreground/background masking and focal anchor detection, torso breathing, subtle head/drape sway, cursor depth parallax, three-plane atmospheric light particles, crossfade transitions on slide changes, and persistent `2.5D Motion` + `Vivid` / `Subtle` toggle controls.

## 0.2.0

- After completing the import of the CHARX cards and enabling the usage mechanism, now CHARX card import is supported, along with the display of its complete media resources, including pictures, audio, and videos, which can all be viewed in the character card editing window. These resources are lazy-loaded, so they will not affect the rendering. 
- Additionally, the issue of unable to immediately display attachment pictures in the front-end chat interface has been fixed.

## 0.1.0

- Initial release of Pebble Companion Chat (P.C.C).
