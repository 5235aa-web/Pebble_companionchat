import atexit
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from .paths import (
    LOCAL_LLM_CONFIG_PATH,
    LOG_DIR,
    llama_dir,
    models_root,
    resolve_user_path,
)
from . import audio_cpp_job

logger = logging.getLogger('llamaserver')

# =============================================================================
# 路径与运行参数
# =============================================================================

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent

# 运行时与模型权重都在 dearpebble 项目下：companionchat 不复制 60MB 的可执行文件
# 和 8.5GB 权重，只负责进程生命周期。需要整体迁移时改 LLAMA_SHARED_ROOT 即可。
# Weights come from the model root the user picked in the app; the runtime is a
# read-only resource that ships next to the executable.
_MODELS_ROOT = models_root()

LLAMA_DIR = llama_dir()
SERVER_EXE = LLAMA_DIR / 'llama-server.exe'
SERVER_LOG = LOG_DIR / 'llama_server_runtime.log'

MODEL_DIR = Path(os.getenv('LLAMA_MODEL_DIR', str(_MODELS_ROOT / 'gemma')))
MODEL_PATH = Path(os.getenv('LLAMA_SERVER_MODEL', str(MODEL_DIR / 'gemma-4-12b-heretic-Q5_K_M.gguf')))
MMPROJ_PATH = Path(os.getenv('LLAMA_SERVER_MMPROJ', str(MODEL_DIR / 'gemma-4-12b-heretic-mmproj-f16.gguf')))

# 历史遗留的模型别名。现在对外暴露的名字由 MODEL_REGISTRY 的 key 决定，
# 改这个环境变量不再影响 --alias；保留它只为让旧启动脚本仍能读到同一个常量。
MODEL_ID = os.getenv('LLAMA_SERVER_MODEL_ID', 'gemma-4-12b-heretic')

# ---------------------------------------------------------------------------
# 模型注册表
# ---------------------------------------------------------------------------
# 一块 12GB 卡同一时刻只能驻留一份权重，切换模型等价于 stop -> start，冷加载 10-20 秒。
# key 同时充当 llama-server 的 --alias，llm_providers.json 的 models 列表要与之对应。
GEMMA412B_DIR = Path(os.getenv('LLAMA_GEMMA412B_DIR', str(_MODELS_ROOT / 'gemma412b')))
ROCINANTE_DIR = Path(os.getenv('LLAMA_ROCINANTE_DIR', str(_MODELS_ROOT / 'Rocinante-X')))

# 每个条目自带 ctx：-c 是启动时一次性分配的 KV 预算，两个模型体积不同，能承受的上限也不同。
# 原 heretic 是 Q5_K_M（8.5 GiB），在 12GB 上开 128K 已接近饱和；新模型是 Q4_K_M（6.87 GiB），
# 且全局层只有 1 个 KV head、40 个滑窗层恒定 1024，256K 的 KV 约 1.16 GiB，仍有富余。
MODEL_REGISTRY: Dict[str, Dict[str, Any]] = {
    'gemma-4-12b-heretic': {
        'label': 'Gemma 4 12B Heretic',
        'detail': 'Q5_K_M · 视觉 · 128K',
        'model': MODEL_PATH,
        'mmproj': MMPROJ_PATH,
        'mtp': None,
        'ctx': int(os.getenv('LLAMA_SERVER_CTX', '131072')),
    },
    'gemma4-12b-uncensored': {
        'label': 'Gemma 4 12B QAT Uncensored',
        'detail': 'Q4_K_M · 视觉 · 256K · HauhauCS Balanced',
        'model': GEMMA412B_DIR / 'Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M.gguf',
        'mmproj': GEMMA412B_DIR / 'mmproj-Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-BF16.gguf',
        'mtp': GEMMA412B_DIR / 'mtp-gemma-4-12B-it.gguf',
        'ctx': int(os.getenv('LLAMA_GEMMA412B_CTX', '262144')),
    },
    # Mistral Nemo 12B 基座的 RP 微调，标准 GQA：每层 8 个 KV head、head_dim 128，
    # KV 按 token 线性增长（q8_0 约 83 KiB/token），所以 -c 必须按显存倒推，
    # 不能照抄模型的 128K 声明。Q5_K_M 权重 8.13 GiB，24K 上下文配 q8_0 KV 约 2.0 GiB。
    # 纯文本权重，目录内没有 mmproj；也没有独立的 MTP 草稿头。
    'rocinante-x-12b-v1': {
        'label': 'Rocinante X 12B v1',
        'detail': 'Q5_K_M · 纯文本 · 24K · Mistral Nemo RP',
        'model': ROCINANTE_DIR / 'Rocinante-X-12B-v1b-Q5_K_M.gguf',
        'mmproj': None,
        'mtp': None,
        'ctx': int(os.getenv('LLAMA_ROCINANTE_CTX', '24576')),
    },
}

