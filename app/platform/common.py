from fastapi import HTTPException
from fastapi import Query
from typing import Annotated
from sqlalchemy import or_, select

from app.platform.models import Agreement, Conversation, WakeelProfile

Offset = Annotated[int, Query(ge=0)]


def dump(row, exclude=()):
    return {c.name: getattr(row, c.name) for c in row.__table__.columns if c.name not in exclude}


async def conversation(db, identifier, user, lock=False):
    query = select(Conversation).where(
        Conversation.id == str(identifier), or_(Conversation.user_id == user.id, Conversation.wakeel_id == user.id)
    )
    if lock:
        query = query.with_for_update()
    item = await db.scalar(query)
    if item is None:
        raise HTTPException(404, "Conversation not found.")
    return item


async def agreement(db, identifier, user, lock=False):
    query = (
        select(Agreement)
        .join(Conversation)
        .where(Agreement.id == str(identifier), or_(Conversation.user_id == user.id, Conversation.wakeel_id == user.id))
    )
    if lock:
        query = query.with_for_update(of=Agreement)
    item = await db.scalar(query)
    if not item:
        raise HTTPException(404, "Agreement not found.")
    thread = await conversation(db, item.conversation_id, user)
    if item.status == "DRAFT" and user.id != thread.wakeel_id:
        raise HTTPException(404, "Agreement not found.")
    return item, thread


async def verified_wakeel(db, user):
    if user.role != "wakeel":
        raise HTTPException(403, "Only a Wakeel can prepare an agreement.")
    profile = await db.get(WakeelProfile, user.id)
    if not profile or profile.verification != "verified":
        raise HTTPException(403, "Your professional profile must be verified first.")


def other_party(thread, user):
    return thread.wakeel_id if user.id == thread.user_id else thread.user_id
