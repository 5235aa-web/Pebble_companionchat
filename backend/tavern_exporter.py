import json
import base64
import struct
import zlib
from pathlib import Path
from typing import Dict, List, Optional
from io import BytesIO

from . import paths
from .charx import build_charx_archive


def _create_v3_card(companion_data: Dict) -> Dict:
    """
    创建V3格式的角色卡数据
    
    Args:
        companion_data: 数据库中的companion数据
    
    Returns:
        V3格式的角色卡字典
    """
    # 解析JSON字段
    try:
        tags = json.loads(companion_data.get('tags', '[]')) if isinstance(companion_data.get('tags'), str) else companion_data.get('tags', [])
    except:
        tags = []
    
    try:
        alternate_greetings = json.loads(companion_data.get('alternate_greetings', '[]')) if isinstance(companion_data.get('alternate_greetings'), str) else companion_data.get('alternate_greetings', [])
    except:
        alternate_greetings = []
    
    try:
        depth_prompt = json.loads(companion_data.get('depth_prompt', '{}')) if isinstance(companion_data.get('depth_prompt'), str) else companion_data.get('depth_prompt', {})
    except:
        depth_prompt = {}
    
    # 构建V3格式数据
    card_data = {
        "name": companion_data.get('name', 'Unknown'),
        "description": companion_data.get('description', ''),
        "personality": companion_data.get('personality', ''),
        "scenario": companion_data.get('scenario', ''),
        "first_mes": companion_data.get('first_mes', ''),
        "mes_example": companion_data.get('mes_example', ''),
        "creator_notes": companion_data.get('creator_notes', ''),
        "system_prompt": companion_data.get('system_prompt', ''),
        "post_history_instructions": companion_data.get('post_history_instructions', ''),
        "creator": companion_data.get('creator', ''),
        "character_version": companion_data.get('character_version', ''),
        "tags": tags,
        "alternate_greetings": alternate_greetings,
        "extensions": {
            "depth_prompt": depth_prompt,
            "advanced_prompt": companion_data.get('advanced_prompt', ''),
            "user_info": companion_data.get('user_info', ''),
            "pebble_companion_id": companion_data.get('id', ''),
        }
    }
    
    # V3格式包装（同时保留 spec/spec_version 供 CCv3/CHARX 读取器识别，以及 version=3 保持向后兼容）
    return {
        "spec": "chara_card_v3",
        "spec_version": "3.0",
        "data": card_data,
        "version": 3
    }


