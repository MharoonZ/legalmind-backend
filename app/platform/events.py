"""Small transactional event envelopes; private content stays out of PostgreSQL NOTIFY."""

from uuid import uuid4


def queue_event(db, recipients, kind, **payload):
    db.info.setdefault("platform_events", []).append(
        {"id": str(uuid4()), "recipients": list(dict.fromkeys(recipients)), "type": kind, **payload}
    )


def invalidate(db, recipients, *paths):
    queue_event(db, recipients, "invalidate", paths=list(paths))


def directory_changed(db):
    queue_event(db, [], "directory.changed")
