import json
import base64
import logging
import shutil
import struct
import tempfile
import uuid
from pathlib import Path
from typing import Dict, Optional, Sequence

from . import paths
from .charx import extract_charx_package, is_zip_container
from .companion_db import add_companion_assets, create_companion
from .world_book_db import import_character_book_for_companion

logger = logging.getLogger(__name__)


def _import_embedded_character_book(
    companion_id: str,
    raw: Dict,
    *,
    char_name: str = "",
    asset_names: Sequence[str] = (),
) -> None:
    """Import ``character_book`` into world books with media-only default activation."""
    try:
        import_character_book_for_companion(
            companion_id,
            raw,
            char_name=char_name,
            asset_names=asset_names,
            skip_if_linked=True,
        )
    except Exception as exc:
        logger.warning("Failed to import character_book for %s: %s", companion_id, exc)


def _read_png_chara_data(png_path: str) -> str:
    """
    读取 PNG 文件中的角色卡元数据。
    对应 character-card-parser 规范的 read() 逻辑。
    
    PNG元数据存储格式：
    - V3: 'ccv3' chunk (base64编码的JSON)
    - V2: 'chara' chunk (base64编码的JSON)
    V3优先于V2。
    """
    with open(png_path, 'rb') as f:
        data = f.read()
    
    # 验证PNG签名
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError("Not a valid PNG file")
    
    # 解析所有 tEXt chunk
    text_chunks = []
    pos = 8  # 跳过PNG签名
    while pos < len(data) - 8:
        try:
            length = struct.unpack('>I', data[pos:pos+4])[0]
            chunk_type = data[pos+4:pos+8].decode('ascii')
            chunk_data = data[pos+8:pos+8+length]
            
            if chunk_type == 'tEXt':
                # tEXt格式: keyword\0text
                null_idx = chunk_data.index(b'\x00')
                keyword = chunk_data[:null_idx].decode('latin-1')
                text = chunk_data[null_idx+1:].decode('latin-1')
                text_chunks.append((keyword, text))
            
            if chunk_type == 'IEND':
                break
            pos += 12 + length  # length(4) + type(4) + data(length) + crc(4)
        except (struct.error, IndexError, ValueError):
            break
    
    if not text_chunks:
        raise ValueError("No text chunks found in PNG")
    
    # V3优先 (ccv3)
    for keyword, text in text_chunks:
        if keyword.lower() == 'ccv3':
            return base64.b64decode(text).decode('utf-8')
    
    # V2回退 (chara)
    for keyword, text in text_chunks:
        if keyword.lower() == 'chara':
            return base64.b64decode(text).decode('utf-8')
    
    raise ValueError("No character data found in PNG (no 'chara' or 'ccv3' metadata)")


# ==================== Horde Studio virtual human 支持 ====================

HORDE_FORMAT_TAG = 'horde-studio-virtual-human'


def _normalize_horde_human(raw: Dict) -> Dict:
    """
    将 Horde Studio virtual human 导出（.horde_human.json）标准化为Companion配置。
    数据位于 raw['companion'] 下，字段命名与角色卡格式完全不同。
    """
    c = raw.get('companion') or {}
    if not isinstance(c, dict):
        c = {}

    # 描述：外貌 + 背景故事 + 社交世界
    desc_parts = [c.get('appearance', ''), c.get('backstory', ''), c.get('socialWorld', '')]
    description = '\n\n'.join(p for p in desc_parts if p)

    # 场景：地点 + 职业 + 初始关系背景
    scene_parts = []
    if c.get('locationLabel'):
        scene_parts.append(f"[Location]\n{c['locationLabel']}")
    if c.get('occupation'):
        scene_parts.append(f"[Occupation]\n{c['occupation']}")
    if c.get('relationshipContext'):
        scene_parts.append(f"[Relationship context]\n{c['relationshipContext']}")
    scenario = '\n\n'.join(scene_parts)

    # 系统提示词：对话风格规则
    style_parts = []
    for key, label in (
        ('textingStyle', 'Texting style'),
        ('conversationStyle', 'Conversation style'),
        ('chatAvoid', 'Avoid'),
    ):
        if c.get(key):
            style_parts.append(f"[{label}]\n{c[key]}")
    system_prompt = '\n\n'.join(style_parts)

    # 高级提示词：行为与心理设定
    extra_parts = []
    for key, label in (
        ('behaviorExamples', 'Behavior examples'),
        ('values', 'Values'),
        ('contradictions', 'Contradictions'),
        ('vulnerabilities', 'Vulnerabilities'),
        ('relationshipStyle', 'Relationship style'),
        ('habits', 'Habits'),
        ('routine', 'Routine'),
        ('privateLife', 'Private life'),
    ):
        if c.get(key):
            extra_parts.append(f"[{label}]\n{c[key]}")
    advanced_prompt = '\n\n'.join(extra_parts)

    return {
        'name': c.get('name') or 'Unknown',
        'description': description,
        'personality': c.get('personality', ''),
        'scenario': scenario,
        'first_mes': c.get('openingMessage', ''),
        'mes_example': c.get('chatExamples', ''),
        'creator_notes': 'Imported from Horde Studio virtual human template.',
        'system_prompt': system_prompt,
        'post_history_instructions': '',
        'creator': 'Horde Studio',
        'character_version': f"horde-v{raw.get('_version', '')}",
        'tags': [],
        'alternate_greetings': [],
        'depth_prompt': {},
        'advanced_prompt': advanced_prompt,
        'user_info': c.get('connectionRole', ''),
    }


