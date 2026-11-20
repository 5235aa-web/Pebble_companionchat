"""Zero-dependency hybrid memory retriever shared by single and group chats.

Pure-Python BM25 + CJK n-gram + phrase-hit scorer sized for companion and room
memory tables (<= 100 entries).

Design goals:
* No torch, transformers, numpy, or external ML runtimes.
* Fix English substring misfires ("lin" matching "listen") via word boundaries
  and lightweight suffix stemming ("promised" <-> "promise").
* Handle Chinese / CJK without external segmenters via 2-gram and 3-gram
  character windows plus exact keyword phrase matching.
* Honour both group-room contracts (`mark == 2` standing instructions, `about`
  subject focus, optional external vector fusion) and single-companion
  contracts (`importance` scaling, `char_budget`, `use_count` rotation).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from difflib import SequenceMatcher
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

_ASCII_WORD_RE = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?")
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_HAS_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")

# Minimal function-word list only. Content verbs/nouns like "come", "back",
# "word", "promise" must stay searchable.
_ASCII_STOPWORDS = frozenset({
    "a", "an", "am", "and", "are", "as", "at", "be", "been", "being", "but",
    "by", "can", "could", "did", "do", "does", "doing", "done", "for", "from",
    "had", "has", "have", "having", "he", "her", "hers", "herself", "him",
    "himself", "his", "how", "i", "if", "in", "into", "is", "it", "its",
    "itself", "just", "me", "might", "more", "most", "my", "myself", "no",
    "nor", "not", "of", "off", "on", "or", "other", "our", "ours", "out",
    "over", "shall", "she", "should", "so", "some", "such", "than", "that",
    "the", "their", "theirs", "them", "themselves", "then", "there", "these",
    "they", "this", "those", "through", "to", "too", "under", "until", "up",
    "very", "was", "we", "were", "what", "when", "where", "which", "while",
    "who", "whom", "whose", "why", "will", "with", "would", "you", "your",
    "yours", "yourself",
})

_CJK_STOPCHARS = frozenset("的了着过呢吗吧啊呀哦嗯这那哪在是有和与就都也很太真")

# Field weights for BM25 term frequencies.
_W_KEYWORDS = 2.2
_W_CONTENT = 1.0
_W_ABOUT = 0.8

# Exact keyword phrase hit bonus.
_PHRASE_BONUS_ASCII = 1.6
_PHRASE_BONUS_CJK = 1.8

# BM25 parameters tuned for short single-sentence memories.
_BM25_K1 = 1.2
_BM25_B = 0.55

# Cutoffs: clear noise while keeping strong matches and near-ties.
MIN_SCORE = 1.95
RELATIVE_RATIO = 0.50

_IMPORTANCE_FACTORS = {1: 0.90, 2: 1.00, 3: 1.15}


def _stem_ascii(word: str) -> str:
    """Lightweight English inflection normalizer (consistent on probe & docs)."""
    if len(word) <= 3 or word.isdigit():
        return word
    if word.endswith("'s") and len(word) > 4:
        word = word[:-2]
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("ied") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("ing") and len(word) > 5:
        stem = word[:-3]
        if len(stem) >= 3 and stem[-1] == stem[-2]:
            stem = stem[:-1]
        return stem
    if word.endswith("ed") and len(word) > 4:
        stem = word[:-2]
        if len(stem) >= 3 and stem[-1] == stem[-2]:
            stem = stem[:-1]
        return stem
    if word.endswith("es") and len(word) > 4:
        if word.endswith(("ches", "shes", "sses", "xes", "zes")):
            return word[:-2]
        return word[:-1]
    if word.endswith("s") and len(word) > 3 and not word.endswith("ss"):
        return word[:-1]
    if word.endswith("ly") and len(word) > 4:
        return word[:-2]
    # Unify silent trailing 'e' on longer stems ("promise" <-> "promised" -> "promis").
    if word.endswith("e") and len(word) >= 6:
        return word[:-1]
    return word


def tokenize(text: str) -> List[str]:
    """Extract normalized ASCII stems and CJK 2/3-gram features."""
    lowered = (text or "").lower()
    if not lowered.strip():
        return []

    tokens: List[str] = []
    for raw in _ASCII_WORD_RE.findall(lowered):
        if len(raw) < 2 or raw in _ASCII_STOPWORDS:
            continue
        stem = _stem_ascii(raw)
        if len(stem) >= 2 and stem not in _ASCII_STOPWORDS:
            tokens.append(stem)

    for run in _CJK_RUN_RE.findall(lowered):
        n = len(run)
        if n == 1:
            if run not in _CJK_STOPCHARS:
                tokens.append(f"c:{run}")
            continue
        for i in range(n - 1):
            bg = run[i : i + 2]
            if not (bg[0] in _CJK_STOPCHARS and bg[1] in _CJK_STOPCHARS):
                tokens.append(f"c:{bg}")
        if n >= 3:
            for i in range(n - 2):
                tokens.append(f"c:{run[i : i + 3]}")
    return tokens


def _extract_keywords(mem: Dict) -> List[str]:
    """Normalize either `keywords` (CSV string) or `keys` (list/string)."""
    raw = mem.get("keywords")
    if raw is None:
        raw = mem.get("keys")
    if isinstance(raw, (list, tuple)):
        parts: Iterable[str] = (str(item) for item in raw)
    else:
        parts = re.split(r"[,，;；\n]+", str(raw or ""))

    out: List[str] = []
    seen = set()
    for part in parts:
        clean = " ".join(part.split()).strip().lower()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return out


def _phrase_in_text(phrase: str, lowered_text: str) -> bool:
    """Match an explicit keyword phrase with word boundaries for ASCII."""
    if not phrase or not lowered_text:
        return False
    if _HAS_CJK_RE.search(phrase):
        return len(phrase) >= 2 and phrase in lowered_text
    if len(phrase) < 2 or phrase in _ASCII_STOPWORDS:
        return False
    pattern = rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])"
    return re.search(pattern, lowered_text) is not None


def _memory_tf(mem: Dict) -> Tuple[Counter, int, List[str]]:
    """Weighted term frequencies across keywords/keys, content, and about."""
    tf: Counter = Counter()
    kw_list = _extract_keywords(mem)
    kw_tokens = tokenize(" ".join(kw_list))
    ct_tokens = tokenize(str(mem.get("content") or ""))
    ab_tokens = tokenize(str(mem.get("about") or ""))

    for tok in kw_tokens:
        tf[tok] += _W_KEYWORDS
    for tok in ct_tokens:
        tf[tok] += _W_CONTENT
    for tok in ab_tokens:
        tf[tok] += _W_ABOUT

    length = len(kw_tokens) + len(ct_tokens) + len(ab_tokens)
    return tf, max(1, length), kw_list


def score_memories(
    probe: str,
    memories: Sequence[Dict],
    vector_scores: Optional[Dict[int, float]] = None,
) -> List[Tuple[float, int, Dict]]:
    """Score every memory against `probe`. Returns `[(score, id, mem), ...]` desc."""
    lowered_probe = (probe or "").lower()
    query_tokens = tokenize(lowered_probe)
    if not query_tokens and not lowered_probe.strip():
        return []

    n_docs = len(memories)
    if n_docs == 0:
        return []

    query_terms = list(dict.fromkeys(query_tokens))
    doc_data: List[Tuple[Counter, int, List[str]]] = []
    df: Counter = Counter()
    total_len = 0

    for mem in memories:
        tf, dl, kw_list = _memory_tf(mem)
        doc_data.append((tf, dl, kw_list))
        total_len += dl
        for tok in tf:
            df[tok] += 1

    avgdl = max(1.0, total_len / n_docs)
    # Small-room cold start: avoid collapsing IDF to near-zero when N < 8.
    n_eff = max(n_docs, 8)

    scored: List[Tuple[float, int, Dict]] = []
    for mem, (tf, dl, kw_list) in zip(memories, doc_data):
        mid = int(mem.get("id") or 0)
        bm25 = 0.0
        for tok in query_terms:
            freq = tf.get(tok, 0.0)
            if freq <= 0.0:
                continue
            doc_freq = df.get(tok, 0)
            idf = math.log(1.0 + (n_eff - doc_freq + 0.5) / (doc_freq + 0.5))
            # CJK 3-grams carry stronger phrase specificity than 2-grams.
            weight = 1.15 if (tok.startswith("c:") and len(tok) == 5) else 1.0
            denom = freq + _BM25_K1 * (1.0 - _BM25_B + _BM25_B * (dl / avgdl))
            bm25 += weight * idf * (freq * (_BM25_K1 + 1.0)) / denom

        phrase_bonus = 0.0
        for kw in kw_list:
            if _phrase_in_text(kw, lowered_probe):
                phrase_bonus += (
                    _PHRASE_BONUS_CJK if _HAS_CJK_RE.search(kw) else _PHRASE_BONUS_ASCII
                )
        # Cap phrase bonus so a memory packed with repetitive tags cannot dominate alone.
        phrase_bonus = min(phrase_bonus, 3.6)

        lexical = bm25 + phrase_bonus

        # Mild boost if the memory's `about` subject is mentioned in the probe,
        # gated on having at least minimal lexical signal so name mentions alone
        # do not pull unrelated facts.
        about = str(mem.get("about") or "").strip().lower()
        if about and lexical > 0.0 and _phrase_in_text(about, lowered_probe):
            lexical *= 1.12

        # Optional hybrid fusion when external /v1/embeddings scores are provided.
        vec_sim = float(vector_scores.get(mid, 0.0)) if vector_scores else 0.0
        if vector_scores and vec_sim >= 0.45:
            combined = lexical + max(0.0, (vec_sim - 0.35) * 9.0)
        else:
            combined = lexical

        # Single-companion importance weighting (group memories omit this field).
        raw_imp = mem.get("importance")
        if combined > 0.0 and raw_imp is not None:
            try:
                imp = min(3, max(1, int(raw_imp)))
            except (TypeError, ValueError):
                imp = 2
            combined *= _IMPORTANCE_FACTORS[imp]

        # Gentle rotation damping for frequently injected memories.
        use_count = max(0, int(mem.get("use_count") or 0))
        if combined > 0.0 and use_count > 0:
            combined *= 1.0 / (1.0 + 0.05 * min(use_count, 10))

        scored.append((combined, mid, mem))

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return scored


def rank_memories(
    probe: str,
    memories: Sequence[Dict],
    limit: int = 6,
    max_required: int = 2,
    vector_scores: Optional[Dict[int, float]] = None,
    min_score: float = MIN_SCORE,
    relative_ratio: float = RELATIVE_RATIO,
    char_budget: Optional[int] = None,
) -> List[Dict]:
    """Select the most relevant memories within count and character budgets."""
    scored = score_memories(probe, memories, vector_scores=vector_scores)
    if not scored:
        return []

    picked: List[Dict] = []
    picked_ids = set()
    used_chars = 0

    def _try_add(mem: Dict) -> bool:
        nonlocal used_chars
        if len(picked) >= limit:
            return False
        cost = len(str(mem.get("content") or ""))
        if char_budget is not None and picked and used_chars + cost > char_budget:
            return False
        picked.append(mem)
        picked_ids.add(int(mem.get("id") or 0))
        used_chars += cost
        return True

    # Standing instructions (`mark == 2`) bypass the score floor when enabled.
    if max_required > 0:
        req_added = 0
        for _, mid, mem in scored:
            if int(mem.get("mark") or 0) == 2:
                if _try_add(mem):
                    req_added += 1
                if req_added >= max_required:
                    break

    normal = [(s, mid, m) for s, mid, m in scored if mid not in picked_ids]
    if normal:
        best = normal[0][0]
        if best >= min_score:
            cutoff = max(min_score, best * relative_ratio)
            for score, _, mem in normal:
                if len(picked) >= limit:
                    break
                if score < cutoff:
                    break
                _try_add(mem)

    return picked[:limit]


def _char_bigrams(text: str) -> set:
    compact = re.sub(r"\s+", "", (text or "").lower())
    if len(compact) < 2:
        return {compact} if compact else set()
    return {compact[i : i + 2] for i in range(len(compact) - 1)}


def text_similarity(a: str, b: str) -> float:
    """Fast lexical similarity in [0, 1] for write-time near-duplicate fallback."""
    sa = " ".join((a or "").lower().split())
    sb = " ".join((b or "").lower().split())
    if not sa or not sb:
        return 0.0
    if sa == sb:
        return 1.0

    bg_a = _char_bigrams(sa)
    bg_b = _char_bigrams(sb)
    jaccard = len(bg_a & bg_b) / max(1, len(bg_a | bg_b))

    toks_a = set(tokenize(sa))
    toks_b = set(tokenize(sb))
    tok_jaccard = (
        len(toks_a & toks_b) / max(1, len(toks_a | toks_b))
        if (toks_a or toks_b)
        else 0.0
    )

    seq = SequenceMatcher(None, sa, sb).ratio()
    return max(seq, 0.5 * jaccard + 0.5 * tok_jaccard)


def is_near_duplicate(
    candidate: str,
    existing_contents: Iterable[str],
    threshold: float = 0.80,
) -> bool:
    """True when `candidate` is essentially a minor rephrasing of an existing row."""
    for existing in existing_contents:
        if text_similarity(candidate, existing) >= threshold:
            return True
    return False
