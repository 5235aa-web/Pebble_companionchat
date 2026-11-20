"""Application settings that are not tied to a companion.

Only the shared prompt rules live here for now. They belong to the app rather
than to a card: "never write my lines" is a rule about how the model talks to
me, not about who the character is.
"""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from .global_prompts import load_global_prompts, save_global_prompts

router = APIRouter(prefix="/settings", tags=["settings"])


class GlobalPromptsPayload(BaseModel):
    """The three shared rules, mirroring the card fields they extend."""

    advanced: str = Field(default="", description="Scope: what may happen in a scene")
    system: str = Field(default="", description="How the character behaves")
    postHistory: str = Field(default="", description="Hard rules, repeated at the very end")


@router.get("/global-prompts")
async def get_global_prompts_endpoint():
    """Return the shared rules. Every field is a string, empty when unset."""
    return load_global_prompts()


@router.put("/global-prompts")
async def put_global_prompts_endpoint(request: GlobalPromptsPayload):
    """Replace the shared rules as a set, so clearing a box really clears it."""
    return save_global_prompts(request.model_dump())