def _strip_oversized_strings(value, max_len: int = 100_000, depth: int = 0):
    """
    递归裁剪超长字符串（媒体 base64 等），返回可安全入库的精简结构。
    仅用于 raw_card_data 存档，不影响解析后的字段。
    """
    if depth > 100:
        return '[stripped: depth limit]'
    if isinstance(value, str):
        if len(value) > max_len:
            return f'[stripped: {len(value)} chars]'
        return value
    if isinstance(value, list):
        return [_strip_oversized_strings(v, max_len, depth + 1) for v in value]
    if isinstance(value, dict):
        return {k: _strip_oversized_strings(v, max_len, depth + 1) for k, v in value.items()}
    return value


def _safe_raw_card_dump(raw: Dict) -> str:
    """
    将原始卡数据序列化为可存储的字符串。
    裁剪超长媒体字符串，避免数百 MB 的导出文件膨胀数据库。
    """
    try:
        return json.dumps(_strip_oversized_strings(raw), ensure_ascii=False)
    except Exception:
        return json.dumps({'_format': 'unknown', '_note': 'raw card data omitted (serialization failed)'}, ensure_ascii=False)


def _extract_horde_avatar(raw: Dict, companion_id: str) -> Optional[str]:
    """
    从 horde 数据中提取头像（profilePhoto/basePhoto data URL）并保存为文件。
    返回相对路径（uploads/companion_avatars/xxx.jpg）；失败时返回 None。
    """
    c = raw.get('companion') or {}
    data_url = c.get('profilePhoto') or c.get('basePhoto') or ''
    if not isinstance(data_url, str) or not data_url.startswith('data:image/'):
        return None
    try:
        header, b64 = data_url.split(',', 1)
        if ';base64' not in header:
            return None
        mime = header[len('data:'):header.index(';')]
    except ValueError:
        return None
    ext = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp'}.get(mime)
    if not ext:
        return None
    try:
        blob = base64.b64decode(b64, validate=False)
    except Exception:
        return None
    if not blob or len(blob) > 20 * 1024 * 1024:
        return None
    avatars_dir = paths.AVATAR_DIR
    avatars_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{companion_id}.{ext}"
    (avatars_dir / filename).write_bytes(blob)
    return f"uploads/companion_avatars/{filename}"


def _normalize_card_data(raw: Dict) -> Dict:
    """
    将角色卡数据标准化为Companion配置。
    兼容 V1/V2/V3 角色卡格式与 Horde Studio virtual human 格式。
    基于 characters.js 的 readFromV2() 和 convertToV2() 逻辑。
    """
    # Horde Studio virtual human 格式（.horde_human.json）
    if raw.get('_format') == HORDE_FORMAT_TAG:
        return _normalize_horde_human(raw)

    # V2/V3: 数据在 data 字段下
    if 'data' in raw:
        d = raw['data']
        ext = d.get('extensions', {})
        return {
            'name': d.get('name', raw.get('name', 'Unknown')),
            'description': d.get('description', ''),
            'personality': d.get('personality', ''),
            'scenario': d.get('scenario', ''),
            'first_mes': d.get('first_mes', ''),
            'mes_example': d.get('mes_example', ''),
            'creator_notes': d.get('creator_notes', ''),
            'system_prompt': d.get('system_prompt', ''),
            'post_history_instructions': d.get('post_history_instructions', ''),
            'creator': d.get('creator', ''),
            'character_version': d.get('character_version', ''),
            'tags': d.get('tags', []),
            'alternate_greetings': d.get('alternate_greetings', []),
            'depth_prompt': ext.get('depth_prompt', {}),
            'advanced_prompt': ext.get('advanced_prompt', ''),
            'user_info': ext.get('user_info', ''),
        }
    
    # V1: 数据在顶层（旧格式）
    return {
        'name': raw.get('name', raw.get('char_name', 'Unknown')),
        'description': raw.get('description', raw.get('char_persona', '')),
        'personality': raw.get('personality', ''),
        'scenario': raw.get('scenario', raw.get('world_scenario', '')),
        'first_mes': raw.get('first_mes', raw.get('char_greeting', '')),
        'mes_example': raw.get('mes_example', raw.get('example_dialogue', '')),
        'creator_notes': raw.get('creatorcomment', raw.get('creator_notes', '')),
        'system_prompt': '',
        'post_history_instructions': '',
        'creator': raw.get('creator', ''),
        'character_version': '',
        'tags': raw.get('tags', []),
        'alternate_greetings': [],
        'depth_prompt': {},
    }


