import re
import random
import logging
from typing import Any, List, Dict, Optional, Set
from dataclasses import dataclass, field, fields

logger = logging.getLogger(__name__)

# 选择性逻辑枚举
SELECTIVE_LOGIC_AND_ANY = 0   # 次要关键词任一匹配
SELECTIVE_LOGIC_NOT_ALL = 1   # 非所有次要关键词匹配
SELECTIVE_LOGIC_NOT_ANY = 2   # 无次要关键词匹配
SELECTIVE_LOGIC_AND_ALL = 3   # 所有次要关键词匹配


@dataclass
class WorldBookEntry:
    """世界书条目"""
    id: int
    uid: int
    comment: str
    keys: List[str] = field(default_factory=list)
    secondary_keys: List[str] = field(default_factory=list)
    content: str = ""
    constant: bool = False
    selective: bool = True
    selective_logic: int = SELECTIVE_LOGIC_AND_ANY
    order: int = 100
    position: int = 0  # 0=before, 1=after, 4=at_depth
    depth: int = 4
    disabled: bool = False
    probability: int = 100
    use_probability: bool = True
    sticky: Optional[int] = None
    cooldown: Optional[int] = None
    delay: Optional[int] = None
    group: str = ""
    group_weight: int = 100
    case_sensitive: Optional[bool] = None
    match_whole_words: Optional[bool] = None
    scan_depth: Optional[int] = None
    triggers: List[str] = field(default_factory=list)
    ignore_budget: bool = False
    category: str = "lore"
    
    def to_dict(self) -> Dict:
        return {
            'id': self.id,
            'uid': self.uid,
            'comment': self.comment,
            'keys': self.keys,
            'secondary_keys': self.secondary_keys,
            'content': self.content,
            'constant': self.constant,
            'selective': self.selective,
            'selective_logic': self.selective_logic,
            'order': self.order,
            'position': self.position,
            'depth': self.depth,
            'disabled': self.disabled,
            'probability': self.probability,
            'use_probability': self.use_probability,
            'sticky': self.sticky,
            'cooldown': self.cooldown,
            'delay': self.delay,
            'group': self.group,
            'group_weight': self.group_weight,
            'case_sensitive': self.case_sensitive,
            'match_whole_words': self.match_whole_words,
            'scan_depth': self.scan_depth,
            'triggers': self.triggers,
            'ignore_budget': self.ignore_budget,
            'category': self.category,
        }


@dataclass
class ActivationResult:
    """激活结果"""
    entry: WorldBookEntry
    content: str
    position: int
    order: int
    group: str


@dataclass
class WorldBookPlan:
    """Structured prompt injection plan separating system context vs message-depth injections."""

    system_before: Optional[str] = None
    system_after: Optional[str] = None
    depth_entries: List[Dict[str, Any]] = field(default_factory=list)
    has_media: bool = False
    has_shallow_media: bool = False

    def as_text(self) -> Optional[str]:
        """Render all sections as one flat block for legacy callers/diagnostics."""
        parts: List[str] = []
        if self.system_before:
            parts.append(self.system_before)
        if self.system_after:
            parts.append(self.system_after)
        if self.depth_entries:
            depth_text = "\n\n".join(item["content"] for item in self.depth_entries if item.get("content"))
            if depth_text:
                parts.append(f"[World Info: Depth prompt]\n{depth_text}")
        return "\n\n".join(parts) if parts else None


_WORLD_BOOK_ENTRY_FIELDS = None


