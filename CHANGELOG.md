# Changelog

All notable releases for Pebble Companion Chat are recorded here.
Each `## x.y.z` section is used by `npm run release -- x.y.z` as both the
GitHub Release description and the in-app update panel notes.

## 0.6.0

- Overhauled the generic `audio.cpp` TTS (`audiocpp_tts`) and ASR (`audiocpp`) engine mechanism so users can run any local `model_specs` family with full feature parity:
  - Updated `backend/audio_specs.py` to expand official `audio.cpp` enum `preset` definitions, parse all three option scopes (`requestOptions`, `sessionOptions`, `loadOptions`), map `model_specs` task names (`clone` → `clon`, `design` → `vdes`), infer `pathKind` (`file` / `dir` / `either`), and cover all streaming TTS families in `STREAM_SAMPLE_RATES_BY_FAMILY`.
  - Upgraded `AudioCppTtsEngine` (`backend/audiocpp_tts_engine.py`) and `backend/tts_service.py` to register all supported task routes (`tts`, `clon`, `vdes`) under `max_loaded_models: 1`, forward `load_options` and `<family>.`-prefixed `session_options`, accept both `.gguf` files and multi-file model directories, separate `voice` (reference library) from `voice_id` (`options.voice_id`), map 4xx/503 errors cleanly, and retry once after unexpected server crashes.
  - Upgraded `AudioCppSttEngine` (`backend/audiocpp_stt_engine.py`), `STTRegistry` (`backend/stt_registry.py`), and `STTEnginePayload` (`backend/api_routes.py`) to support directory-based ASR models, persist and apply `requestOptions` / `sessionOptions` / `loadOptions`, support `/v1/tasks/unload_all_models` release, and retry once on crashes.
  - Fixed character voice override (`activeFor` in `frontendV2/src/stores/tts.ts`) so `ttsVoiceRef` properly overrides the generic engine's panel `voiceName`, and passed `tts.specs` to `synthesizeStream` across callers (`BackstagePanel.vue`, `ChatBeat.vue`, `call.ts`).
  - Enhanced `PathPickerModal.vue` (`allowDir`) and `BackstagePanel.vue` to allow selecting either `.gguf` files or model directories, switch tasks (`tts` / `clon` / `vdes`), pick from the shared `voice/` clip list, render all `requestOptions` (removing the 12-item cap) alongside `loadOptions` and `sessionOptions` with path pickers and proper numeric steps, and surface preview/audition errors in the UI instead of silently swallowing them.

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