def import_json(json_path: str) -> str:
    """
    导入JSON角色卡文件。
    返回: companion_id
    """
    with open(json_path, 'r', encoding='utf-8') as f:
        raw = json.load(f)
    
    normalized = _normalize_card_data(raw)
    companion_id = f"companion_{uuid.uuid4().hex[:12]}"
    
    # 尝试查找同名PNG作为头像
    avatar_path = None
    json_file = Path(json_path)
    png_candidate = json_file.with_suffix('.png')
    if png_candidate.exists():
        avatar_path = str(png_candidate)
    # Horde 格式：从数据内嵌的 profilePhoto 提取头像
    if avatar_path is None and raw.get('_format') == HORDE_FORMAT_TAG:
        avatar_path = _extract_horde_avatar(raw, companion_id)

    create_companion(
        companion_id=companion_id,
        fields=normalized,
        avatar_path=avatar_path,
        raw_card_data=_safe_raw_card_dump(raw),
        card_format='json',
    )
    _import_embedded_character_book(companion_id, raw, char_name=normalized.get('name', ''))
    return companion_id


def import_png(png_path: str) -> str:
    """
    导入PNG角色卡（从元数据提取JSON）。
    返回: companion_id
    """
    json_str = _read_png_chara_data(png_path)
    raw = json.loads(json_str)
    normalized = _normalize_card_data(raw)
    companion_id = f"companion_{uuid.uuid4().hex[:12]}"
    
    create_companion(
        companion_id=companion_id,
        fields=normalized,
        avatar_path=png_path,
        raw_card_data=json_str,
        card_format='png',
    )
    _import_embedded_character_book(companion_id, raw, char_name=normalized.get('name', ''))
    return companion_id


def import_charx(charx_path: str) -> str:
    """
    导入 CHARX（CCv3 ZIP 容器）角色卡，提取文本、多媒体资产与内嵌世界书（仅默认启用媒体资产指令条目）。
    返回: companion_id
    """
    companion_id = f"companion_{uuid.uuid4().hex[:12]}"
    raw_card, assets, avatar_rel = extract_charx_package(Path(charx_path), companion_id)
    normalized = _normalize_card_data(raw_card)

    created = create_companion(
        companion_id=companion_id,
        fields=normalized,
        avatar_path=avatar_rel,
        raw_card_data=_safe_raw_card_dump(raw_card),
        card_format='charx',
    )
    if not created:
        shutil.rmtree(paths.COMPANION_ASSETS_DIR / companion_id, ignore_errors=True)
        raise RuntimeError(f"Failed to persist companion {companion_id}")

    if assets:
        add_companion_assets(companion_id, assets)
    asset_names = [str(a.get('name') or '').strip() for a in (assets or []) if str(a.get('name') or '').strip()]
    _import_embedded_character_book(
        companion_id,
        raw_card,
        char_name=normalized.get('name', ''),
        asset_names=asset_names,
    )
    return companion_id


def import_card_file(file_path: str, file_type: Optional[str] = None) -> str:
    """
    从磁盘文件流式导入角色卡（支持 .charx / .zip / .png / .json）。
    适用于大文件上传落盘后的解析，不将整个包体读入内存。
    """
    path = Path(file_path)
    kind = (file_type or "").strip().lower().lstrip(".")
    if not kind:
        kind = path.suffix.lower().lstrip(".")

    if kind in ("charx", "zip") or is_zip_container(path):
        return import_charx(str(path))

    if kind == "json":
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        normalized = _normalize_card_data(raw)
        companion_id = f"companion_{uuid.uuid4().hex[:12]}"
        avatar_path = None
        if raw.get("_format") == HORDE_FORMAT_TAG:
            avatar_path = _extract_horde_avatar(raw, companion_id)
        create_companion(
            companion_id=companion_id,
            fields=normalized,
            avatar_path=avatar_path,
            raw_card_data=_safe_raw_card_dump(raw),
            card_format="json",
        )
        _import_embedded_character_book(companion_id, raw, char_name=normalized.get("name", ""))
        return companion_id

    if kind == "png":
        json_str = _read_png_chara_data(str(path))
        raw = json.loads(json_str)
        normalized = _normalize_card_data(raw)
        companion_id = f"companion_{uuid.uuid4().hex[:12]}"
        avatars_dir = paths.AVATAR_DIR
        avatars_dir.mkdir(parents=True, exist_ok=True)
        avatar_filename = f"{companion_id}.png"
        shutil.copyfile(path, avatars_dir / avatar_filename)
        avatar_rel_path = f"uploads/companion_avatars/{avatar_filename}"
        create_companion(
            companion_id=companion_id,
            fields=normalized,
            avatar_path=avatar_rel_path,
            raw_card_data=_safe_raw_card_dump(raw),
            card_format="png",
        )
        _import_embedded_character_book(companion_id, raw, char_name=normalized.get("name", ""))
        return companion_id

    raise ValueError(f"Unsupported file_type: {kind or 'unknown'}. Use 'charx', 'png', or 'json'.")


