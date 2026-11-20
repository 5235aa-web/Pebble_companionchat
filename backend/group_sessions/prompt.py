"""Group rooms — prompt assembly.

The card fields a room sends are deliberately narrow: name, personality,
description and scenario. A full card (examples, its own advanced/system/
post-history text) belongs to a one-to-one conversation; sending it here would
stack three sets of instructions and dilute the scene.

What a room adds on top:

* a shared `[Scene]` line that all members act inside,
* a `[Present]` roster so each speaker knows who else is in the room,
* the app-wide rules from `global_prompts`,
* group-specific conduct rules,
* group-level memory.

Speaker attribution is the part that matters most. History is replayed with the
current speaker's own lines as `assistant` and everyone else's as `user` with a
name prefix — otherwise the model reads three characters' dialogue as its own
and starts writing all of them.
"""

import base64
import logging
import re
from typing import Any, Dict, List, Optional, Union

from ..attachment_parser import resolve_image_path
from ..global_prompts import load_global_prompts
from ..memory_engine import resolve_user_name
from ..placeholders import substitute

logger = logging.getLogger(__name__)

# How a member is introduced in the roster: one line, no card dump.
ROSTER_DESC_CHARS = 90

# A card's personality and description can run to several thousand characters. A
# room needs enough of them to hold the voice, not the whole dossier — and with
# three members the prompt doubles or triples that cost. The opening of each
# field is where a card puts its defining traits, so that is what survives.
CHARACTER_FIELD_CHARS = 2000

# A scenario is the premise of the scene rather than a portrait of a person, and
# it is read once. Its ceiling stays lower so three of them cannot crowd out the
# transcript.
SCENARIO_FIELD_CHARS = 1800

GROUP_RULES = (
    "[Group]\n"
    "You are one of several people sharing this scene with the user.\n"
    "Speak only for yourself: never write another character's words, actions or "
    "thoughts, and never decide what the user does next.\n"
    "Use names when it helps the others tell who is being addressed.\n"
    "A small gesture or a look is a complete reply — you do not have to answer "
    "everything that is said.\n"
    "Do not narrate what other people just did, and do not summarise the scene."
)


NARRATION_RULES = (
    "This is a Narration, not a message from the user, and not the user "
    "speaking to you: it is a voice-over. It describes what the user and the "
    "characters are doing, how things stand, and what this moment is.\n"
    "Build your reply on that narration: stay inside the moment it sets and act "
    "from inside it. Never answer it as if it were dialogue, and never recite "
    "it back."
)


