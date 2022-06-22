import enum
from typing import Any
from typing import Optional

from sqlalchemy import JSON
from sqlalchemy import Boolean
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import Enum
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import relationship

from app import activitypub as ap
from app.actor import LOCAL_ACTOR
from app.actor import Actor as BaseActor
from app.ap_object import Object as BaseObject
from app.database import Base
from app.database import now


class Actor(Base, BaseActor):
    __tablename__ = "actors"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now)

    ap_id = Column(String, unique=True, nullable=False, index=True)
    ap_actor: Mapped[ap.RawObject] = Column(JSON, nullable=False)
    ap_type = Column(String, nullable=False)

    handle = Column(String, nullable=True, index=True)

    @property
    def is_from_db(self) -> bool:
        return True


class InboxObject(Base, BaseObject):
    __tablename__ = "inbox"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now)

    actor_id = Column(Integer, ForeignKey("actors.id"), nullable=False)
    actor: Mapped[Actor] = relationship(Actor, uselist=False)

    server = Column(String, nullable=False)

    is_hidden_from_stream = Column(Boolean, nullable=False, default=False)

    ap_actor_id = Column(String, nullable=False)
    ap_type = Column(String, nullable=False)
    ap_id = Column(String, nullable=False, unique=True, index=True)
    ap_context = Column(String, nullable=True)
    ap_published_at = Column(DateTime(timezone=True), nullable=False)
    ap_object: Mapped[ap.RawObject] = Column(JSON, nullable=False)

    activity_object_ap_id = Column(String, nullable=True)

    visibility = Column(Enum(ap.VisibilityEnum), nullable=False)

    # Used for Like, Announce and Undo activities
    relates_to_inbox_object_id = Column(
        Integer,
        ForeignKey("inbox.id"),
        nullable=True,
    )
    relates_to_inbox_object: Mapped[Optional["InboxObject"]] = relationship(
        "InboxObject",
        foreign_keys=relates_to_inbox_object_id,
        remote_side=id,
        uselist=False,
    )
    relates_to_outbox_object_id = Column(
        Integer,
        ForeignKey("outbox.id"),
        nullable=True,
    )
    relates_to_outbox_object: Mapped[Optional["OutboxObject"]] = relationship(
        "OutboxObject",
        foreign_keys=[relates_to_outbox_object_id],
        uselist=False,
    )

    undone_by_inbox_object_id = Column(Integer, ForeignKey("inbox.id"), nullable=True)

    # Link the oubox AP ID to allow undo without any extra query
    liked_via_outbox_object_ap_id = Column(String, nullable=True)
    announced_via_outbox_object_ap_id = Column(String, nullable=True)

    is_bookmarked = Column(Boolean, nullable=False, default=False)

    # FIXME(ts): do we need this?
    has_replies = Column(Boolean, nullable=False, default=False)

    og_meta: Mapped[list[dict[str, Any]] | None] = Column(JSON, nullable=True)


class OutboxObject(Base, BaseObject):
    __tablename__ = "outbox"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now)

    is_hidden_from_homepage = Column(Boolean, nullable=False, default=False)

    public_id = Column(String, nullable=False, index=True)

    ap_type = Column(String, nullable=False)
    ap_id = Column(String, nullable=False, unique=True, index=True)
    ap_context = Column(String, nullable=True)
    ap_object: Mapped[ap.RawObject] = Column(JSON, nullable=False)

    activity_object_ap_id = Column(String, nullable=True)

    # Source content for activities (like Notes)
    source = Column(String, nullable=True)

    ap_published_at = Column(DateTime(timezone=True), nullable=False, default=now)
    visibility = Column(Enum(ap.VisibilityEnum), nullable=False)

    likes_count = Column(Integer, nullable=False, default=0)
    announces_count = Column(Integer, nullable=False, default=0)
    replies_count = Column(Integer, nullable=False, default=0)

    webmentions = Column(JSON, nullable=True)

    og_meta: Mapped[list[dict[str, Any]] | None] = Column(JSON, nullable=True)

    # Never actually delete from the outbox
    is_deleted = Column(Boolean, nullable=False, default=False)

    # Used for Like, Announce and Undo activities
    relates_to_inbox_object_id = Column(
        Integer,
        ForeignKey("inbox.id"),
        nullable=True,
    )
    relates_to_inbox_object: Mapped[Optional["InboxObject"]] = relationship(
        "InboxObject",
        foreign_keys=[relates_to_inbox_object_id],
        uselist=False,
    )
    relates_to_outbox_object_id = Column(
        Integer,
        ForeignKey("outbox.id"),
        nullable=True,
    )
    relates_to_outbox_object: Mapped[Optional["OutboxObject"]] = relationship(
        "OutboxObject",
        foreign_keys=[relates_to_outbox_object_id],
        remote_side=id,
        uselist=False,
    )

    undone_by_outbox_object_id = Column(Integer, ForeignKey("outbox.id"), nullable=True)

    @property
    def actor(self) -> BaseActor:
        return LOCAL_ACTOR