DEFAULT_MODEL_KEY = os.getenv('LLAMA_SERVER_MODEL_KEY', 'gemma-4-12b-heretic')

# Built-in relative paths under models_root(), used when resolving dynamically
# after the user changes modelsRoot at runtime (unless env vars override them).
_BUILTIN_REL: Dict[str, Dict[str, Optional[str]]] = {
    'gemma-4-12b-heretic': {
        'model': 'gemma/gemma-4-12b-heretic-Q5_K_M.gguf' if not os.getenv('LLAMA_SERVER_MODEL') else None,
        'mmproj': 'gemma/gemma-4-12b-heretic-mmproj-f16.gguf' if not os.getenv('LLAMA_SERVER_MMPROJ') else None,
        'mtp': None,
    },
    'gemma4-12b-uncensored': {
        'model': 'gemma412b/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M.gguf' if not os.getenv('LLAMA_GEMMA412B_DIR') else None,
        'mmproj': 'gemma412b/mmproj-Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-BF16.gguf' if not os.getenv('LLAMA_GEMMA412B_DIR') else None,
        'mtp': 'gemma412b/mtp-gemma-4-12B-it.gguf' if not os.getenv('LLAMA_GEMMA412B_DIR') else None,
    },
    'rocinante-x-12b-v1': {
        'model': 'Rocinante-X/Rocinante-X-12B-v1b-Q5_K_M.gguf' if not os.getenv('LLAMA_ROCINANTE_DIR') else None,
        'mmproj': None,
        'mtp': None,
    },
}


def _llama_dir() -> Path:
    return llama_dir()


def _server_exe() -> Path:
    return _llama_dir() / 'llama-server.exe'


def _load_custom_models() -> Dict[str, Dict[str, Any]]:
    try:
        if not LOCAL_LLM_CONFIG_PATH.is_file():
            return {}
        data = json.loads(LOCAL_LLM_CONFIG_PATH.read_text(encoding='utf-8'))
        items = data.get('models') if isinstance(data, dict) else data
        if not isinstance(items, list):
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            key = str(item.get('key') or '').strip()
            model_raw = str(item.get('model') or item.get('path') or '').strip()
            if not key or not model_raw:
                continue
            out[key] = {
                'key': key,
                'label': str(item.get('label') or key).strip(),
                'detail': str(item.get('detail') or 'Custom GGUF').strip(),
                'model': model_raw,
                'mmproj': str(item.get('mmproj') or '').strip() or None,
                'mtp': str(item.get('mtp') or '').strip() or None,
                'ctx': int(item.get('ctx') or 32768),
                'ngl': int(item['ngl']) if item.get('ngl') is not None and str(item.get('ngl')).strip() != '' else None,
                'device': str(item.get('device') or '').strip() or None,
                'kv_type': str(item.get('kv_type') or '').strip() or None,
                'flash_attn': item.get('flash_attn') if isinstance(item.get('flash_attn'), bool) else True,
                'jinja': item.get('jinja') if isinstance(item.get('jinja'), bool) else True,
            }
        return out
    except Exception as exc:
        logger.warning('读取自定义本地 LLM 配置失败: %s', exc)
        return {}


def _save_custom_models(entries: List[Dict[str, Any]]) -> None:
    LOCAL_LLM_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = LOCAL_LLM_CONFIG_PATH.with_suffix('.json.tmp')
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump({'models': entries}, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, LOCAL_LLM_CONFIG_PATH)


