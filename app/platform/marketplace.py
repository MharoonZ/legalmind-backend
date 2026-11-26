from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from app.platform.common import Offset, conversation, dump, other_party
from app.platform.models import Account, Conversation, Message, Preparation, Review, WakeelProfile, now
from app.platform.schemas import PreparationIn, SendMessage, StartConversation
from app.platform.security import DB, User, audit, notify, require_role

router = APIRouter(tags=["Wakeel discovery and messaging"])


@router.get("/wakeels")
async def wakeels(
    db: DB,
    q: str = Query("", max_length=160),
    city: str = Query("", max_length=120),
    specialization: str = Query("", max_length=160),
    offset: Offset = 0,
):
    query = (
        select(Account, WakeelProfile)
        .join(WakeelProfile, Account.id == WakeelProfile.user_id)
        .where(Account.active.is_(True), WakeelProfile.verification == "verified")
    )
    for column, value in ((Account.name, q), (Account.city, city), (WakeelProfile.specialization, specialization)):
        if value:
            query = query.where(column.icontains(value, autoescape=True))
    rows = (await db.execute(query.order_by(Account.name).offset(offset).limit(200))).all()
    ratings = (
        await db.execute(
            select(Review.subject_id, func.avg(Review.rating), func.count(Review.id))
            .where(Review.subject_id.in_([account.id for account, _ in rows]))
            .group_by(Review.subject_id)
        )
    ).all()
    ratings_by_user = {subject: (average, count) for subject, average, count in ratings}
    results = []
    for account, professional in rows:
        average, count = ratings_by_user.get(account.id, (None, 0))
        results.append(
            {
                "id": account.id,
                "name": account.name,
                "city": account.city,
                **dump(professional),
                "rating": float(average) if average else None,
                "review_count": count,
            }
        )
    return results


@router.get("/wakeels/{identifier}")
async def wakeel(identifier: UUID, db: DB):
    rows = (
        await db.execute(
            select(Account, WakeelProfile)
            .join(WakeelProfile)
            .where(Account.id == str(identifier), Account.active.is_(True), WakeelProfile.verification == "verified")
        )
    ).first()
    if not rows:
        raise HTTPException(404, "Wakeel not found.")
    user, profile = rows
    reviews = (
        await db.scalars(
            select(Review).where(Review.subject_id == user.id).order_by(Review.created_at.desc()).limit(100)
        )
    ).all()
    return {
        "id": user.id,
        "name": user.name,
        "city": user.city,
        **dump(profile),
        "reviews": [dump(r, ("author_id",)) for r in reviews],
    }


@router.get("/conversations")
async def conversations(user: User, db: DB, offset: Offset = 0):
    items = (
        await db.scalars(
            select(Conversation)
            .where(or_(Conversation.user_id == user.id, Conversation.wakeel_id == user.id))
            .order_by(Conversation.updated_at.desc())
            .offset(offset)
            .limit(200)
        )
    ).all()
    peers = (await db.scalars(select(Account).where(Account.id.in_([other_party(item, user) for item in items])))).all()
    names = {peer.id: peer.name for peer in peers}
    counts = (
        await db.execute(
            select(Message.conversation_id, func.count(Message.id))
            .where(
                Message.conversation_id.in_([item.id for item in items]),
                Message.sender_id != user.id,
                Message.read_at.is_(None),
            )
            .group_by(Message.conversation_id)
        )
    ).all()
    unread_counts = dict(counts)
    results = []
    for item in items:
        results.append(
            {**dump(item), "peer_name": names[other_party(item, user)], "unread": unread_counts.get(item.id, 0)}
        )
    return results


@router.post("/conversations", status_code=201)
async def start(body: StartConversation, user: User, db: DB):
    require_role(user, "user")
    wakeel = await db.get(WakeelProfile, str(body.wakeel_id))
    account = await db.get(Account, str(body.wakeel_id))
    if not wakeel or wakeel.verification != "verified" or not account.active:
        raise HTTPException(404, "Verified Wakeel not found.")
    query = select(Conversation).where(Conversation.user_id == user.id, Conversation.wakeel_id == str(body.wakeel_id))
    item = await db.scalar(query)
    if item:
        return dump(item)
    try:
        async with db.begin_nested():
            item = Conversation(user_id=user.id, wakeel_id=str(body.wakeel_id))
            db.add(item)
            await db.flush()
    except IntegrityError:
        item = await db.scalar(query)
    return dump(item)


@router.get("/conversations/{identifier}")
async def messages(identifier: UUID, user: User, db: DB, before: UUID | None = None):
    item = await conversation(db, identifier, user)
    query = select(Message).where(Message.conversation_id == item.id)
    if before:
        cursor = await db.get(Message, str(before))
        if not cursor or cursor.conversation_id != item.id:
            raise HTTPException(404, "Message not found.")
        query = query.where(Message.created_at < cursor.created_at)
    rows = (await db.scalars(query.order_by(Message.created_at.desc()).limit(100))).all()
    peer = await db.get(Account, other_party(item, user))
    return {**dump(item), "peer_name": peer.name, "messages": [dump(m) for m in reversed(rows)]}


@router.post("/conversations/{identifier}/read")
async def read_messages(identifier: UUID, user: User, db: DB):
    from sqlalchemy import update

    item = await conversation(db, identifier, user)
    await db.execute(
        update(Message)
        .where(Message.conversation_id == item.id, Message.sender_id != user.id, Message.read_at.is_(None))
        .values(read_at=now())
    )
    return {"ok": True}


@router.post("/conversations/{identifier}/messages", status_code=201)
async def send(identifier: UUID, body: SendMessage, user: User, db: DB):
    item = await conversation(db, identifier, user, lock=True)
    peer = await db.get(Account, other_party(item, user))
    if not peer.active:
        raise HTTPException(409, "This account is currently unavailable.")
    existing = await db.scalar(
        select(Message).where(
            Message.conversation_id == item.id, Message.sender_id == user.id, Message.request_id == str(body.request_id)
        )
    )
    if existing:
        if existing.body != body.body:
            raise HTTPException(409, "This request identifier belongs to another message.")
        return dump(existing)
    message = Message(conversation_id=item.id, sender_id=user.id, request_id=str(body.request_id), body=body.body)
    db.add(message)
    item.updated_at = now()
    notify(db, peer.id, f"New message from {user.name}", f"/messages/{item.id}")
    await db.flush()
    return dump(message)


@router.get("/preparations")
async def preparations(user: User, db: DB, offset: Offset = 0):
    query = (
        select(Preparation)
        .join(Conversation)
        .where(or_(Conversation.user_id == user.id, Conversation.wakeel_id == user.id))
    )
    return [
        dump(p)
        for p in (await db.scalars(query.order_by(Preparation.created_at.desc()).offset(offset).limit(200))).all()
    ]


@router.post("/preparations", status_code=201)
async def prepare(body: PreparationIn, user: User, db: DB):
    require_role(user, "user")
    item = await conversation(db, body.conversation_id, user)
    row = Preparation(conversation_id=item.id, title=body.title, requirements=body.requirements)
    db.add(row)
    await db.flush()
    notify(db, item.wakeel_id, f"Contract preparation requested by {user.name}", "/agreements")
    audit(db, user, "preparation_requested", row.id)
    return dump(row)