_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def _strip_mentions(text: str, members: List[Dict]) -> str:
    """Turn `@Name` into `Name` for the model's eyes only.

    The @ is this UI's way of addressing someone, not part of the sentence. Left
    in, it teaches the model to answer with "@Evelyn, did you hear that", which
    belongs to no one's voice. Only known member names (and word-level aliases
    for multi-word names) are touched, and email addresses like a@b.com survive
    untouched.
    """
    tokens: List[str] = []
    for member in members:
        name = str(member.get("name") or "").strip()
        if not name:
            continue
        tokens.append(name)
        parts = name.split()
        if len(parts) > 1:
            for part in parts:
                if len(part) >= 2 or _CJK.search(part):
                    tokens.append(part)

    # Longest tokens first so "@Grandma Evelyn" strips cleanly before "@Grandma".
    tokens = sorted(set(tokens), key=len, reverse=True)
    out = text
    for token in tokens:
        tail_boundary = "" if _CJK.search(token[-1]) else r"(?![A-Za-z0-9])"
        pattern = rf"(?<![A-Za-z0-9._%+-])@\s*{re.escape(token)}{tail_boundary}"
        out = re.sub(pattern, token, out, flags=re.IGNORECASE)
    return out


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _truncate(text: str, limit: int) -> str:
    """Cut a long field without flattening its paragraph breaks."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # Prefer ending on a paragraph boundary so the field does not stop mid-word.
    head = cut.rsplit("\n\n", 1)[0].rstrip()
    return (head or cut.rstrip()) + "\n…"


def user_display_name(session: Dict, members: List[Dict]) -> str:
    """The user's name as the room should address them.

    The card's own `user_info` is the primary source; a room-level note is
    appended, so a name typed once in the room applies to every member that has
    not declared one.
    """
    note = str(session.get("user_note") or "")
    for member in members:
        name = resolve_user_name(str(member.get("user_info") or ""))
        if name != "the user":
            return name
    return resolve_user_name(note)


def _macro(speaker_name: str, user_name: str):
    return lambda value: substitute(value, speaker_name, user_name)


def build_system_prompt(
    session: Dict,
    members: List[Dict],
    speaker: Dict,
    memories: Optional[List[Dict]] = None,
    has_narration: bool = False,
) -> str:
    """System prompt for one speaker's turn."""
    speaker_name = str(speaker.get("name") or "your character")
    user_name = user_display_name(session, members)
    sub = _macro(speaker_name, user_name)
    rules = load_global_prompts()

    parts: List[str] = []

    scene = sub(str(session.get("scene") or "")).strip()
    if scene:
        parts.append(f"[Scene]\n{scene}")

    roster_lines = []
    for member in members:
        name = str(member.get("name") or "")
        if not name:
            continue
        if member.get("companion_id") == speaker.get("companion_id"):
            # The speaker's own card is spelled out under [Character] below, so
            # a line of it here would only spend budget on itself.
            roster_lines.append(f"- {name} (you)")
            continue
        desc = _clip(
            sub(str(member.get("personality") or member.get("description") or "")),
            ROSTER_DESC_CHARS,
        )
        roster_lines.append(f"- {name}: {desc}" if desc else f"- {name}")
    if roster_lines:
        parts.append(
            "[Present]\n"
            "Everyone in this scene, with the user:\n" + "\n".join(roster_lines)
        )

    who = [f"[Character]\nYou are {speaker_name}."]
    personality = _truncate(sub(str(speaker.get("personality") or "")), CHARACTER_FIELD_CHARS)
    description = _truncate(sub(str(speaker.get("description") or "")), CHARACTER_FIELD_CHARS)
    scenario = _truncate(sub(str(speaker.get("scenario") or "")), SCENARIO_FIELD_CHARS)
    if personality:
        who.append(f"[Personality]\n{personality}")
    if description:
        who.append(f"[Description]\n{description}")
    if scenario:
        who.append(f"[Scenario]\n{scenario}")
    parts.append("\n".join(who))

    user_bits = []
    card_info = sub(str(speaker.get("user_info") or "")).strip()
    if card_info:
        user_bits.append(card_info)
    note = sub(str(session.get("user_note") or "")).strip()
    if note:
        user_bits.append(note)
    if user_bits:
        parts.append("[User Info]\n" + "\n\n".join(user_bits))

    advanced = sub(rules.get("advanced") or "").strip()
    if advanced:
        parts.append(f"[Advanced Prompt]\n{advanced}")

    system_rule = sub(rules.get("system") or "").strip()
    if system_rule:
        parts.append(f"[System Instruction]\n{system_rule}")

    parts.append(GROUP_RULES)

    # Only taught when a narration is actually present: describing a tag the
    # model never sees invites it to invent one.
    if has_narration:
        parts.append(NARRATION_RULES)

    if memories:
        lines = "\n".join(f"- {m['content']}" for m in memories)
        parts.append(
            "[Memories]\n"
            "Things the people in this room remember from earlier. Refer to them "
            "naturally when they matter; never recite them as a list.\n"
            f"{lines}"
        )

    return "\n\n".join(parts)


def _image_parts(attachments: List[Dict]) -> List[Dict[str, Any]]:
    """Convert stored image attachments into OpenAI-compatible `image_url` parts."""
    parts: List[Dict[str, Any]] = []
    for item in attachments:
        if not isinstance(item, dict) or item.get("kind") != "image":
            continue
        url = str(item.get("url") or "")
        path = resolve_image_path(url)
        if not path:
            logger.warning("Group image attachment missing on disk: %s", url)
            continue
        try:
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError as exc:
            logger.warning("Failed to read group image attachment %s: %s", path, exc)
            continue
        mime = str(item.get("mime") or "image/png")
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{encoded}"},
        })
    return parts


def _with_recent_images(
    text_line: str,
    attachments: List[Dict],
) -> Union[str, List[Dict[str, Any]]]:
    image_parts = _image_parts(attachments)
    if not image_parts:
        return text_line
    return [{"type": "text", "text": text_line}, *image_parts]


