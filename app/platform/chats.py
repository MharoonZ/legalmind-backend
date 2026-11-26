import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import and_, or_, select

from app.api.deps import SettingsDep, enforce_rate_limit, get_services
from app.api.routes.qa import ask
from app.platform.models import Chat, ChatMessage, now
from app.platform.schemas import ChatAsk, GuestImport
from app.platform.security import DB, User
from app.platform.common import Offset, dump
from app.platform.events import queue_event
from app.platform.security import throttle
from app.schemas.qa import AskIn, ConversationTurn

router = APIRouter(prefix="/chats", tags=["saved AI conversations"])


@router.get("")
async def chats(user: User, db: DB, offset: Offset = 0):
    return [
        dump(c)
        for c in (
            await db.scalars(
                select(Chat).where(Chat.user_id == user.id).order_by(Chat.updated_at.desc()).offset(offset).limit(200)
            )
        ).all()
    ]


@router.post("", status_code=201)
async def create(user: User, db: DB):
    item = Chat(user_id=user.id)
    db.add(item)
    await db.flush()
    return dump(item)


async def owned(db, identifier, user, lock=False):
    query = select(Chat).where(Chat.id == str(identifier), Chat.user_id == user.id)
    if lock:
        query = query.with_for_update()
    chat = await db.scalar(query)
    if chat is None:
        raise HTTPException(404, "Chat not found.")
    return chat


@router.post("/import", status_code=201)
async def import_guest(body: GuestImport, user: User, db: DB, services=Depends(get_services)):
    # Imported transcripts belong only to the signing-in user. Check every quoted
    # citation against the current publication before saving it.
    snapshot = await services.index.ensure_loaded()
    if not snapshot:
        raise HTTPException(503, "The source collection is unavailable. Your guest chat remains on this device.")
    chunks = {str(chunk.id): chunk for chunk in snapshot.chunks}
    for turn in body.messages:
        if turn.question != turn.response.question:
            raise HTTPException(409, "The guest question and response do not match.")
        for citation in turn.response.citations:
            chunk = chunks.get(str(citation.chunk_id))
            if (
                not chunk
                or str(citation.publication_id) != str(snapshot.id)
                or citation.quote not in " ".join(chunk.text.split())
                or citation.law != snapshot.instruments[chunk.instrument_id].title
                or citation.label != chunk.label
                or citation.kind != chunk.kind
                or not any(
                    str(s.source_span_id) == str(citation.source_span_id)
                    and str(s.file_id) == str(citation.file_id)
                    and s.file_sha256 == citation.file_sha256
                    and s.page_index == citation.page_index
                    for s in chunk.segments
                )
            ):
                raise HTTPException(
                    409,
                    "A guest citation is from an older collection. Keep this chat on this device or start a new saved chat.",
                )
    chat = Chat(user_id=user.id)
    db.add(chat)
    await db.flush()
    from uuid import uuid4

    for turn in body.messages:
        db.add(
            ChatMessage(
                chat_id=chat.id,
                request_id=str(uuid4()),
                question=turn.question,
                response=turn.response.model_dump(mode="json"),
            )
        )
    await db.flush()
    queue_event(db, [user.id], "title.requested", chat_id=chat.id)
    return dump(chat)


@router.get("/{chat_id}")
async def detail(chat_id: UUID, user: User, db: DB, before: UUID | None = None):
    item = await owned(db, chat_id, user)
    query = select(ChatMessage).where(ChatMessage.chat_id == item.id)
    if before:
        cursor = await db.get(ChatMessage, str(before))
        if not cursor or cursor.chat_id != item.id:
            raise HTTPException(404, "Message not found.")
        query = query.where(
            or_(
                ChatMessage.created_at < cursor.created_at,
                and_(ChatMessage.created_at == cursor.created_at, ChatMessage.id < cursor.id),
            )
        )
    messages = (await db.scalars(query.order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).limit(50))).all()
    return {**dump(item), "messages": [dump(m) for m in reversed(messages)], "has_more": len(messages) == 50}


@router.post("/{chat_id}/title")
async def retry_title(chat_id: UUID, request: Request, user: User, db: DB):
    await throttle(request, "title_retry", user.id, 3)
    chat = await owned(db, chat_id, user, lock=True)
    if chat.title_status == "failed":
        chat.title_status, chat.title_attempts, chat.title_attempted_at = "pending", 0, None
        queue_event(db, [user.id], "title.requested", chat_id=chat.id)
    return dump(chat)


@router.post("/{chat_id}/ask", dependencies=[Depends(enforce_rate_limit)])
async def answer(
    chat_id: UUID, body: ChatAsk, user: User, db: DB, settings: SettingsDep, services=Depends(get_services)
):
    chat = await owned(db, chat_id, user, lock=True)
    existing = await db.scalar(
        select(ChatMessage).where(ChatMessage.chat_id == chat.id, ChatMessage.request_id == str(body.request_id))
    )
    if existing:
        if existing.question != body.question:
            raise HTTPException(409, "This request identifier belongs to another message.")
        return dump(existing)
    previous = (
        await db.scalars(
            select(ChatMessage).where(ChatMessage.chat_id == chat.id).order_by(ChatMessage.created_at.desc()).limit(10)
        )
    ).all()
    history = []
    for message in reversed(previous):
        history.extend(
            [
                ConversationTurn(role="user", content=message.question),
                ConversationTurn(role="assistant", content=message.response["answer"][:8000]),
            ]
        )
    result = await ask(AskIn(question=body.question, history=history, law=body.law), settings, services)
    payload = json.loads(result.body)
    if result.status_code != 200:
        raise HTTPException(503, payload.get("answer", "The answer service is unavailable."))
    message = ChatMessage(chat_id=chat.id, request_id=str(body.request_id), question=body.question, response=payload)
    db.add(message)
    chat.updated_at = now()
    if not previous:
        queue_event(db, [user.id], "title.requested", chat_id=chat.id)
    await db.flush()
    return dump(message)
