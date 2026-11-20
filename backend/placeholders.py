"""Card-level text macros.

SillyTavern character cards write ``{{char}}`` and ``{{user}}`` instead of a
concrete name, so the same card works with any persona. Those placeholders used
to reach the prompt assembly verbatim: a card whose description says
"{{char}} folds her arms" made the model read a placeholder where a name should
be, and a careless model echoed it back into the reply.

Substitution happens where the prompt is built, not when the card is imported.
Editing a card's name must change what the model reads on the next turn, and an
imported card is not modified on disk.
"""

import re

# Cards in the wild spell these in every case: {{char}}, {{Char}}, {{CHAR}},
# and rarely {{character}}. The whitespace inside the braces varies too.
_MACRO = re.compile(r"\{\{\s*([A-Za-z_]+)\s*\}\}")

_CHARACTER_ALIASES = {"char", "character"}
_USER_ALIASES = {"user", "persona"}


def substitute(text, char_name: str = "", user_name: str = "the user") -> str:
    """Replace card macros with the names this branch is actually using.

    Unknown macros are left untouched on purpose: a card may carry placeholders
    for some other frontend, and silently deleting their text would lose data.
    """
    if not text or "{{" not in text:
        return text or ""

    def replace(match: re.Match) -> str:
        kind = match.group(1).lower()
        if kind in _CHARACTER_ALIASES:
            return char_name or "the character"
        if kind in _USER_ALIASES:
            return user_name or "the user"
        return match.group(0)

    return _MACRO.sub(replace, text)


def has_macros(text) -> bool:
    """True when the text carries at least one recognised macro."""
    if not text or "{{" not in text:
        return False
    return _MACRO.search(text) is not None