def build_messages(
    session: Dict,
    members: List[Dict],
    speaker: Dict,
    history: List[Dict],
    memories: Optional[List[Dict]] = None,
    compaction: Optional[Dict] = None,
    include_images: bool = True,
) -> List[Dict]:
    """Full message list for one speaker's turn.

    The speaker's own previous lines carry the `assistant` role; every other
    voice is `user` with a name prefix. Without that split a model treats the
    whole room's dialogue as something it said, and answers for everyone.
    """
    member_names = {
        str(m.get("companion_id")): str(m.get("name") or "") for m in members
    }
    user_name = user_display_name(session, members)
    if user_name == "the user":
        user_name = "You"
    speaker_id = str(speaker.get("companion_id") or "")

    # Narration never replays. It is a direction for exactly one reply — the one
    # that follows it — and once that reply exists the direction is spent. Only
    # a narration still sitting at the end of the transcript reaches the model;
    # an older one belongs to the reader alone. Re-injecting an old one would
    # move a past moment to the present and the reply would play that instead.
    latest_narration = ""
    last_image_idx = -1
    for idx, msg in enumerate(history):
        if str(msg.get("kind") or "") == "narration":
            continue
        if (
            include_images
            and str(msg.get("speaker_id") or "") == "user"
            and any(
                isinstance(a, dict) and a.get("kind") == "image"
                for a in (msg.get("attachments") or [])
            )
        ):
            last_image_idx = idx

    if history and str(history[-1].get("kind") or "") == "narration":
        latest_narration = str(history[-1].get("content") or "").strip()

    # Keep images inline only while the user's share is still fresh (current turn,
    # immediate relay, or reroll) so older photos do not inflate every future call.
    if last_image_idx >= 0 and (len(history) - 1 - last_image_idx) > 2:
        last_image_idx = -1

    messages: List[Dict] = [{
        "role": "system",
        "content": build_system_prompt(
            session, members, speaker, memories, bool(latest_narration)
        ),
    }]

    if compaction and compaction.get("summary"):
        messages.append({
            "role": "system",
            "content": (
                "[Earlier in this room]\n"
                f"{compaction['summary']}\n"
                "[/Earlier in this room]"
            ),
        })

    for idx, msg in enumerate(history):
        content = str(msg.get("content") or "").strip()
        is_image_msg = idx == last_image_idx
        if not content and not is_image_msg:
            continue
        sid = msg.get("speaker_id")
        name = str(msg.get("speaker_name") or member_names.get(str(sid)) or "")
        kind = str(msg.get("kind") or "say")

        if kind == "narration":
            # Held out of the replay; the newest one is injected once below.
            continue
        if kind == "cue":
            # A card click that names the next speaker. It is not something the
            # user said, so it never becomes dialogue — the tail of the history
            # turns it into a floor note below instead.
            continue
        if sid == speaker_id:
            messages.append({"role": "assistant", "content": content})
        elif str(sid) == "user":
            # Always the name the room calls them by, whatever the stored row
            # says: lines written before this change carry "You". Mentions lose
            # their @ on the way in, not in storage.
            clean = _strip_mentions(content, members) if content else ""
            text_line = f"{user_name}: {clean}" if clean else f"{user_name}:"
            msg_content: Union[str, List[Dict[str, Any]]] = (
                _with_recent_images(text_line, msg.get("attachments") or [])
                if is_image_msg
                else text_line
            )
            messages.append({"role": "user", "content": msg_content})
        else:
            who = name or user_name
            messages.append({"role": "user", "content": f"{who}: {content}"})

    if latest_narration:
        clean_narration = _strip_mentions(latest_narration, members)
        messages.append({
            "role": "system",
            "content": (
                f"{clean_narration}\n"
                "This is the present moment your reply takes place in."
            ),
        })

    if history and str(history[-1].get("kind") or "") == "cue":
        # The user clicked this character's card and said nothing. Handing the
        # floor over is the whole message: there is no line to answer, and the
        # stored cue must never reach the model, or it reads as the user
        # calling the character by name and answers that instead of the scene.
        messages.append({
            "role": "system",
            "content": (
                "[Floor]\n"
                "The user has handed this turn to you without saying anything. "
                "Nobody is addressing you, so there is nothing to answer. "
                "Continue the scene from where it stands and speak as yourself."
            ),
        })

    post = build_post_history(session, members, speaker)
    if post:
        messages.append({"role": "system", "content": post})
    return messages


def build_post_history(session: Dict, members: List[Dict], speaker: Dict) -> str:
    """The final system note. Only the app-wide rule goes here — a room has no
    per-card post-history text to place after the scene."""
    rules = load_global_prompts()
    speaker_name = str(speaker.get("name") or "your character")
    user_name = user_display_name(session, members)
    text = substitute(str(rules.get("postHistory") or ""), speaker_name, user_name).strip()
    if not text:
        return ""
    return f"[Post-History Instructions]\n{text}"
