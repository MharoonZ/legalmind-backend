from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.exc import IntegrityError

from app.platform.common import Offset, conversation, dump, other_party
from app.platform.models import Account, Conversation, Message, Preparation, Rating, Review, WakeelProfile, now
from app.platform.schemas import PreparationAction, PreparationIn, SendMessage, StartConversation
from app.platform.security import DB, User, audit, notify, require_role
from app.platform.events import queue_event

router = APIRouter(tags=["Wakeel discovery and messaging"])


def all_ratings():
    """Average of agreement reviews, case ratings and engagement ratings per Wakeel."""
    scores = (
        select(Review.subject_id.label("subject_id"), Review.rating.label("score"))
        .union_all(select(Rating.subject_id, Rating.overall))
        .subquery()
    )
    return (
        select(scores.c.subject_id, func.avg(scores.c.score).label("rating"), func.count().label("reviews"))
        .group_by(scores.c.subject_id)
        .subquery()
    )


@router.get("/wakeels")
async def wakeels(
    request: Request,
    db: DB,
    q: str = Query("", max_length=160),
    city: str = Query("", max_length=120),
    specialization: str = Query("", max_length=160),
    offset: Offset = 0,
):
    async def load():
        ratings = all_ratings()
        query = (
            select(Account, WakeelProfile, ratings.c.rating, ratings.c.reviews)
            .join(WakeelProfile, Account.id == WakeelProfile.user_id)
            .outerjoin(ratings, ratings.c.subject_id == Account.id)
            .where(Account.active.is_(True), WakeelProfile.verification == "verified")
        )
        for column, value in ((Account.name, q), (Account.city, city), (WakeelProfile.specialization, specialization)):
            if value:
                query = query.where(column.icontains(value, autoescape=True))
        rows = (await db.execute(query.order_by(Account.name).offset(offset).limit(200))).all()
        results = []
        for account, professional, average, count in rows:
            results.append(
                {
                    "id": account.id,
                    "name": account.name,
                    "city": account.city,
                    **dump(professional),
                    "rating": float(average) if average else None,
                    "review_count": count or 0,
                }
            )
        return results

    hub = getattr(request.app.state, "realtime", None)
    if hub:
        return await hub.directory_cache.get((q, city, specialization, offset), load, enabled=lambda: hub.connected)
    return await load()


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
    ratings = all_ratings()
    average = (
        await db.execute(select(ratings.c.rating, ratings.c.reviews).where(ratings.c.subject_id == user.id))
    ).first()
    return {
        "id": user.id,
        "name": user.name,
        "city": user.city,
        **dump(profile),
        "rating": float(average[0]) if average and average[0] else None,
        "review_count": average[1] if average else 0,
        "reviews": await public_reviews(db, user.id),
    }


async def public_reviews(db, subject_id, limit=100):
    """Published feedback without author identities."""
    reviews = (
        await db.scalars(
            select(Review).where(Review.subject_id == subject_id).order_by(Review.created_at.desc()).limit(limit)
        )
    ).all()
    ratings = (
        await db.scalars(
            select(Rating).where(Rating.subject_id == subject_id).order_by(Rating.created_at.desc()).limit(limit)
        )
    ).all()
    rows = [
        {"id": r.id, "rating": r.rating, "body": r.body, "created_at": r.created_at, "kind": "agreement", "scores": {}}
        for r in reviews
    ] + [
        {
            "id": r.id,
            "rating": r.overall,
            "body": r.body,
            "created_at": r.created_at,
            "kind": "case" if r.case_id else "engagement",
            "scores": r.scores,
        }
        for r in ratings
    ]
    return sorted(rows, key=lambda row: row["created_at"], reverse=True)[:limit]