def import_from_bytes(file_bytes: bytes, file_type: str) -> str:
    """
    从字节流导入（用于API上传场景）。
    file_type: 'json'、'png' 或 'charx'
    返回: companion_id
    """
    if file_type in ('charx', 'zip'):
        with tempfile.NamedTemporaryFile(suffix='.charx', delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = Path(tmp.name)
        try:
            return import_charx(str(tmp_path))
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass
    elif file_type == 'json':
        raw = json.loads(file_bytes.decode('utf-8'))
        normalized = _normalize_card_data(raw)
        companion_id = f"companion_{uuid.uuid4().hex[:12]}"
        avatar_path = None
        if raw.get('_format') == HORDE_FORMAT_TAG:
            avatar_path = _extract_horde_avatar(raw, companion_id)
        create_companion(
            companion_id=companion_id,
            fields=normalized,
            avatar_path=avatar_path,
            raw_card_data=_safe_raw_card_dump(raw),
            card_format='json',
        )
        _import_embedded_character_book(companion_id, raw, char_name=normalized.get('name', ''))
        return companion_id
    elif file_type == 'png':
        # 验证PNG签名
        if file_bytes[:8] != b'\x89PNG\r\n\x1a\n':
            raise ValueError("Not a valid PNG file")
        json_str = _read_png_bytes_chara_data(file_bytes)
        raw = json.loads(json_str)
        normalized = _normalize_card_data(raw)
        companion_id = f"companion_{uuid.uuid4().hex[:12]}"
        # 保存PNG到companions专属头像目录
        avatars_dir = paths.AVATAR_DIR
        avatars_dir.mkdir(parents=True, exist_ok=True)
        avatar_filename = f"{companion_id}.png"
        avatar_path_obj = avatars_dir / avatar_filename
        with open(avatar_path_obj, 'wb') as f:
            f.write(file_bytes)
        # 存储相对路径，供前端通过 /api/archive/file/ 访问
        avatar_rel_path = f"uploads/companion_avatars/{avatar_filename}"
        create_companion(
            companion_id=companion_id,
            fields=normalized,
            avatar_path=avatar_rel_path,
            raw_card_data=json_str,
            card_format='png',
        )
        _import_embedded_character_book(companion_id, raw, char_name=normalized.get('name', ''))
        return companion_id
    else:
        raise ValueError(f"Unsupported file_type: {file_type}. Use 'json', 'png', or 'charx'.")


def _read_png_bytes_chara_data(png_bytes: bytes) -> str:
    """从PNG字节流读取角色卡数据（不读文件）。"""
    if png_bytes[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError("Not a valid PNG file")
    
    text_chunks = []
    pos = 8
    while pos < len(png_bytes) - 8:
        try:
            length = struct.unpack('>I', png_bytes[pos:pos+4])[0]
            chunk_type = png_bytes[pos+4:pos+8].decode('ascii')
            chunk_data = png_bytes[pos+8:pos+8+length]
            
            if chunk_type == 'tEXt':
                null_idx = chunk_data.index(b'\x00')
                keyword = chunk_data[:null_idx].decode('latin-1')
                text = chunk_data[null_idx+1:].decode('latin-1')
                text_chunks.append((keyword, text))
            
            if chunk_type == 'IEND':
                break
            pos += 12 + length
        except (struct.error, IndexError, ValueError):
            break
    
    for keyword, text in text_chunks:
        if keyword.lower() == 'ccv3':
            return base64.b64decode(text).decode('utf-8')
    for keyword, text in text_chunks:
        if keyword.lower() == 'chara':
            return base64.b64decode(text).decode('utf-8')
    
    raise ValueError("No character data found in PNG")