class WorldBookEngine:
    """世界书引擎 - 处理条目匹配和激活"""
    
    def __init__(self, entries: List[Dict], budget: int = 1000):
        """
        初始化引擎
        
        Args:
            entries: 世界书条目列表（从数据库获取的原始数据）
            budget: 激活条目的token预算（简化版，实际应该计算token）
        """
        global _WORLD_BOOK_ENTRY_FIELDS
        if _WORLD_BOOK_ENTRY_FIELDS is None:
            _WORLD_BOOK_ENTRY_FIELDS = frozenset(f.name for f in fields(WorldBookEntry))

        self.entries: List[WorldBookEntry] = []
        for entry_data in entries:
            filtered = {k: v for k, v in entry_data.items() if k in _WORLD_BOOK_ENTRY_FIELDS}
            self.entries.append(WorldBookEntry(**filtered))
        
        self.budget = budget
        self.activated_entries: Set[int] = set()
        self.group_scores: Dict[str, float] = {}
        
    def match_keys(self, text: str, keys: List[str], case_sensitive: bool = False, match_whole_words: bool = False) -> bool:
        """
        匹配关键词
        
        Args:
            text: 要搜索的文本
            keys: 关键词列表
            case_sensitive: 是否区分大小写
            match_whole_words: 是否全词匹配
            
        Returns:
            是否至少有一个关键词匹配
        """
        if not keys or not text:
            return False
        
        # Two views of the text: the lowered one for literal keys, the original
        # one for regex keys. Lowering a pattern rewrites character classes such
        # as [A-Z], so regex matching must see the text as written.
        lowered = text if case_sensitive else text.lower()

        for key in keys:
            if not key:
                continue

            # Regex keys keep their original spelling; case folding is left to
            # the IGNORECASE flag instead of being baked into the pattern.
            if len(key) >= 2 and key.startswith('/') and key.endswith('/'):
                try:
                    pattern = key[1:-1]
                    flags = 0 if case_sensitive else re.IGNORECASE
                    if re.search(pattern, text, flags):
                        return True
                except re.error:
                    logger.warning(f"Invalid regex pattern: {pattern}")
                    continue

            key_str = key if case_sensitive else key.lower()

            if match_whole_words:
                # 全词匹配 - 使用单词边界
                pattern = r'(?:^|\W)' + re.escape(key_str) + r'(?:$|\W)'
                if re.search(pattern, lowered, 0 if case_sensitive else re.IGNORECASE):
                    return True
            else:
                # 普通包含匹配
                if key_str in lowered:
                    return True
        
        return False
    
    def check_selective_logic(self, text: str, secondary_keys: List[str], logic: int, 
                             case_sensitive: bool = False, match_whole_words: bool = False) -> bool:
        """
        检查选择性逻辑
        
        Args:
            text: 要搜索的文本
            secondary_keys: 次要关键词列表
            logic: 逻辑类型 (0=AND_ANY, 1=NOT_ALL, 2=NOT_ANY, 3=AND_ALL)
            
        Returns:
            是否符合逻辑条件
        """
        if not secondary_keys:
            return True

        # Who decides whether selective logic applies is the caller: evaluate_entry
        # already gates on `entry.selective and entry.secondary_keys`. This method
        # is on the engine and has no `selective` of its own.

        # 统计匹配的次要关键词数量
        matched_count = 0
        for key in secondary_keys:
            if self.match_keys(text, [key], case_sensitive, match_whole_words):
                matched_count += 1
        
        if logic == SELECTIVE_LOGIC_AND_ANY:
            # 任一匹配即可
            return matched_count > 0
        elif logic == SELECTIVE_LOGIC_NOT_ALL:
            # 不是所有都匹配（即至少一个不匹配）
            return matched_count < len(secondary_keys)
        elif logic == SELECTIVE_LOGIC_NOT_ANY:
            # 没有一个匹配
            return matched_count == 0
        elif logic == SELECTIVE_LOGIC_AND_ALL:
            # 所有都匹配
            return matched_count == len(secondary_keys)
        
        return False
    
    def check_probability(self, probability: int) -> bool:
        """检查概率激活"""
        if probability >= 100:
            return True
        return random.randint(1, 100) <= probability
    
    def get_scan_text(self, messages: List[Dict], depth: int, 
                     include_persona: bool = False, include_character: bool = False) -> str:
        """
        获取要扫描的文本
        
        Args:
            messages: 消息列表
            depth: 扫描深度（最近多少条消息）
            include_persona: 是否包含用户角色描述
            include_character: 是否包含角色描述
        """
        texts = []
        
        # 取最近depth条消息
        recent_messages = messages[-depth:] if len(messages) > depth else messages
        
        for msg in recent_messages:
            role = msg.get('role', '')
            content = msg.get('content', '')
            
            if role == 'user':
                texts.append(f"User: {content}")
            elif role == 'assistant':
                texts.append(f"AI: {content}")
        
        return '\n'.join(texts)
    
    def evaluate_entry(self, entry: WorldBookEntry, scan_text: str) -> bool:
        """
        评估单个条目是否应该激活
        
        Args:
            entry: 世界书条目
            scan_text: 扫描文本
            
        Returns:
            是否应该激活
        """
        # 1. 检查是否禁用
        if entry.disabled:
            return False
        
        # 2. 常驻条目直接激活
        if entry.constant:
            return True
        
        # 3. 主关键词匹配
        if not entry.keys:
            return False
        
        case_sensitive = entry.case_sensitive if entry.case_sensitive is not None else False
        match_whole_words = entry.match_whole_words if entry.match_whole_words is not None else False
        
        if not self.match_keys(scan_text, entry.keys, case_sensitive, match_whole_words):
            return False
        
        # 4. 选择性逻辑检查
        if entry.selective and entry.secondary_keys:
            if not self.check_selective_logic(
                scan_text, 
                entry.secondary_keys, 
                entry.selective_logic,
                case_sensitive,
                match_whole_words
            ):
                return False
        
        # 5. 概率检查
        if entry.use_probability:
            if not self.check_probability(entry.probability):
                return False
        
        return True
    
    def process_group_scoring(self, activated: List[ActivationResult]) -> List[ActivationResult]:
        """
        处理分组评分 - 同组只保留权重最高的
        
        Args:
            activated: 已激活的条目列表
            
        Returns:
            筛选后的条目列表
        """
        # 按分组组织
        groups: Dict[str, List[ActivationResult]] = {}
        ungrouped: List[ActivationResult] = []
        
        for result in activated:
            if result.group:
                if result.group not in groups:
                    groups[result.group] = []
                groups[result.group].append(result)
            else:
                ungrouped.append(result)
        
        # 对每个组只保留权重最高的
        final_results = ungrouped[:]
        
        for group_name, group_entries in groups.items():
            # 按权重降序排序，取第一个
            group_entries.sort(key=lambda x: x.entry.group_weight, reverse=True)
            final_results.append(group_entries[0])
        
        return final_results
    
    def evaluate(self, messages: List[Dict], scan_depth: int = 10) -> List[ActivationResult]:
        """
        评估所有条目，返回应该激活的条目
        
        Args:
            messages: 完整的消息历史
            scan_depth: 扫描深度（默认最近10条消息）
            
        Returns:
            激活结果列表
        """
        if not self.entries:
            return []
        
        scan_cache: Dict[int, str] = {scan_depth: self.get_scan_text(messages, scan_depth)}
        activated: List[ActivationResult] = []

        for entry in self.entries:
            depth = entry.scan_depth if entry.scan_depth is not None else scan_depth
            if depth not in scan_cache:
                scan_cache[depth] = self.get_scan_text(messages, depth)
            scan_text = scan_cache[depth]

            if self.evaluate_entry(entry, scan_text):
                activated.append(ActivationResult(
                    entry=entry,
                    content=entry.content,
                    position=entry.position,
                    order=entry.order,
                    group=entry.group
                ))

        # 处理分组
        activated = self.process_group_scoring(activated)

        # 按order降序排序（数值大的先插入）
        activated.sort(key=lambda x: x.order, reverse=True)

        return activated

    @staticmethod
    def _sort_slot_media_last(items: List[ActivationResult]) -> List[ActivationResult]:
        """Place ``category == 'media'`` formatting protocols later in the block so lore doesn't bury them."""
        non_media = [r for r in items if getattr(r.entry, "category", "lore") != "media"]
        media = [r for r in items if getattr(r.entry, "category", "lore") == "media"]
        return non_media + media

    def build_plan(self, activated: List[ActivationResult]) -> WorldBookPlan:
        """Split activated entries into system before/after slots and message-depth injections."""
        if not activated:
            return WorldBookPlan()

        before_entries = self._sort_slot_media_last([r for r in activated if r.position == 0])
        after_entries = self._sort_slot_media_last([r for r in activated if r.position == 1])
        raw_depth = self._sort_slot_media_last([r for r in activated if r.position == 4])

        sys_before = (
            f"[World Info: Context before character]\n" + "\n\n".join(e.content for e in before_entries)
            if before_entries
            else None
        )
        sys_after = (
            f"[World Info: Context after character]\n" + "\n\n".join(e.content for e in after_entries)
            if after_entries
            else None
        )

        depth_items: List[Dict[str, Any]] = []
        has_shallow_media = False
        for r in raw_depth:
            d = max(0, int(getattr(r.entry, "depth", 4) if r.entry.depth is not None else 4))
            cat = getattr(r.entry, "category", "lore")
            if cat == "media" and d <= 2:
                has_shallow_media = True
            depth_items.append(
                {
                    "depth": d,
                    "content": r.content,
                    "order": r.order,
                    "category": cat,
                }
            )

        has_media = any(getattr(r.entry, "category", "lore") == "media" for r in activated)
        return WorldBookPlan(
            system_before=sys_before,
            system_after=sys_after,
            depth_entries=depth_items,
            has_media=has_media,
            has_shallow_media=has_shallow_media,
        )

    def build_prompt_injection(self, activated: List[ActivationResult]) -> Optional[str]:
        """构建提示词注入内容（扁平文本形态，兼容旧调用方）。"""
        return self.build_plan(activated).as_text()


