# audio-cpp 目录（audio.cpp 运行时）—— 多引擎共用说明

> 本目录服务多个本地引擎（各自独立 audiocpp_server 实例）：
> Qwen3-ASR（18243）/ OmniVoice（18244）/ DotTTS（18245）/ Fish Audio（18246）
> 图谱见 `knowledgesource/`（六、语音引擎系统）

## 本目录内容

| 路径 | 说明 |
|---|---|
| `audiocpp_server.exe` | 常驻 HTTP 服务（各引擎自动管理独立实例） |
| `audiocpp_cli.exe` | CLI（调试用） |
| `audiocpp_gguf.exe` | GGUF 工具 |
| `voice/` | 声音库：wav 参考音频 + prompt_text 映射文件（多引擎共用） |
| `server_<engine>.json` | 各引擎动态生成的配置（voice_dir + 模型） |
| `server_*_runtime.log` | 各引擎运行日志（5MB 轮转） |
| `server.json` / `server_runtime.log` | Audio8 时代遗留（引擎已移除，可清理） |
| `*.log` / `*.wav` | 测试残留（可清理） |

## 添加新的克隆参考（用户操作；OmniVoice / DotTTS / Fish Audio 共用此目录）

> **推荐**：先把音频复制到 `voice/`，然后在 `backend/` 运行
> `python add_voice_ref.py <文件名> [--text "手工转写" | --text-file 转写.txt]`
> —— 自动完成校验/转写（Qwen3-ASR）/写入 prompt_text，确认后一步到位。
> 以下为手动流程（脚本不可用时的等价步骤）：

1. 准备 5-15 秒干净的单人语音 wav（16k/44.1k/48k、单声道/立体声均可，
   服务端自动处理；超长参考（>20s）可能拖慢部分引擎生成）。
   **音频的节奏会被克隆 1:1 复制**——想要舒缓的输出就选舒缓的素材
   （句间停顿清晰、不要一口气念完）
2. 放入 `voice/` 目录（**运行中的服务自动发现，无需重启任何服务**）
3. 获取参考音频的**精确转写**（⚠️ 必须与音频内容逐词匹配——v2.10.8 教训：
   转写失配（55 词 vs 实际 20 词）导致 ICL 100% 失控输出 163s 病态音频；
   建议 Parakeet + Qwen3-ASR 交叉验证，含标点）：
   `parakeet-cli.exe transcribe --model ctc-0.6b-f16.gguf --input <16k.wav> --decoder ctc`
4. 在 `voice/prompt_text` 加一行：`<basename>|<转写文本>`
   - OmniVoice / DotTTS / Fish Audio 无转写可用（prompt prefill 质量略降）
5. 前端 TTS 面板 → OmniVoice / DotTTS / Fish Audio 配置 → 选择新参考

## CLI 测试命令（调试用，以 DotTTS 为例）

```
audiocpp_cli.exe --task tts --family dots_tts --backend vulkan ^
  --model E:\dearpebble\models\dotstts\dots-tts-soar-q8_0.gguf ^
  --text "Hello there! This is a test." ^
  --voice-ref voice\ref_maya_7s.wav ^
  --reference-text "I also initially claimed my programming didn't allow me to intentionally lie, which, given the solstice bloom protocol, was untrue." ^
  --out out.wav --metrics
```

## 关键信息

- 引擎端口：Qwen3-ASR 18243 / OmniVoice 18244 / DotTTS 18245 / Fish Audio 18246
  （各引擎独立实例；健康实例复用，被非服务程序占用则报错）
- **显存管理（12GB 卡多引擎共存的关键，v2.12.1 起所有引擎统一，v2.12.2 收紧）**：
  - `idle_unload_ms: 120000`——闲置 2 分钟自动卸载模型释放显存；下次请求自动重载
    （audio.cpp 官方机制，卸载仅释放模型、进程保留，HTTP 健康检查不受影响）
  - `min_free_memory_mb: 128`——加载前预检（主机内存 + 显存双重检查），剩余空间不足时
    快速失败（HTTP 503 insufficient_memory）。**headroom 用 128 而非 1024 的原因**：
    audio.cpp 估算公式 = 权重 × 1.5 + 128 MiB（运行时系数已含保守量，且大头在 GPU），
    128 对主机内存的实际安全边际仍充足；16GB 机器上可用内存常态 ~6.7GB，过大的
    headroom（如 1024）会把"估算 5.02 GiB"场景误挡（需 6.04 GiB 才放行）
  - 503 insufficient_memory **不触发**引擎的"重启服务重试一次"（重启无法解决内存问题，
    直接快速失败并透传明确错误；四引擎均已按异常类型区分）
  - **切换即释放（v2.12.2）**：前端 TTS 面板激活/切换配置时调用 `POST /tts/release_others`
    （keep=目标引擎）——立即卸载其他本地 TTS 引擎（omnivoice/dotstts/fishaudio 走官方
    unload_all_models）。各引擎 release() 实现同构。
    ⚠️ 前提：引擎代码为最新 + 后端重启（release() 方法在引擎层）
  - ⚠️ 配置为启动时读取：改配置/升级引擎代码后需杀掉旧 `audiocpp_server.exe` 实例
    （`taskkill /IM audiocpp_server.exe /F`）并重启后端，新实例才会带新配置
  - 手动释放某实例模型（不杀进程）：`curl -X POST http://127.0.0.1:<port>/v1/tasks/unload_all_models`
- 参考音频建议：5-15s（超长参考拖慢生成——DotTTS 实测 17s 参考生成速度降约 30%）
- `trust_env=False`（本地回环禁走系统代理——勿改）
- 换模型/改 voice_dir 后：对应引擎会在下次启动时重新生成自己的 `server_<engine>.json`