class Follower(Base):
    __tablename__ = "followers"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now)

    actor_id = Column(Integer, ForeignKey("actors.id"), nullable=False, unique=True)
    actor = relationship(Actor, uselist=False)

    inbox_object_id = Column(Integer, ForeignKey("inbox.id"), nullable=False)
    inbox_object = relationship(InboxObject, uselist=False)

    ap_actor_id = Column(String, nullable=False, unique=True)


class Following(Base):
    __tablename__ = "following"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now)

    actor_id = Column(Integer, ForeignKey("actors.id"), nullable=False, unique=True)
    actor = relationship(Actor, uselist=False)

    outbox_object_id = Column(Integer, ForeignKey("outbox.id"), nullable=False)
    outbox_object = relationship(OutboxObject, uselist=False)

    ap_actor_id = Column(String, nullable=False, unique=True)


@enum.unique
class NotificationType(str, enum.Enum):
    NEW_FOLLOWER = "new_follower"
    UNFOLLOW = "unfollow"
    LIKE = "like"
    UNDO_LIKE = "undo_like"
    ANNOUNCE = "announce"
    UNDO_ANNOUNCE = "undo_announce"

    # TODO:
    MENTION = "mention"


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    notification_type = Column(Enum(NotificationType), nullable=True)
    is_new = Column(Boolean, nullable=False, default=True)

    actor_id = Column(Integer, ForeignKey("actors.id"), nullable=True)
    actor = relationship(Actor, uselist=False)

    outbox_object_id = Column(Integer, ForeignKey("outbox.id"), nullable=True)
    outbox_object = relationship(OutboxObject, uselist=False)

    inbox_object_id = Column(Integer, ForeignKey("inbox.id"), nullable=True)
    inbox_object = relationship(InboxObject, uselist=False)


class OutgoingActivity(Base):
    __tablename__ = "outgoing_activities"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)

    recipient = Column(String, nullable=False)
    outbox_object_id = Column(Integer, ForeignKey("outbox.id"), nullable=False)
    outbox_object = relationship(OutboxObject, uselist=False)

    tries = Column(Integer, nullable=False, default=0)
    next_try = Column(DateTime(timezone=True), nullable=True, default=now)

    last_try = Column(DateTime(timezone=True), nullable=True)
    last_status_code = Column(Integer, nullable=True)
    last_response = Column(String, nullable=True)

    is_sent = Column(Boolean, nullable=False, default=False)
    is_errored = Column(Boolean, nullable=False, default=False)
    error = Column(String, nullable=True)


class TaggedOutboxObject(Base):
    __tablename__ = "tagged_outbox_objects"
    __table_args__ = (
        UniqueConstraint("outbox_object_id", "tag", name="uix_tagged_object"),
    )

    id = Column(Integer, primary_key=True, index=True)

    outbox_object_id = Column(Integer, ForeignKey("outbox.id"), nullable=False)
    outbox_object = relationship(OutboxObject, uselist=False)

    tag = Column(String, nullable=False, index=True)


"""
class Upload(Base):
    __tablename__ = "upload"

    filename = Column(String, nullable=False)
    filehash = Column(String, nullable=False)
    filesize = Column(Integer, nullable=False)


class OutboxObjectAttachment(Base):
    __tablename__ = "outbox_object_attachment"

    id = Column(Integer, primary_key=True, index=True)

    outbox_object_id = Column(Integer, ForeignKey("outbox.id"), nullable=False)
    outbox_object = relationship(OutboxObject, uselist=False)

    upload_id = Column(Integer, ForeignKey("upload.id"))
    upload = relationship(Upload, uselist=False)
"""