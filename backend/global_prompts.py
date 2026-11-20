"""Application-wide prompt rules.

Three card fields steer how a reply is written: `advanced_prompt` (the scope —
what may happen), `system_prompt` (how the character behaves) and
`post_history_instructions` (the hard rules repeated just before generation).
They live on the card, which is right for anything character-specific and wrong
for rules that apply everywhere: "never write my lines" has nothing to do with
who the character is, and typing it into ten cards means ten places to fix when
it changes.

This module stores that shared layer in `app.json`, next to `modelsRoot`. Cards
keep working exactly as before; the global text is appended after the card's own
text in each of the three corresponding places.
"""

from typing import Dict

from .paths import load_app_config, save_app_config

CONFIG_KEY = "globalPrompts"

FIELDS = ("advanced", "system", "postHistory")

# A prompt rule that runs to thousands of characters is almost always a pasted
# novel rather than a rule. The cap keeps a bad paste from eating the context
# window on every single turn.
MAX_CHARS = 4000


def load_global_prompts() -> Dict[str, str]:
    """Current global rules. Missing or malformed fields read as empty."""
    raw = load_app_config().get(CONFIG_KEY)
    if not isinstance(raw, dict):
        return {name: "" for name in FIELDS}
    out: Dict[str, str] = {}
    for name in FIELDS:
        value = raw.get(name)
        out[name] = value.strip() if isinstance(value, str) else ""
    return out


def save_global_prompts(values: Dict[str, str]) -> Dict[str, str]:
    """Replace the global rules as a set. Unknown keys are ignored."""
    cleaned: Dict[str, str] = {}
    for name in FIELDS:
        value = values.get(name)
        text = value.strip() if isinstance(value, str) else ""
        cleaned[name] = text[:MAX_CHARS]
    config = load_app_config()
    config[CONFIG_KEY] = cleaned
    save_app_config(config)
    return cleaned
