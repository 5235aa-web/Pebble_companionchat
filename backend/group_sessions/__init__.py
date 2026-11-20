"""Group rooms.

Several companions in one conversation, on their own tables, with their own
prompt assembly, memory and compaction. Nothing here is reachable from the
single-companion thread or from script mode: those read `companion_messages`,
these read `group_messages`.
"""

from .db import MAX_MEMBERS

__all__ = ["MAX_MEMBERS"]