def upsert_custom_model(payload: Dict[str, Any]) -> Dict[str, Any]:
    key = str(payload.get('key') or '').strip()
    if not key:
        raise ValueError('key 不能为空')
    raw_model = str(payload.get('model') or payload.get('path') or '').strip()
    if not raw_model:
        raise ValueError('模型路径不能为空')
    existing = list(_load_custom_models().values())
    entry: Dict[str, Any] = {
        'key': key,
        'label': str(payload.get('label') or key).strip(),
        'detail': str(payload.get('detail') or '').strip() or 'Custom GGUF',
        'model': raw_model,
        'mmproj': str(payload.get('mmproj') or '').strip(),
        'mtp': str(payload.get('mtp') or '').strip(),
        'ctx': max(512, int(payload.get('ctx') or 32768)),
        'ngl': int(payload['ngl']) if payload.get('ngl') is not None and str(payload.get('ngl')).strip() != '' else None,
        'device': str(payload.get('device') or '').strip(),
        'kv_type': str(payload.get('kv_type') or '').strip(),
        'flash_attn': bool(payload.get('flash_attn', True)),
        'jinja': bool(payload.get('jinja', True)),
    }
    updated = [e for e in existing if e['key'] != key]
    updated.append(entry)
    _save_custom_models(updated)
    return entry


def delete_custom_model(key: str) -> bool:
    existing = list(_load_custom_models().values())
    kept = [e for e in existing if e['key'] != key]
    if len(kept) == len(existing):
        return False
    _save_custom_models(kept)
    return True


def _resolve_spec(key: str, spec: Dict[str, Any], is_builtin: bool) -> Dict[str, Any]:
    resolved = dict(spec)
    if is_builtin:
        rel = _BUILTIN_REL.get(key) or {}
        if rel.get('model') and not os.getenv('LLAMA_MODEL_DIR'):
            resolved['model'] = models_root() / rel['model']
        if rel.get('mmproj') and not os.getenv('LLAMA_MODEL_DIR'):
            resolved['mmproj'] = models_root() / rel['mmproj']
        if rel.get('mtp'):
            resolved['mtp'] = models_root() / rel['mtp']
    else:
        resolved['model'] = resolve_user_path(str(spec['model']))
        resolved['mmproj'] = resolve_user_path(str(spec['mmproj'])) if spec.get('mmproj') else None
        resolved['mtp'] = resolve_user_path(str(spec['mtp'])) if spec.get('mtp') else None
    return resolved