def get_active_world_book_plan(
    companion_id: str,
    messages: List[Dict],
    world_book_id: Optional[int] = None,
) -> WorldBookPlan:
    """Evaluate active world-book entries and return a structured ``WorldBookPlan``."""
    from .world_book_db import get_companion_primary_world_book, get_entries

    if world_book_id:
        entries = get_entries(world_book_id)
    else:
        world_book = get_companion_primary_world_book(companion_id)
        if not world_book:
            return WorldBookPlan()
        entries = get_entries(world_book["id"])

    if not entries:
        return WorldBookPlan()

    engine = WorldBookEngine(entries)
    activated = engine.evaluate(messages)
    return engine.build_plan(activated)


def get_active_world_book_content(companion_id: str, messages: List[Dict], 
                                  world_book_id: Optional[int] = None) -> Optional[str]:
    """获取AI伴侣当前激活的世界书内容（扁平文本形式）。"""
    return get_active_world_book_plan(
        companion_id,
        messages,
        world_book_id=world_book_id,
    ).as_text()


# 简单的测试函数
def test_engine():
    """测试引擎功能"""
    entries = [
        {
            'id': 1,
            'uid': 1,
            'comment': 'Test entry',
            'keys': ['magic', '魔法'],
            'secondary_keys': [],
            'content': 'This is a world where magic exists.',
            'constant': False,
            'selective': False,
            'selective_logic': 0,
            'order': 100,
            'position': 0,
            'depth': 4,
            'disabled': False,
            'probability': 100,
            'use_probability': True,
            'group': '',
            'group_weight': 100,
        },
        {
            'id': 2,
            'uid': 2,
            'comment': 'Dragon entry',
            'keys': ['dragon', '龙'],
            'secondary_keys': ['fire', '火焰'],
            'content': 'Dragons are ancient creatures that breathe fire.',
            'constant': False,
            'selective': True,
            'selective_logic': SELECTIVE_LOGIC_AND_ANY,
            'order': 200,
            'position': 1,
            'depth': 4,
            'disabled': False,
            'probability': 100,
            'use_probability': True,
            'group': '',
            'group_weight': 100,
        }
    ]
    
    engine = WorldBookEngine(entries)
    
    messages = [
        {'role': 'user', 'content': 'Tell me about the magic in this world'},
        {'role': 'assistant', 'content': 'Magic is everywhere here.'},
        {'role': 'user', 'content': 'What about dragons that breathe fire?'},
    ]
    
    activated = engine.evaluate(messages)
    
    print(f"Activated {len(activated)} entries:")
    for result in activated:
        print(f"  - {result.entry.comment}: {result.content[:50]}...")
    
    injection = engine.build_prompt_injection(activated)
    if injection:
        print(f"\nPrompt injection:\n{injection}")
    
    return activated


if __name__ == "__main__":
    test_engine()