def export_to_json(companion_data: Dict) -> str:
    """
    导出角色卡为JSON字符串
    
    Args:
        companion_data: 数据库中的companion数据
    
    Returns:
        JSON格式字符串
    """
    try:
        card_data = _create_v3_card(companion_data)
        return json.dumps(card_data, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[CardExporter] export_to_json error: {e}")
        raise


def export_to_json_bytes(companion_data: Dict) -> bytes:
    """
    导出角色卡为JSON字节流
    
    Args:
        companion_data: 数据库中的companion数据
    
    Returns:
        JSON格式字节流
    """
    json_str = export_to_json(companion_data)
    return json_str.encode('utf-8')


def _write_png_text_chunk(keyword: str, text: str) -> bytes:
    """
    创建PNG tEXt chunk
    
    Args:
        keyword: 关键字（如 'chara', 'ccv3'）
        text: 文本内容
    
    Returns:
        tEXt chunk 字节数据
    """
    # tEXt chunk 格式: length(4) + type(4) + data + crc(4)
    # data 格式: keyword\0text
    data = keyword.encode('latin-1') + b'\x00' + text.encode('latin-1')
    
    chunk_type = b'tEXt'
    length = len(data)
    
    # 计算CRC (chunk type + data)
    crc = zlib.crc32(chunk_type + data) & 0xffffffff
    
    # 构建chunk
    chunk = struct.pack('>I', length) + chunk_type + data + struct.pack('>I', crc)
    return chunk


def export_to_png(companion_data: Dict, avatar_bytes: Optional[bytes] = None) -> bytes:
    """
    导出角色卡为PNG格式（嵌入元数据）
    
    Args:
        companion_data: 数据库中的companion数据
        avatar_bytes: 头像图片字节数据（可选）
    
    Returns:
        PNG格式字节流
    """
    try:
        # 如果没有提供头像数据，创建一个默认的PNG
        if avatar_bytes is None:
            # 尝试从avatar_path读取
            avatar_path = companion_data.get('avatar_path')
            if avatar_path:
                try:
                    # 处理相对路径
                    if avatar_path.startswith('uploads/'):
                        base_path = paths.DATA_ROOT
                        full_path = base_path / avatar_path
                    else:
                        full_path = Path(avatar_path)
                    
                    if full_path.exists():
                        with open(full_path, 'rb') as f:
                            avatar_bytes = f.read()
                except Exception as e:
                    print(f"[CardExporter] Failed to read avatar: {e}")
        
        # 如果仍然没有头像数据，创建一个简单的默认PNG
        if avatar_bytes is None:
            # 创建一个1x1像素的透明PNG作为默认
            avatar_bytes = _create_default_png()
        
        # 验证PNG签名，如果不是PNG格式，使用默认PNG
        if not avatar_bytes.startswith(b'\x89PNG\r\n\x1a\n'):
            print(f"[CardExporter] Avatar is not PNG format, using default PNG")
            avatar_bytes = _create_default_png()
        
        # 创建角色卡数据
        card_data = _create_v3_card(companion_data)
        json_str = json.dumps(card_data, ensure_ascii=False, separators=(',', ':'))
        
        # Base64编码（V3格式使用ccv3）
        encoded_data = base64.b64encode(json_str.encode('utf-8')).decode('ascii')
        
        # 解析PNG并插入tEXt chunk
        # PNG结构: signature(8) + chunks...
        # 我们需要在IHDR之后，IDAT之前插入tEXt chunk
        
        output = BytesIO()
        output.write(avatar_bytes[:8])  # PNG签名
        
        pos = 8
        inserted = False
        
        while pos < len(avatar_bytes) - 8:
            try:
                length = struct.unpack('>I', avatar_bytes[pos:pos+4])[0]
                chunk_type = avatar_bytes[pos+4:pos+8]
                
                # 在IHDR之后插入tEXt chunk
                if chunk_type == b'IHDR' and not inserted:
                    output.write(avatar_bytes[pos:pos+12+length])  # 写入IHDR
                    # 插入ccv3 chunk (V3格式)
                    ccv3_chunk = _write_png_text_chunk('ccv3', encoded_data)
                    output.write(ccv3_chunk)
                    inserted = True
                elif chunk_type == b'IDAT':
                    # 如果还没插入，在IDAT之前插入
                    if not inserted:
                        ccv3_chunk = _write_png_text_chunk('ccv3', encoded_data)
                        output.write(ccv3_chunk)
                        inserted = True
                    output.write(avatar_bytes[pos:pos+12+length])  # 写入IDAT
                elif chunk_type == b'IEND':
                    # 确保在IEND之前已插入
                    if not inserted:
                        ccv3_chunk = _write_png_text_chunk('ccv3', encoded_data)
                        output.write(ccv3_chunk)
                        inserted = True
                    output.write(avatar_bytes[pos:pos+12+length])  # 写入IEND
                    break
                elif chunk_type not in [b'tEXt']:  # 跳过旧的tEXt chunk
                    output.write(avatar_bytes[pos:pos+12+length])
                
                pos += 12 + length
            except (struct.error, IndexError):
                break
        
        return output.getvalue()
    except Exception as e:
        print(f"[CardExporter] export_to_png error: {e}")
        import traceback
        traceback.print_exc()
        raise


def _create_default_png() -> bytes:
    """
    创建一个简单的默认PNG（1x1像素，灰色）
    用于没有头像时的导出
    """
    # 这是一个1x1像素的灰色PNG
    # 实际应用中可以使用更复杂的默认图片
    return bytes([
        0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A,  # PNG签名
        0x00, 0x00, 0x00, 0x0D,  # IHDR长度
        0x49, 0x48, 0x44, 0x52,  # IHDR
        0x00, 0x00, 0x00, 0x01,  # 宽度: 1
        0x00, 0x00, 0x00, 0x01,  # 高度: 1
        0x08, 0x02, 0x00, 0x00, 0x00,  # 8位RGB
        0x90, 0x77, 0x53, 0xDE,  # IHDR CRC
        0x00, 0x00, 0x00, 0x0C,  # IDAT长度
        0x49, 0x44, 0x41, 0x54,  # IDAT
        0x08, 0xD7, 0x63, 0xF8, 0xCF, 0xC0, 0x00, 0x00, 0x00, 0x03, 0x01, 0x01, 0x00, 0x05,  # 压缩数据
        0x18, 0xD8, 0xAE, 0xD5,  # IDAT CRC
        0x00, 0x00, 0x00, 0x00,  # IEND长度
        0x49, 0x45, 0x4E, 0x44,  # IEND
        0xAE, 0x42, 0x60, 0x82,  # IEND CRC
    ])


def export_to_charx_file(
    companion_data: Dict,
    assets: Optional[List[Dict]],
    dest_path: Path,
) -> Path:
    """流式打包导出角色卡为 .charx (CCv3 ZIP) 文件。"""
    return build_charx_archive(
        companion_data=companion_data,
        assets=assets or [],
        card_payload_builder=_create_v3_card,
        dest_path=dest_path,
    )


def get_export_filename(companion_data: Dict, format: str = 'json') -> str:
    """
    获取导出文件名
    
    Args:
        companion_data: 数据库中的companion数据
        format: 'json' 或 'png'
    
    Returns:
        文件名（不含路径）
    """
    name = companion_data.get('name', 'Unknown')
    # 清理文件名中的非法字符
    safe_name = "".join(c for c in name if c.isalnum() or c in (' ', '-', '_')).strip()
    if not safe_name:
        safe_name = "character"
    
    return f"{safe_name}.{format}"