def get_merged_registry() -> Dict[str, Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for key, spec in MODEL_REGISTRY.items():
        s = _resolve_spec(key, spec, is_builtin=True)
        s['builtin'] = True
        merged[key] = s
    for key, spec in _load_custom_models().items():
        s = _resolve_spec(key, spec, is_builtin=False)
        s['builtin'] = key in MODEL_REGISTRY
        merged[key] = s
    return merged


def scan_gguf_models(root: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Scan up to depth 3 under *root* (default ``models_root()``) for GGUF LLM files."""
    base = root or models_root()
    if not base.is_dir():
        return []
    hits: List[Dict[str, Any]] = []
    # Skip audio.cpp subdirectories/files by heuristics & inspect depth <= 3
    for dirpath, dirnames, filenames in os.walk(base):
        rel_parts = Path(dirpath).relative_to(base).parts
        if len(rel_parts) >= 3:
            dirnames[:] = []
        ggufs = sorted(f for f in filenames if f.lower().endswith('.gguf'))
        if not ggufs:
            continue
        mmprojs = [f for f in ggufs if 'mmproj' in f.lower()]
        mtps = [f for f in ggufs if f.lower().startswith('mtp-') or '-mtp-' in f.lower()]
        for fname in ggufs:
            low = fname.lower()
            if 'mmproj' in low or low.startswith('mtp-') or '-mtp-' in low:
                continue
            full = Path(dirpath) / fname
            try:
                size_gb = round(full.stat().st_size / (1024 ** 3), 2)
            except OSError:
                size_gb = 0.0
            stem = full.stem
            key = re.sub(r'[^a-zA-Z0-9._-]+', '-', stem).strip('-').lower() or 'local-model'
            hits.append({
                'key': key,
                'label': stem,
                'path': str(full),
                'relPath': str(full.relative_to(base)),
                'mmproj': str(Path(dirpath) / mmprojs[0]) if mmprojs else '',
                'mtp': str(Path(dirpath) / mtps[0]) if mtps else '',
                'sizeGb': size_gb,
            })
    return hits

HOST = '127.0.0.1'
SERVER_PORT = int(os.getenv('LLAMA_SERVER_PORT', '8080'))

# 上下文与卸载策略：默认 128K（用户选定），改环境变量即可切换档位
N_CTX = int(os.getenv('LLAMA_SERVER_CTX', '131072'))
N_GPU_LAYERS = int(os.getenv('LLAMA_SERVER_NGL', '99'))
# 单用户对话场景：显式固定 1 个 slot。slot 数自动时 llama-server 会开 4 个并行槽，
# compute buffer 与调度状态按 4 份准备，在 12GB 卡上会触发 fit_params 显存不足告警。
N_PARALLEL = int(os.getenv('LLAMA_SERVER_PARALLEL', '1'))
# 视觉编码（mmproj）的 image_max_tokens 会被 n_ubatch 压低，512 时图片预算只剩 512 而非 1120
UBATCH = int(os.getenv('LLAMA_SERVER_UBATCH', '1024'))
KV_TYPE = os.getenv('LLAMA_SERVER_KV_TYPE', 'q8_0')         # f16 / q8_0
OFFLOAD_EMBEDDING = os.getenv('LLAMA_SERVER_EMBED_CPU', '0') != '0'
ENABLE_MMPROJ = os.getenv('LLAMA_SERVER_MMPROJ_ENABLE', '1') != '0'

# 设备选择：留空则自动挑独显（枚举结果里排除名称含 Graphics 的核显）。
# 这个机器的 Vulkan 枚举会同时列出 RX 9070 GRE 与 CPU 核显，不显式指定时有跑错设备的可能。
VULKAN_DEVICE = os.getenv('LLAMA_SERVER_DEVICE', '').strip()

READY_TIMEOUT = float(os.getenv('LLAMA_SERVER_READY_TIMEOUT', '300'))   # 8.55GB 冷加载留足余量
HEALTH_TIMEOUT = 3.0
STOP_TIMEOUT = 15.0
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_TAIL_LINES = 25

_DEVICES_CACHE: Optional[List[Dict[str, str]]] = None


def _bool_flag(value: bool) -> str:
    return 'on' if value else 'off'


def _parse_devices(output: str) -> List[Dict[str, str]]:
    '''解析 `llama-server --list-devices` 的输出。

    行样例：`  Vulkan0: AMD Radeon RX 9070 GRE (12224 MiB, 11348 MiB free)`
    核显样例：`  Vulkan1: AMD Radeon(TM) Graphics (7993 MiB, 7594 MiB free)`
    '''
    devices: List[Dict[str, str]] = []
    pattern = re.compile(r'^\s*(\w+):\s*(.+?)\s*\((\d+)\s*MiB,\s*(\d+)\s*MiB free\)')
    for line in output.splitlines():
        m = pattern.match(line)
        if m:
            devices.append({
                'id': m.group(1),
                'name': m.group(2).strip(),
                'vram_mib': m.group(3),
                'free_mib': m.group(4),
            })
    return devices


def list_devices(refresh: bool = False) -> List[Dict[str, str]]:
    '''枚举可用设备（带缓存；失败返回空列表，由调用方回退）。'''
    global _DEVICES_CACHE
    if _DEVICES_CACHE is not None and not refresh:
        return _DEVICES_CACHE
    exe = _server_exe()
    if not exe.exists():
        return []
    try:
        r = subprocess.run(
            [str(exe), '--list-devices'],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0,
        )
    except Exception as e:
        logger.warning('设备枚举失败(%s)，回退 Vulkan0', e)
        return []
    parsed = _parse_devices((r.stdout or '') + (r.stderr or ''))
    if parsed:
        _DEVICES_CACHE = parsed
    return parsed


def resolve_device() -> str:
    '''挑独立显卡：显式环境变量 > 名称不含 Graphics 的设备 > 枚举第一个 > Vulkan0。

    核显在 Vulkan 里同样会被枚举出来（名称带 "Graphics"，UMA 共享内存），
    模型一旦跑到它上面，权重会落到系统内存，速度断崖式下跌。
    '''
    if VULKAN_DEVICE:
        return VULKAN_DEVICE
    devices = list_devices()
    for d in devices:
        if 'graphics' not in d['name'].lower():
            return d['id']
    if devices:
        return devices[0]['id']
    return 'Vulkan0'


def describe_models() -> List[Dict[str, Any]]:
    '''把注册表展开成前端可直接渲染的清单，附带权重文件是否就绪。'''
    out: List[Dict[str, Any]] = []
    for key, spec in get_merged_registry().items():
        model_path = Path(spec['model']) if spec.get('model') else Path('')
        mmproj_path = Path(spec['mmproj']) if spec.get('mmproj') else None
        mtp_path = Path(spec['mtp']) if spec.get('mtp') else None
        available = bool(spec.get('model')) and model_path.is_file()
        size_gb = 0.0
        if available:
            try:
                size_gb = round(model_path.stat().st_size / (1024 ** 3), 2)
            except OSError:
                size_gb = 0.0
        out.append({
            'key': key,
            'label': spec['label'],
            'detail': spec.get('detail') or '',
            'path': str(model_path) if spec.get('model') else '',
            'mmproj': str(mmproj_path) if mmproj_path else '',
            'mtp': str(mtp_path) if mtp_path else '',
            'available': available,
            'has_mmproj': bool(mmproj_path and mmproj_path.exists()),
            'has_mtp': bool(mtp_path and mtp_path.exists()),
            'ctx': int(spec.get('ctx') or N_CTX),
            'ngl': spec.get('ngl') if spec.get('ngl') is not None else N_GPU_LAYERS,
            'device': spec.get('device') or 'auto',
            'kv_type': spec.get('kv_type') or KV_TYPE,
            'flash_attn': bool(spec.get('flash_attn', True)),
            'jinja': bool(spec.get('jinja', True)),
            'builtin': bool(spec.get('builtin', False)),
            'size_gb': size_gb,
        })
    return out


def build_args(model_key: str = DEFAULT_MODEL_KEY, enable_mtp: bool = False) -> List[str]:
    '''构造 llama-server 命令行。'''
    registry = get_merged_registry()
    spec = registry.get(model_key)
    if spec is None:
        raise RuntimeError(f'未知模型 key：{model_key}')
    model_path = Path(spec['model'])
    mmproj_path = Path(spec['mmproj']) if spec.get('mmproj') else None
    mtp_path = Path(spec['mtp']) if spec.get('mtp') else None

    raw_device = str(spec.get('device') or '').strip()
    is_cpu = raw_device.lower() == 'cpu'
    device = '' if is_cpu else (raw_device if raw_device and raw_device.lower() != 'auto' else resolve_device())
    ngl = 0 if is_cpu else (int(spec['ngl']) if spec.get('ngl') is not None else N_GPU_LAYERS)
    kv_type = str(spec.get('kv_type') or KV_TYPE)
    flash_attn = bool(spec.get('flash_attn', True))
    use_jinja = bool(spec.get('jinja', True))

    args: List[str] = [
        str(_server_exe()),
    ]
    if device:
        args += ['--device', device]
    args += [
        '-m', str(model_path),
        '--host', HOST,
        '--port', str(SERVER_PORT),
        '-ngl', str(ngl),
        '-c', str(spec.get('ctx') or N_CTX),
        '-np', str(N_PARALLEL),
        '-fa', _bool_flag(flash_attn),
        '-ub', str(UBATCH),
        '-ctk', kv_type,
        '-ctv', kv_type,
    ]
    if use_jinja:
        args.append('--jinja')
    args += [
        '--no-webui',
        '--alias', model_key,
    ]
    if OFFLOAD_EMBEDDING:
        args += ['-ot', 'token_embd=CPU']
    if ENABLE_MMPROJ and mmproj_path and mmproj_path.exists():
        args += ['--mmproj', str(mmproj_path)]
        if device:
            args += ['--mmproj-device', device]
    if enable_mtp and mtp_path and mtp_path.exists():
        args += ['--model-draft', str(mtp_path)]
    return args


class LlamaServerEngine:
    '''llama-server 子进程的启停、探活与状态查询（进程级单例）。'''

    _instance: Optional['LlamaServerEngine'] = None
    _instance_lock = threading.Lock()

    def __new__(cls) -> 'LlamaServerEngine':
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    obj = super().__new__(cls)
                    obj._init_state()
                    cls._instance = obj
        return cls._instance

    def _init_state(self) -> None:
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self._ready = False
        self._started_at: Optional[float] = None
        self._last_error: str = ''
        # 当前驻留的模型与 MTP 开关。切换模型必须 stop -> start，显存只够放一份权重。
        self._model_key: str = DEFAULT_MODEL_KEY
        self._mtp_enabled: bool = False

    # ------------------------------------------------------------------
    # 探活
    # ------------------------------------------------------------------

    def _base_url(self) -> str:
        return f'http://{HOST}:{SERVER_PORT}'

    def _port_free(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((HOST, SERVER_PORT))
                return True
            except OSError:
                return False

    def _healthy(self, timeout: float = HEALTH_TIMEOUT) -> bool:
        try:
            r = httpx.get(f'{self._base_url()}/health', timeout=timeout, trust_env=False)
            return r.status_code == 200
        except Exception:
            return False

    def _running_alias(self) -> str:
        '''读在跑服务的模型别名，用于判断端口上驻留的是不是另一个权重。'''
        try:
            r = httpx.get(f'{self._base_url()}/v1/models', timeout=2.0, trust_env=False)
            items = (r.json() or {}).get('data') or []
            return items[0].get('id', '') if items else ''
        except Exception:
            return ''

    def _log_tail(self, lines: int = LOG_TAIL_LINES) -> str:
        '''读日志尾部，用于把启动失败的真正原因回传给调用方。'''
        try:
            if not SERVER_LOG.exists():
                return ''
            text = SERVER_LOG.read_text(encoding='utf-8', errors='ignore')
            return '\n'.join(text.splitlines()[-lines:])
        except Exception:
            return ''

    def _rotate_log(self) -> None:
        try:
            if SERVER_LOG.exists() and SERVER_LOG.stat().st_size > LOG_MAX_BYTES:
                bak = SERVER_LOG.with_suffix('.log.1')
                if bak.exists():
                    bak.unlink()
                SERVER_LOG.rename(bak)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 进程控制
    # ------------------------------------------------------------------

    def _kill_locked(self) -> None:
        proc = self._proc
        self._proc = None
        self._ready = False
        self._started_at = None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=STOP_TIMEOUT)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass

    def _port_owner_pid(self) -> Optional[int]:
        '''Listening pid on SERVER_PORT, or None when nothing holds it.'''
        try:
            out = subprocess.run(
                ['netstat', '-ano', '-p', 'TCP'],
                capture_output=True, text=True, timeout=10,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            ).stdout
        except Exception as e:
            logger.warning('netstat 失败：%s', e)
            return None
        for line in out.splitlines():
            if f':{SERVER_PORT}' not in line or 'LISTENING' not in line.upper():
                continue
            pid = line.split()[-1].strip()
            if pid.isdigit():
                return int(pid)
        return None

    def _is_llama_binary(self, pid: int) -> bool:
        '''True only for llama-server.exe, never for whatever else grabbed the port.

        The runtime is shared with another project on this machine and both
        copies bind 8080, so a blind taskkill would end the other application's
        model. Name check first, kill second.
        '''
        try:
            out = subprocess.run(
                ['tasklist', '/FI', f'PID eq {pid}', '/FO', 'CSV', '/NH'],
                capture_output=True, text=True, timeout=10,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            ).stdout
        except Exception as e:
            logger.warning('tasklist 失败：%s', e)
            return False
        return 'llama-server.exe' in out.lower()

    def _kill_port_owner(self) -> None:
        '''孤儿实例兜底清理：只在本进程没有句柄但端口仍被占用时使用。'''
        if os.name != 'nt':
            return
        pid = self._port_owner_pid()
        if pid is None:
            return
        if not self._is_llama_binary(pid):
            logger.warning(
                '端口 %s 被 pid %s 占用，但不是 llama-server，拒绝强行结束',
                SERVER_PORT, pid,
            )
            return
        try:
            subprocess.run(
                ['taskkill', '/F', '/PID', str(pid)],
                capture_output=True, timeout=10,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            logger.info('已结束端口 %s 上的残留 llama-server（pid %s）', SERVER_PORT, pid)
        except Exception as e:
            logger.warning('清理端口 %s 占用进程失败：%s', SERVER_PORT, e)

    def _wait_ready(self, proc: subprocess.Popen, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                self._last_error = (
                    f'llama-server 启动后立即退出（exit={proc.returncode}）：\n{self._log_tail()}'
                )
                return False
            if self._healthy():
                return True
            time.sleep(1.0)
        self._last_error = f'llama-server 在 {int(timeout)}s 内未就绪：\n{self._log_tail()}'
        return False

    def _launch_locked(self, model_key: str, enable_mtp: bool) -> bool:
        '''按指定模型拉起进程并等待就绪；失败时返回 False，原因写进 _last_error。'''
        self._rotate_log()
        args = build_args(model_key, enable_mtp)
        logger.info('启动 llama-server：%s', ' '.join(args))
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
        with open(SERVER_LOG, 'ab') as logf:
            proc = subprocess.Popen(
                args,
                cwd=str(_llama_dir()),
                stdout=logf,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=flags,
            )
        self._proc = proc
        # Same kill-on-exit job the audio engines use. Without it the only
        # cleanup paths were atexit and shutdown_all, and both are skipped when
        # the backend is terminated hard - which is exactly how this server
        # ended up orphaned on port 8080.
        audio_cpp_job.adopt(proc)
        self._started_at = time.time()
        if not self._wait_ready(proc, READY_TIMEOUT):
            self._kill_locked()
            return False
        self._ready = True
        self._last_error = ''
        spec = get_merged_registry().get(model_key) or {}
        logger.info(
            'llama-server 就绪：%s（模型 %s，MTP %s，ctx %s）',
            self._base_url(), model_key, 'on' if enable_mtp else 'off',
            spec.get('ctx') or N_CTX,
        )
        return True

    def _spawn_locked(
        self,
        model_key: Optional[str] = None,
        enable_mtp: Optional[bool] = None,
    ) -> Dict[str, Any]:
        registry = get_merged_registry()
        key = model_key or self._model_key or DEFAULT_MODEL_KEY
        spec = registry.get(key)
        if spec is None:
            raise RuntimeError(f'未知模型 key：{key}')
        want_mtp = self._mtp_enabled if enable_mtp is None else bool(enable_mtp)

        exe = _server_exe()
        if not exe.exists():
            raise RuntimeError(f'llama-server.exe 不存在：{exe}')
        model_path = Path(spec['model'])
        if not model_path.exists():
            raise RuntimeError(f'模型文件不存在：{model_path}')

        # 端口被占用：先确认上面驻留的是不是同一个权重，是才复用
        if not self._port_free():
            if not self._healthy():
                raise RuntimeError(f'端口 {SERVER_PORT} 被非 llama-server 进程占用，请先释放')
            running = self._running_alias()
            same_model = (not running) or running == key
            if same_model and want_mtp == self._mtp_enabled:
                # Reuse is only safe once the instance is bound to this process's
                # job: an unbound server survives a hard kill of the backend and
                # keeps holding port 8080 and its VRAM. Bind it by pid; when that
                # fails, fall through to kill-and-relaunch rather than adopting an
                # orphan this process could never clean up.
                reuse_pid = self._port_owner_pid()
                reusable = os.name != 'nt' or (
                    reuse_pid is not None
                    and self._is_llama_binary(reuse_pid)
                    and audio_cpp_job.adopt_pid(reuse_pid)
                )
                if reusable:
                    self._ready = True
                    self._proc = None
                    self._model_key = key
                    if self._started_at is None:
                        self._started_at = time.time()
                    logger.info(
                        '复用已在运行的 llama-server（%s，模型 %s，pid %s）',
                        self._base_url(), key, reuse_pid,
                    )
                    return self.status()
                logger.warning(
                    '端口 %s 上的 llama-server（pid %s）无法纳入 kill-on-exit job，杀掉后重新启动',
                    SERVER_PORT, reuse_pid,
                )
            logger.info(
                '端口上驻留 %s（MTP %s），目标 %s（MTP %s），先停止再启动',
                running or '未知', 'on' if self._mtp_enabled else 'off',
                key, 'on' if want_mtp else 'off',
            )
            self._kill_locked()
            if not self._port_free():
                self._kill_port_owner()
            deadline = time.time() + 5
            while time.time() < deadline and not self._port_free():
                time.sleep(0.2)
            if not self._port_free():
                raise RuntimeError(f'端口 {SERVER_PORT} 释放失败，无法切换模型')

        started = self._launch_locked(key, want_mtp)
        if not started and want_mtp:
            # draft 头不被当前 llama.cpp 识别时退回纯主模型，避免整个服务起不来
            logger.warning('带 MTP 启动失败，回退到不带 draft 重试：%s', self._last_error)
            self._kill_locked()
            started = self._launch_locked(key, False)
            if started:
                want_mtp = False
        if not started:
            raise RuntimeError(self._last_error or 'llama-server 启动失败')

        self._model_key = key
        self._mtp_enabled = want_mtp
        return self.status()

    def start(
        self,
        model_key: Optional[str] = None,
        enable_mtp: Optional[bool] = None,
    ) -> Dict[str, Any]:
        '''确保目标模型在运行；已在运行且是同一权重、同一 MTP 档位则直接返回状态。阻塞直到就绪。'''
        with self._lock:
            key = model_key or self._model_key or DEFAULT_MODEL_KEY
            want_mtp = self._mtp_enabled if enable_mtp is None else bool(enable_mtp)
            if (
                self._proc is not None
                and self._proc.poll() is None
                and self._healthy()
                and self._model_key == key
                and self._mtp_enabled == want_mtp
            ):
                self._ready = True
                return self.status()
            return self._spawn_locked(key, want_mtp)

    def stop(self) -> Dict[str, Any]:
        '''停止进程并释放显存。'''
        with self._lock:
            was_running = self._proc is not None or not self._port_free()
            self._kill_locked()
            if not self._port_free():
                self._kill_port_owner()
            deadline = time.time() + 5
            while time.time() < deadline and not self._port_free():
                time.sleep(0.2)
            logger.info('llama-server 已停止（was_running=%s）', was_running)
            return {'stopped': True, 'was_running': was_running}

    def restart(
        self,
        model_key: Optional[str] = None,
        enable_mtp: Optional[bool] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            self._kill_locked()
            if not self._port_free():
                self._kill_port_owner()
            return self._spawn_locked(model_key, enable_mtp)

    def release(self) -> Dict[str, Any]:
        '''释放显存（语义同 stop；llama-server 单模型模式没有热卸载端点）。'''
        result = self.stop()
        result['released'] = True
        result['note'] = 'llama-server 单模型模式无热卸载端点，release 等价于停止进程'
        return result

    def close(self) -> None:
        with self._lock:
            self._kill_locked()
            # A server that was adopted (started by an earlier backend) has no
            # handle here: _kill_locked is a no-op and the port is the only
            # thing left to act on.
            if not self._port_free():
                self._kill_port_owner()

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        proc = self._proc
        alive = proc is not None and proc.poll() is None
        healthy = self._healthy(timeout=1.5) if (alive or not self._port_free()) else False
        uptime = int(time.time() - self._started_at) if (self._started_at and healthy) else 0
        device = resolve_device()
        device_name = next((d['name'] for d in list_devices() if d['id'] == device), '')
        key = self._model_key
        registry = get_merged_registry()
        spec = registry.get(key) or next(iter(registry.values()), {})
        model_path = Path(spec['model']) if spec.get('model') else Path('')
        mmproj_path = Path(spec['mmproj']) if spec.get('mmproj') else None
        mtp_path = Path(spec['mtp']) if spec.get('mtp') else None
        return {
            'running': bool(healthy),
            'managed': alive,
            'pid': proc.pid if alive else None,
            'port': SERVER_PORT,
            'base_url': f'{self._base_url()}/v1',
            'model': key,
            'model_label': spec.get('label', key),
            'model_path': str(model_path) if spec.get('model') else '',
            'mmproj': str(mmproj_path) if (ENABLE_MMPROJ and mmproj_path and mmproj_path.exists()) else '',
            'mtp': bool(self._mtp_enabled and mtp_path and mtp_path.exists()),
            'mtp_path': str(mtp_path) if mtp_path else '',
            'ctx': int(spec.get('ctx') or N_CTX),
            'ngl': spec.get('ngl') if spec.get('ngl') is not None else N_GPU_LAYERS,
            'parallel': N_PARALLEL,
            'ubatch': UBATCH,
            'kv_type': spec.get('kv_type') or KV_TYPE,
            'embed_on_cpu': OFFLOAD_EMBEDDING,
            'device': device,
            'device_name': device_name,
            'uptime_s': uptime,
            'last_error': self._last_error,
            'models': describe_models(),
        }


llama_server = LlamaServerEngine()
atexit.register(llama_server.close)