@router.get("/conversations")
async def conversations(user: User, db: DB, offset: Offset = 0):
    unread = (
        select(func.count(Message.id))
        .where(
            Message.conversation_id == Conversation.id,
            Message.sender_id != user.id,
            Message.read_at.is_(None),
            Message.case_id.is_(None),
        )
        .correlate(Conversation)
        .scalar_subquery()
    )
    peer_id = case((Conversation.user_id == user.id, Conversation.wakeel_id), else_=Conversation.user_id)
    rows = (
        await db.execute(
            select(Conversation, Account.name, unread)
            .join(Account, Account.id == peer_id)
            .where(or_(Conversation.user_id == user.id, Conversation.wakeel_id == user.id))
            .order_by(Conversation.updated_at.desc(), Conversation.id)
            .offset(offset)
            .limit(200)
        )
    ).all()
    ids = [item.id for item, _, _ in rows]
    latest = (
        select(Message.conversation_id, func.max(Message.created_at).label("at"))
        .where(Message.conversation_id.in_(ids or [""]), Message.case_id.is_(None))
        .group_by(Message.conversation_id)
        .subquery()
    )
    previews = dict(
        (
            await db.execute(
                select(Message.conversation_id, Message.body)
                .join(
                    latest, and_(latest.c.conversation_id == Message.conversation_id, latest.c.at == Message.created_at)
                )
                .where(Message.case_id.is_(None))
            )
        ).all()
    )
    return [
        {**dump(item), "peer_name": name, "unread": count, "preview": (previews.get(item.id) or "")[:120]}
        for item, name, count in rows
    ]


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
    query = select(Message).where(Message.conversation_id == item.id, Message.case_id.is_(None))
    if before:
        cursor = await db.get(Message, str(before))
        if not cursor or cursor.conversation_id != item.id:
            raise HTTPException(404, "Message not found.")
        query = query.where(
            or_(
                Message.created_at < cursor.created_at,
                and_(Message.created_at == cursor.created_at, Message.id < cursor.id),
            )
        )
    rows = (await db.scalars(query.order_by(Message.created_at.desc(), Message.id.desc()).limit(100))).all()
    from app.platform.models import Relationship

    peer = await db.get(Account, other_party(item, user))
    workspace = await db.scalar(
        select(Relationship.id).where(Relationship.conversation_id == item.id, Relationship.status != "ENDED").limit(1)
    )
    return {
        **dump(item),
        "peer_name": peer.name,
        "relationship_id": workspace,
        "messages": [dump(m) for m in reversed(rows)],
    }


@router.post("/conversations/{identifier}/read")
async def read_messages(identifier: UUID, user: User, db: DB):
    from sqlalchemy import update

    item = await conversation(db, identifier, user, lock=True)
    read_at = now()
    await db.execute(
        update(Message)
        .where(Message.conversation_id == item.id, Message.sender_id != user.id, Message.read_at.is_(None))
        .values(read_at=read_at)
    )
    queue_event(
        db,
        [item.user_id, item.wakeel_id],
        "conversation.read",
        conversation_id=item.id,
        reader_id=user.id,
        read_at=read_at.isoformat(),
    )
    return {"ok": True, "read_at": read_at.isoformat()}


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
    await notify(db, peer.id, f"New message from {user.name}", f"/messages/{item.id}")
    await db.flush()
    queue_event(db, [item.user_id, item.wakeel_id], "message.created", conversation_id=item.id, message_id=message.id)
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
    await notify(db, item.wakeel_id, f"Contract preparation requested by {user.name}", "/agreements")
    audit(db, user, "preparation_requested", row.id)
    return dump(row)


@router.post("/preparations/{identifier}/actions")
async def preparation_action(identifier: UUID, body: PreparationAction, user: User, db: DB):
    from app.platform.common import verified_wakeel
    from app.platform.events import invalidate

    await verified_wakeel(db, user)
    row = await db.scalar(select(Preparation).where(Preparation.id == str(identifier)).with_for_update())
    if not row:
        raise HTTPException(404, "Preparation request not found.")
    thread = await conversation(db, row.conversation_id, user)
    if row.status != "PREPARATION_REQUESTED":
        raise HTTPException(409, "This preparation request was already handled.")
    row.status = "ACCEPTED" if body.action == "accept" else "DECLINED"
    audit(db, user, "preparation_" + body.action, row.id, body.reason)
    await notify(db, thread.user_id, f"Your agreement preparation request was {row.status.lower()}", "/agreements")
    invalidate(db, [thread.user_id, thread.wakeel_id], "/preparations", "/agreements")
    return dump(row)
