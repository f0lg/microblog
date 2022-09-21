"""Actions related to the AP inbox/outbox."""
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlparse

import fastapi
import httpx
from loguru import logger
from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from app import activitypub as ap
from app import config
from app import ldsig
from app import models
from app.actor import LOCAL_ACTOR
from app.actor import Actor
from app.actor import RemoteActor
from app.actor import fetch_actor
from app.actor import save_actor
from app.ap_object import RemoteObject
from app.config import BASE_URL
from app.config import BLOCKED_SERVERS
from app.config import ID
from app.config import MANUALLY_APPROVES_FOLLOWERS
from app.config import set_moved_to
from app.database import AsyncSession
from app.outgoing_activities import new_outgoing_activity
from app.source import markdownify
from app.uploads import upload_to_attachment
from app.utils import opengraph
from app.utils import webmentions
from app.utils.datetime import as_utc
from app.utils.datetime import now
from app.utils.datetime import parse_isoformat

AnyboxObject = models.InboxObject | models.OutboxObject


def allocate_outbox_id() -> str:
    return uuid.uuid4().hex


def outbox_object_id(outbox_id) -> str:
    return f"{BASE_URL}/o/{outbox_id}"


async def save_outbox_object(
    db_session: AsyncSession,
    public_id: str,
    raw_object: ap.RawObject,
    relates_to_inbox_object_id: int | None = None,
    relates_to_outbox_object_id: int | None = None,
    relates_to_actor_id: int | None = None,
    source: str | None = None,
    is_transient: bool = False,
    conversation: str | None = None,
) -> models.OutboxObject:
    ro = await RemoteObject.from_raw_object(raw_object)

    outbox_object = models.OutboxObject(
        public_id=public_id,
        ap_type=ro.ap_type,
        ap_id=ro.ap_id,
        ap_context=ro.ap_context,
        ap_object=ro.ap_object,
        visibility=ro.visibility,
        og_meta=await opengraph.og_meta_from_note(db_session, ro),
        relates_to_inbox_object_id=relates_to_inbox_object_id,
        relates_to_outbox_object_id=relates_to_outbox_object_id,
        relates_to_actor_id=relates_to_actor_id,
        activity_object_ap_id=ro.activity_object_ap_id,
        is_hidden_from_homepage=True if ro.in_reply_to else False,
        source=source,
        is_transient=is_transient,
        conversation=conversation,
    )
    db_session.add(outbox_object)
    await db_session.flush()
    await db_session.refresh(outbox_object)

    return outbox_object


async def send_delete(db_session: AsyncSession, ap_object_id: str) -> None:
    outbox_object_to_delete = await get_outbox_object_by_ap_id(db_session, ap_object_id)
    if not outbox_object_to_delete:
        raise ValueError(f"{ap_object_id} not found in the outbox")

    delete_id = allocate_outbox_id()
    # FIXME addressing
    delete = {
        "@context": ap.AS_EXTENDED_CTX,
        "id": outbox_object_id(delete_id),
        "type": "Delete",
        "actor": ID,
        "object": {
            "type": "Tombstone",
            "id": ap_object_id,
        },
    }
    outbox_object = await save_outbox_object(
        db_session,
        delete_id,
        delete,
        relates_to_outbox_object_id=outbox_object_to_delete.id,
    )
    if not outbox_object.id:
        raise ValueError("Should never happen")

    outbox_object_to_delete.is_deleted = True
    await db_session.commit()

    # Compute the original recipients
    recipients = await _compute_recipients(
        db_session, outbox_object_to_delete.ap_object
    )
    for rcp in recipients:
        await new_outgoing_activity(db_session, rcp, outbox_object.id)

    # Revert side effects
    if outbox_object_to_delete.in_reply_to:
        replied_object = await get_anybox_object_by_ap_id(
            db_session, outbox_object_to_delete.in_reply_to
        )
        if replied_object:
            new_replies_count = await _get_replies_count(
                db_session, replied_object.ap_id
            )

            replied_object.replies_count = new_replies_count
            if replied_object.replies_count < 0:
                logger.warning("negative replies count for {replied_object.ap_id}")
                replied_object.replies_count = 0
        else:
            logger.info(f"{outbox_object_to_delete.in_reply_to} not found")

    await db_session.commit()


async def send_like(db_session: AsyncSession, ap_object_id: str) -> None:
    inbox_object = await get_inbox_object_by_ap_id(db_session, ap_object_id)
    if not inbox_object:
        logger.info(f"Saving unknwown object {ap_object_id}")
        raw_object = await ap.fetch(ap.get_id(ap_object_id))
        await save_object_to_inbox(db_session, raw_object)
        await db_session.commit()
        # XXX: we need to reload it as lazy-loading the actor will fail
        # (asyncio SQLAlchemy issue)
        inbox_object = await get_inbox_object_by_ap_id(db_session, ap_object_id)
        if not inbox_object:
            raise ValueError("Should never happen")

    like_id = allocate_outbox_id()
    like = {
        "@context": ap.AS_CTX,
        "id": outbox_object_id(like_id),
        "type": "Like",
        "actor": ID,
        "object": ap_object_id,
    }
    outbox_object = await save_outbox_object(
        db_session, like_id, like, relates_to_inbox_object_id=inbox_object.id
    )
    if not outbox_object.id:
        raise ValueError("Should never happen")

    inbox_object.liked_via_outbox_object_ap_id = outbox_object.ap_id

    await new_outgoing_activity(
        db_session, inbox_object.actor.inbox_url, outbox_object.id
    )
    await db_session.commit()


async def send_announce(db_session: AsyncSession, ap_object_id: str) -> None:
    inbox_object = await get_inbox_object_by_ap_id(db_session, ap_object_id)
    if not inbox_object:
        logger.info(f"Saving unknwown object {ap_object_id}")
        raw_object = await ap.fetch(ap.get_id(ap_object_id))
        await save_object_to_inbox(db_session, raw_object)
        await db_session.commit()
        # XXX: we need to reload it as lazy-loading the actor will fail
        # (asyncio SQLAlchemy issue)
        inbox_object = await get_inbox_object_by_ap_id(db_session, ap_object_id)
        if not inbox_object:
            raise ValueError("Should never happen")

    if inbox_object.visibility not in [
        ap.VisibilityEnum.PUBLIC,
        ap.VisibilityEnum.UNLISTED,
    ]:
        raise ValueError("Cannot announce non-public object")

    announce_id = allocate_outbox_id()
    announce = {
        "@context": ap.AS_CTX,
        "id": outbox_object_id(announce_id),
        "type": "Announce",
        "actor": ID,
        "object": ap_object_id,
        "to": [ap.AS_PUBLIC],
        "cc": [
            f"{BASE_URL}/followers",
            inbox_object.ap_actor_id,
        ],
    }
    outbox_object = await save_outbox_object(
        db_session, announce_id, announce, relates_to_inbox_object_id=inbox_object.id
    )
    if not outbox_object.id:
        raise ValueError("Should never happen")

    inbox_object.announced_via_outbox_object_ap_id = outbox_object.ap_id

    recipients = await _compute_recipients(db_session, announce)
    for rcp in recipients:
        await new_outgoing_activity(db_session, rcp, outbox_object.id)

    await db_session.commit()


async def send_follow(db_session: AsyncSession, ap_actor_id: str) -> None:
    await _send_follow(db_session, ap_actor_id)
    await db_session.commit()


async def _send_follow(db_session: AsyncSession, ap_actor_id: str) -> None:
    actor = await fetch_actor(db_session, ap_actor_id)

    follow_id = allocate_outbox_id()
    follow = {
        "@context": ap.AS_CTX,
        "id": outbox_object_id(follow_id),
        "type": "Follow",
        "actor": ID,
        "object": ap_actor_id,
    }

    outbox_object = await save_outbox_object(
        db_session, follow_id, follow, relates_to_actor_id=actor.id
    )
    if not outbox_object.id:
        raise ValueError("Should never happen")

    await new_outgoing_activity(db_session, actor.inbox_url, outbox_object.id)

    # Caller should commit


async def send_undo(db_session: AsyncSession, ap_object_id: str) -> None:
    await _send_undo(db_session, ap_object_id)
    await db_session.commit()


async def _send_undo(db_session: AsyncSession, ap_object_id: str) -> None:
    outbox_object_to_undo = await get_outbox_object_by_ap_id(db_session, ap_object_id)
    if not outbox_object_to_undo:
        raise ValueError(f"{ap_object_id} not found in the outbox")

    if outbox_object_to_undo.ap_type not in ["Follow", "Like", "Announce"]:
        raise ValueError(
            f"Cannot build Undo for {outbox_object_to_undo.ap_type} activity"
        )

    undo_id = allocate_outbox_id()
    undo = {
        "@context": ap.AS_CTX,
        "id": outbox_object_id(undo_id),
        "type": "Undo",
        "actor": ID,
        "object": ap.remove_context(outbox_object_to_undo.ap_object),
    }

    outbox_object = await save_outbox_object(
        db_session,
        undo_id,
        undo,
        relates_to_outbox_object_id=outbox_object_to_undo.id,
    )
    if not outbox_object.id:
        raise ValueError("Should never happen")

    outbox_object_to_undo.undone_by_outbox_object_id = outbox_object.id

    if outbox_object_to_undo.ap_type == "Follow":
        if not outbox_object_to_undo.activity_object_ap_id:
            raise ValueError("Should never happen")
        followed_actor = await fetch_actor(
            db_session, outbox_object_to_undo.activity_object_ap_id
        )
        await new_outgoing_activity(
            db_session,
            followed_actor.inbox_url,
            outbox_object.id,
        )
        # Also remove the follow from the following collection
        await db_session.execute(
            delete(models.Following).where(
                models.Following.ap_actor_id == followed_actor.ap_id
            )
        )
    elif outbox_object_to_undo.ap_type == "Like":
        liked_object_ap_id = outbox_object_to_undo.activity_object_ap_id
        if not liked_object_ap_id:
            raise ValueError("Should never happen")
        liked_object = await get_inbox_object_by_ap_id(db_session, liked_object_ap_id)
        if not liked_object:
            raise ValueError(f"Cannot find liked object {liked_object_ap_id}")
        liked_object.liked_via_outbox_object_ap_id = None

        # Send the Undo to the liked object's actor
        await new_outgoing_activity(
            db_session,
            liked_object.actor.inbox_url,  # type: ignore
            outbox_object.id,
        )
    elif outbox_object_to_undo.ap_type == "Announce":
        announced_object_ap_id = outbox_object_to_undo.activity_object_ap_id
        if not announced_object_ap_id:
            raise ValueError("Should never happen")
        announced_object = await get_inbox_object_by_ap_id(
            db_session, announced_object_ap_id
        )
        if not announced_object:
            raise ValueError(f"Cannot find announced object {announced_object_ap_id}")
        announced_object.announced_via_outbox_object_ap_id = None

        # Send the Undo to the original recipients
        recipients = await _compute_recipients(db_session, outbox_object.ap_object)
        for rcp in recipients:
            await new_outgoing_activity(db_session, rcp, outbox_object.id)
    else:
        raise ValueError("Should never happen")

    # called should commit


async def fetch_conversation_root(
    db_session: AsyncSession,
    obj: AnyboxObject | RemoteObject,
    is_root: bool = False,
) -> str:
    """Some softwares do not set the context/conversation field (like Misskey).
    This means we have to track conversation ourselves. To do so, we fetch
    the root of the conversation and either:
     - use the context field if set
     - or build a custom conversation ID
    """
    if not obj.in_reply_to or is_root:
        if obj.ap_context:
            return obj.ap_context
        else:
            # Use the root AP ID if there'no context
            return f"microblogpub:root:{obj.ap_id}"
    else:
        in_reply_to_object: AnyboxObject | RemoteObject | None = (
            await get_anybox_object_by_ap_id(db_session, obj.in_reply_to)
        )
        if not in_reply_to_object:
            try:
                raw_reply = await ap.fetch(ap.get_id(obj.in_reply_to))
                raw_reply_actor = await fetch_actor(
                    db_session, ap.get_actor_id(raw_reply)
                )
                in_reply_to_object = RemoteObject(raw_reply, actor=raw_reply_actor)
            except (
                ap.ObjectNotFoundError,
                ap.ObjectIsGoneError,
                ap.NotAnObjectError,
                ap.ObjectUnavailableError,
            ):
                return await fetch_conversation_root(db_session, obj, is_root=True)
            except httpx.HTTPStatusError as http_status_error:
                if 400 <= http_status_error.response.status_code < 500:
                    # We may not have access, in this case consider if root
                    return await fetch_conversation_root(db_session, obj, is_root=True)
                else:
                    raise

        return await fetch_conversation_root(db_session, in_reply_to_object)


async def send_move(
    db_session: AsyncSession,
    target: str,
) -> None:
    move_id = allocate_outbox_id()
    obj = {
        "@context": ap.AS_CTX,
        "type": "Move",
        "id": outbox_object_id(move_id),
        "actor": LOCAL_ACTOR.ap_id,
        "object": LOCAL_ACTOR.ap_id,
        "target": target,
    }

    outbox_object = await save_outbox_object(db_session, move_id, obj)
    if not outbox_object.id:
        raise ValueError("Should never happen")

    recipients = await _get_followers_recipients(db_session)
    for rcp in recipients:
        await new_outgoing_activity(db_session, rcp, outbox_object.id)

    # Store the moved to in order to update the profile
    set_moved_to(target)

    await db_session.commit()


async def send_self_destruct(db_session: AsyncSession) -> None:
    delete_id = allocate_outbox_id()
    delete = {
        "@context": ap.AS_EXTENDED_CTX,
        "id": outbox_object_id(delete_id),
        "type": "Delete",
        "actor": ID,
        "object": ID,
        "to": [ap.AS_PUBLIC],
    }
    outbox_object = await save_outbox_object(
        db_session,
        delete_id,
        delete,
    )
    if not outbox_object.id:
        raise ValueError("Should never happen")

    recipients = await compute_all_known_recipients(db_session)
    for rcp in recipients:
        await new_outgoing_activity(db_session, rcp, outbox_object.id)

    await db_session.commit()


async def send_create(
    db_session: AsyncSession,
    ap_type: str,
    source: str,
    uploads: list[tuple[models.Upload, str, str | None]],
    in_reply_to: str | None,
    visibility: ap.VisibilityEnum,
    content_warning: str | None = None,
    is_sensitive: bool = False,
    poll_type: str | None = None,
    poll_answers: list[str] | None = None,
    poll_duration_in_minutes: int | None = None,
    name: str | None = None,
) -> str:
    note_id = allocate_outbox_id()
    published = now().replace(microsecond=0).isoformat().replace("+00:00", "Z")
    context = f"{ID}/contexts/" + uuid.uuid4().hex
    conversation = context
    content, tags, mentioned_actors = await markdownify(db_session, source)
    attachments = []

    in_reply_to_object: AnyboxObject | None = None
    if in_reply_to:
        in_reply_to_object = await get_anybox_object_by_ap_id(db_session, in_reply_to)
        if not in_reply_to_object:
            raise ValueError(f"Invalid in reply to {in_reply_to=}")
        if not in_reply_to_object.ap_context:
            logger.warning(f"Replied object {in_reply_to} has no context")
            try:
                conversation = await fetch_conversation_root(
                    db_session,
                    in_reply_to_object,
                )
            except Exception:
                logger.exception(f"Failed to fetch convo root {in_reply_to}")
        else:
            context = in_reply_to_object.ap_context
            conversation = in_reply_to_object.ap_context

    for (upload, filename, alt_text) in uploads:
        attachments.append(upload_to_attachment(upload, filename, alt_text))

    to = []
    cc = []
    mentioned_actor_ap_ids = [actor.ap_id for actor in mentioned_actors]
    if visibility == ap.VisibilityEnum.PUBLIC:
        to = [ap.AS_PUBLIC]
        cc = [f"{BASE_URL}/followers"] + mentioned_actor_ap_ids
    elif visibility == ap.VisibilityEnum.UNLISTED:
        to = [f"{BASE_URL}/followers"]
        cc = [ap.AS_PUBLIC] + mentioned_actor_ap_ids
    elif visibility == ap.VisibilityEnum.FOLLOWERS_ONLY:
        to = [f"{BASE_URL}/followers"]
        cc = mentioned_actor_ap_ids
    elif visibility == ap.VisibilityEnum.DIRECT:
        to = mentioned_actor_ap_ids
        cc = []
    else:
        raise ValueError(f"Unhandled visibility {visibility}")

    extra_obj_attrs = {}
    if ap_type == "Question":
        if not poll_answers or len(poll_answers) < 2:
            raise ValueError("Question must have at least 2 possible answers")

        if not poll_type:
            raise ValueError("Mising poll_type")

        if not poll_duration_in_minutes:
            raise ValueError("Missing poll_duration_in_minutes")

        extra_obj_attrs = {
            "votersCount": 0,
            "endTime": (now() + timedelta(minutes=poll_duration_in_minutes))
            .isoformat()
            .replace("+00:00", "Z"),
            poll_type: [
                {
                    "type": "Note",
                    "name": answer,
                    "replies": {"type": "Collection", "totalItems": 0},
                }
                for answer in poll_answers
            ],
        }
    elif ap_type == "Article":
        if not name:
            raise ValueError("Article must have a name")

        extra_obj_attrs = {"name": name}

    obj = {
        "@context": ap.AS_EXTENDED_CTX,
        "type": ap_type,
        "id": outbox_object_id(note_id),
        "attributedTo": ID,
        "content": content,
        "to": to,
        "cc": cc,
        "published": published,
        "context": context,
        "conversation": context,
        "url": outbox_object_id(note_id),
        "tag": tags,
        "summary": content_warning,
        "inReplyTo": in_reply_to,
        "sensitive": is_sensitive,
        "attachment": attachments,
        **extra_obj_attrs,  # type: ignore
    }
    outbox_object = await save_outbox_object(
        db_session,
        note_id,
        obj,
        source=source,
        conversation=conversation,
    )
    if not outbox_object.id:
        raise ValueError("Should never happen")

    for tag in tags:
        if tag["type"] == "Hashtag":
            tagged_object = models.TaggedOutboxObject(
                tag=tag["name"][1:],
                outbox_object_id=outbox_object.id,
            )
            db_session.add(tagged_object)

    for (upload, filename, alt) in uploads:
        outbox_object_attachment = models.OutboxObjectAttachment(
            filename=filename,
            alt=alt,
            outbox_object_id=outbox_object.id,
            upload_id=upload.id,
        )
        db_session.add(outbox_object_attachment)

    recipients = await _compute_recipients(db_session, obj)
    for rcp in recipients:
        await new_outgoing_activity(db_session, rcp, outbox_object.id)

    # If the note is public, check if we need to send any webmentions
    if visibility == ap.VisibilityEnum.PUBLIC:
        possible_targets = await opengraph.external_urls(db_session, outbox_object)
        logger.info(f"webmentions possible targert {possible_targets}")
        for target in possible_targets:
            webmention_endpoint = await webmentions.discover_webmention_endpoint(target)
            logger.info(f"{target=} {webmention_endpoint=}")
            if webmention_endpoint:
                await new_outgoing_activity(
                    db_session,
                    webmention_endpoint,
                    outbox_object_id=outbox_object.id,
                    webmention_target=target,
                )

    await db_session.commit()

    # Refresh the replies counter if needed
    if in_reply_to_object:
        new_replies_count = await _get_replies_count(
            db_session, in_reply_to_object.ap_id
        )
        if in_reply_to_object.is_from_outbox:
            await db_session.execute(
                update(models.OutboxObject)
                .where(
                    models.OutboxObject.ap_id == in_reply_to_object.ap_id,
                )
                .values(replies_count=new_replies_count)
            )
        elif in_reply_to_object.is_from_inbox:
            await db_session.execute(
                update(models.InboxObject)
                .where(
                    models.InboxObject.ap_id == in_reply_to_object.ap_id,
                )
                .values(replies_count=new_replies_count)
            )

    await db_session.commit()

    return note_id


async def send_vote(
    db_session: AsyncSession,
    in_reply_to: str,
    names: list[str],
) -> str:
    logger.info(f"Send vote {names}")
    published = now().replace(microsecond=0).isoformat().replace("+00:00", "Z")

    in_reply_to_object = await get_inbox_object_by_ap_id(db_session, in_reply_to)
    if not in_reply_to_object:
        raise ValueError(f"Invalid in reply to {in_reply_to=}")
    if not in_reply_to_object.ap_context:
        raise ValueError("Object has no context")
    context = in_reply_to_object.ap_context

    # TODO: ensure the name are valid?

    # Save the answers
    in_reply_to_object.voted_for_answers = names

    to = [in_reply_to_object.actor.ap_id]

    for name in names:
        vote_id = allocate_outbox_id()
        note = {
            "@context": ap.AS_EXTENDED_CTX,
            "type": "Note",
            "id": outbox_object_id(vote_id),
            "attributedTo": ID,
            "name": name,
            "to": to,
            "cc": [],
            "published": published,
            "context": context,
            "conversation": context,
            "url": outbox_object_id(vote_id),
            "inReplyTo": in_reply_to,
        }
        outbox_object = await save_outbox_object(
            db_session, vote_id, note, is_transient=True
        )
        if not outbox_object.id:
            raise ValueError("Should never happen")

        recipients = await _compute_recipients(db_session, note)
        for rcp in recipients:
            await new_outgoing_activity(db_session, rcp, outbox_object.id)

    await db_session.commit()
    return vote_id


async def send_update(
    db_session: AsyncSession,
    ap_id: str,
    source: str,
) -> str:
    outbox_object = await get_outbox_object_by_ap_id(db_session, ap_id)
    if not outbox_object:
        raise ValueError(f"{ap_id} not found")

    revisions = outbox_object.revisions or []
    revisions.append(
        {
            "ap_object": outbox_object.ap_object,
            "source": outbox_object.source,
            "updated": (
                outbox_object.ap_object.get("updated")
                or outbox_object.ap_object.get("published")
            ),
        }
    )

    updated = now().replace(microsecond=0).isoformat().replace("+00:00", "Z")
    content, tags, mentioned_actors = await markdownify(db_session, source)

    note = {
        "@context": ap.AS_EXTENDED_CTX,
        "type": outbox_object.ap_type,
        "id": outbox_object.ap_id,
        "attributedTo": ID,
        "content": content,
        "to": outbox_object.ap_object["to"],
        "cc": outbox_object.ap_object["cc"],
        "published": outbox_object.ap_object["published"],
        "context": outbox_object.ap_context,
        "conversation": outbox_object.ap_context,
        "url": outbox_object.url,
        "tag": tags,
        "summary": outbox_object.summary,
        "inReplyTo": outbox_object.in_reply_to,
        "sensitive": outbox_object.sensitive,
        "attachment": outbox_object.ap_object["attachment"],
        "updated": updated,
    }

    outbox_object.ap_object = note
    outbox_object.source = source
    outbox_object.revisions = revisions

    recipients = await _compute_recipients(db_session, note)
    for rcp in recipients:
        await new_outgoing_activity(db_session, rcp, outbox_object.id)

    # If the note is public, check if we need to send any webmentions
    if outbox_object.visibility == ap.VisibilityEnum.PUBLIC:

        possible_targets = await opengraph.external_urls(db_session, outbox_object)
        logger.info(f"webmentions possible targert {possible_targets}")
        for target in possible_targets:
            webmention_endpoint = await webmentions.discover_webmention_endpoint(target)
            logger.info(f"{target=} {webmention_endpoint=}")
            if webmention_endpoint:
                await new_outgoing_activity(
                    db_session,
                    webmention_endpoint,
                    outbox_object_id=outbox_object.id,
                    webmention_target=target,
                )

    await db_session.commit()
    return outbox_object.public_id  # type: ignore


async def _compute_recipients(
    db_session: AsyncSession, ap_object: ap.RawObject
) -> set[str]:
    _recipients = []
    for field in ["to", "cc", "bto", "bcc"]:
        if field in ap_object:
            _recipients.extend(ap.as_list(ap_object[field]))

    recipients = set()
    logger.info(f"{_recipients}")
    for r in _recipients:
        if r in [ap.AS_PUBLIC, ID]:
            continue

        # If we got a local collection, assume it's a collection of actors
        if r.startswith(BASE_URL):
            for actor in await fetch_actor_collection(db_session, r):
                recipients.add(actor.shared_inbox_url)

            continue

        # Is it a known actor?
        known_actor = (
            await db_session.execute(
                select(models.Actor).where(models.Actor.ap_id == r)
            )
        ).scalar_one_or_none()  # type: ignore
        if known_actor:
            recipients.add(known_actor.shared_inbox_url)
            continue

        # Fetch the object
        raw_object = await ap.fetch(r)
        if raw_object.get("type") in ap.ACTOR_TYPES:
            saved_actor = await save_actor(db_session, raw_object)
            recipients.add(saved_actor.shared_inbox_url)
        else:
            # Assume it's a collection of actors
            for raw_actor in await ap.parse_collection(payload=raw_object):
                actor = RemoteActor(raw_actor)
                recipients.add(actor.shared_inbox_url)

    return recipients


async def compute_all_known_recipients(db_session: AsyncSession) -> set[str]:
    return {
        actor.shared_inbox_url or actor.inbox_url
        for actor in (
            await db_session.scalars(
                select(models.Actor).where(models.Actor.is_deleted.is_(False))
            )
        ).all()
    }


async def _get_following(db_session: AsyncSession) -> list[models.Follower]:
    return (
        (
            await db_session.scalars(
                select(models.Following).options(joinedload(models.Following.actor))
            )
        )
        .unique()
        .all()
    )


async def _get_followers(db_session: AsyncSession) -> list[models.Follower]:
    return (
        (
            await db_session.scalars(
                select(models.Follower).options(joinedload(models.Follower.actor))
            )
        )
        .unique()
        .all()
    )


async def _get_followers_recipients(
    db_session: AsyncSession,
    skip_actors: list[models.Actor] | None = None,
) -> set[str]:
    """Returns all the recipients from the local follower collection."""
    actor_ap_ids_to_skip = []
    if skip_actors:
        actor_ap_ids_to_skip = [actor.ap_id for actor in skip_actors]

    followers = await _get_followers(db_session)
    return {
        follower.actor.shared_inbox_url  # type: ignore
        for follower in followers
        if follower.actor.ap_id not in actor_ap_ids_to_skip
    }


async def get_notification_by_id(
    db_session: AsyncSession, notification_id: int
) -> models.Notification | None:
    return (
        await db_session.execute(
            select(models.Notification)
            .where(models.Notification.id == notification_id)
            .options(
                joinedload(models.Notification.inbox_object).options(
                    joinedload(models.InboxObject.actor)
                ),
            )
        )
    ).scalar_one_or_none()  # type: ignore


async def get_inbox_object_by_ap_id(
    db_session: AsyncSession, ap_id: str
) -> models.InboxObject | None:
    return (
        await db_session.execute(
            select(models.InboxObject)
            .where(models.InboxObject.ap_id == ap_id)
            .options(
                joinedload(models.InboxObject.actor),
                joinedload(models.InboxObject.relates_to_inbox_object),
                joinedload(models.InboxObject.relates_to_outbox_object),
            )
        )
    ).scalar_one_or_none()  # type: ignore


async def get_inbox_delete_for_activity_object_ap_id(
    db_session: AsyncSession, activity_object_ap_id: str
) -> models.InboxObject | None:
    return (
        await db_session.execute(
            select(models.InboxObject)
            .where(
                models.InboxObject.ap_type == "Delete",
                models.InboxObject.activity_object_ap_id == activity_object_ap_id,
            )
            .options(
                joinedload(models.InboxObject.actor),
                joinedload(models.InboxObject.relates_to_inbox_object),
                joinedload(models.InboxObject.relates_to_outbox_object),
            )
        )
    ).scalar_one_or_none()  # type: ignore


async def get_outbox_object_by_ap_id(
    db_session: AsyncSession, ap_id: str
) -> models.OutboxObject | None:
    return (
        (
            await db_session.execute(
                select(models.OutboxObject)
                .where(models.OutboxObject.ap_id == ap_id)
                .options(
                    joinedload(models.OutboxObject.outbox_object_attachments).options(
                        joinedload(models.OutboxObjectAttachment.upload)
                    ),
                    joinedload(models.OutboxObject.relates_to_inbox_object).options(
                        joinedload(models.InboxObject.actor),
                    ),
                    joinedload(models.OutboxObject.relates_to_outbox_object).options(
                        joinedload(
                            models.OutboxObject.outbox_object_attachments
                        ).options(joinedload(models.OutboxObjectAttachment.upload)),
                    ),
                )
            )
        )
        .unique()
        .scalar_one_or_none()
    )  # type: ignore


async def get_anybox_object_by_ap_id(
    db_session: AsyncSession, ap_id: str
) -> AnyboxObject | None:
    if ap_id.startswith(BASE_URL):
        return await get_outbox_object_by_ap_id(db_session, ap_id)
    else:
        return await get_inbox_object_by_ap_id(db_session, ap_id)


async def _handle_delete_activity(
    db_session: AsyncSession,
    from_actor: models.Actor,
    delete_activity: models.InboxObject,
    relates_to_inbox_object: models.InboxObject | None,
    forwarded_by_actor: models.Actor | None,
) -> None:
    ap_object_to_delete: models.InboxObject | models.Actor | None = None
    if relates_to_inbox_object:
        ap_object_to_delete = relates_to_inbox_object
    elif delete_activity.activity_object_ap_id:
        # If it's not a Delete for an inbox object, it may be related to
        # an actor
        try:
            ap_object_to_delete = await fetch_actor(
                db_session,
                delete_activity.activity_object_ap_id,
                save_if_not_found=False,
            )
        except ap.ObjectNotFoundError:
            pass

    if ap_object_to_delete is None or not ap_object_to_delete.is_from_db:
        logger.info(
            "Received Delete for an unknown object "
            f"{delete_activity.activity_object_ap_id}"
        )
        return

    if isinstance(ap_object_to_delete, models.InboxObject):
        if from_actor.ap_id != ap_object_to_delete.actor.ap_id:
            logger.warning(
                "Actor mismatch between the activity and the object: "
                f"{from_actor.ap_id}/{ap_object_to_delete.actor.ap_id}"
            )
            return

        logger.info(
            f"Deleting {ap_object_to_delete.ap_type}/{ap_object_to_delete.ap_id}"
        )
        await _revert_side_effect_for_deleted_object(
            db_session,
            delete_activity,
            ap_object_to_delete,
            forwarded_by_actor,
        )
        ap_object_to_delete.is_deleted = True
    elif isinstance(ap_object_to_delete, models.Actor):
        if from_actor.ap_id != ap_object_to_delete.ap_id:
            logger.warning(
                "Actor mismatch between the activity and the object: "
                f"{from_actor.ap_id}/{ap_object_to_delete.ap_id}"
            )
            return

        logger.info(f"Deleting actor {ap_object_to_delete.ap_id}")
        follower = (
            await db_session.scalars(
                select(models.Follower).where(
                    models.Follower.ap_actor_id == ap_object_to_delete.ap_id,
                )
            )
        ).one_or_none()
        if follower:
            logger.info("Removing actor from follower")
            await db_session.delete(follower)

        following = (
            await db_session.scalars(
                select(models.Following).where(
                    models.Following.ap_actor_id == ap_object_to_delete.ap_id,
                )
            )
        ).one_or_none()
        if following:
            logger.info("Removing actor from following")
            await db_session.delete(following)

        # Mark the actor as deleted
        ap_object_to_delete.is_deleted = True

        inbox_objects = (
            await db_session.scalars(
                select(models.InboxObject).where(
                    models.InboxObject.actor_id == ap_object_to_delete.id,
                    models.InboxObject.is_deleted.is_(False),
                )
            )
        ).all()
        logger.info(f"Deleting {len(inbox_objects)} objects")
        for inbox_object in inbox_objects:
            await _revert_side_effect_for_deleted_object(
                db_session,
                delete_activity,
                inbox_object,
                forwarded_by_actor=None,
            )
            inbox_object.is_deleted = True
    else:
        raise ValueError("Should never happen")

    await db_session.flush()


async def _get_replies_count(
    db_session: AsyncSession,
    replied_object_ap_id: str,
) -> int:
    return (
        await db_session.scalar(
            select(func.count(models.InboxObject.id)).where(
                func.json_extract(models.InboxObject.ap_object, "$.inReplyTo")
                == replied_object_ap_id,
                models.InboxObject.is_deleted.is_(False),
            )
        )
    ) + (
        await db_session.scalar(
            select(func.count(models.OutboxObject.id)).where(
                func.json_extract(models.OutboxObject.ap_object, "$.inReplyTo")
                == replied_object_ap_id,
                models.OutboxObject.is_deleted.is_(False),
            )
        )
    )


async def _revert_side_effect_for_deleted_object(
    db_session: AsyncSession,
    delete_activity: models.InboxObject,
    deleted_ap_object: models.InboxObject,
    forwarded_by_actor: models.Actor | None,
) -> None:
    is_delete_needs_to_be_forwarded = False

    # Decrement the replies counter if needed
    if deleted_ap_object.in_reply_to:
        replied_object = await get_anybox_object_by_ap_id(
            db_session,
            deleted_ap_object.in_reply_to,
        )
        if replied_object:
            if replied_object.is_from_outbox:
                # It's a local reply that was likely forwarded, the Delete
                # also needs to be forwarded
                is_delete_needs_to_be_forwarded = True

                new_replies_count = await _get_replies_count(
                    db_session, replied_object.ap_id
                )

                await db_session.execute(
                    update(models.OutboxObject)
                    .where(
                        models.OutboxObject.id == replied_object.id,
                    )
                    .values(replies_count=new_replies_count)
                )
            else:
                new_replies_count = await _get_replies_count(
                    db_session, replied_object.ap_id
                )

                await db_session.execute(
                    update(models.InboxObject)
                    .where(
                        models.InboxObject.id == replied_object.id,
                    )
                    .values(replies_count=new_replies_count)
                )

    if deleted_ap_object.ap_type == "Like" and deleted_ap_object.activity_object_ap_id:
        related_object = await get_outbox_object_by_ap_id(
            db_session,
            deleted_ap_object.activity_object_ap_id,
        )
        if related_object:
            if related_object.is_from_outbox:
                await db_session.execute(
                    update(models.OutboxObject)
                    .where(
                        models.OutboxObject.id == related_object.id,
                    )
                    .values(likes_count=models.OutboxObject.likes_count - 1)
                )
    elif (
        deleted_ap_object.ap_type == "Annouce"
        and deleted_ap_object.activity_object_ap_id
    ):
        related_object = await get_outbox_object_by_ap_id(
            db_session,
            deleted_ap_object.activity_object_ap_id,
        )
        if related_object:
            if related_object.is_from_outbox:
                await db_session.execute(
                    update(models.OutboxObject)
                    .where(
                        models.OutboxObject.id == related_object.id,
                    )
                    .values(announces_count=models.OutboxObject.announces_count - 1)
                )

    # Delete any Like/Announce
    await db_session.execute(
        update(models.OutboxObject)
        .where(
            models.OutboxObject.activity_object_ap_id == deleted_ap_object.ap_id,
        )
        .values(is_deleted=True)
    )

    # If it's a local replies, it was forwarded, so we also need to forward
    # the Delete activity if possible
    if (
        delete_activity.activity_object_ap_id == deleted_ap_object.ap_id
        and delete_activity.has_ld_signature
        and is_delete_needs_to_be_forwarded
    ):
        logger.info("Forwarding Delete activity as it's a local reply")

        # Don't forward to the forwarding actor and the original Delete actor
        skip_actors = [delete_activity.actor]
        if forwarded_by_actor:
            skip_actors.append(forwarded_by_actor)
        recipients = await _get_followers_recipients(
            db_session,
            skip_actors=skip_actors,
        )
        for rcp in recipients:
            await new_outgoing_activity(
                db_session,
                rcp,
                outbox_object_id=None,
                inbox_object_id=delete_activity.id,
            )


async def _handle_follow_follow_activity(
    db_session: AsyncSession,
    from_actor: models.Actor,
    follow_activity: models.InboxObject,
) -> None:
    if follow_activity.activity_object_ap_id != LOCAL_ACTOR.ap_id:
        logger.warning(
            f"Dropping Follow activity for {follow_activity.activity_object_ap_id}"
        )
        await db_session.delete(follow_activity)
        return

    if MANUALLY_APPROVES_FOLLOWERS:
        notif = models.Notification(
            notification_type=models.NotificationType.PENDING_INCOMING_FOLLOWER,
            actor_id=from_actor.id,
            inbox_object_id=follow_activity.id,
        )
        db_session.add(notif)
        return None

    await _send_accept(db_session, from_actor, follow_activity)


async def _get_incoming_follow_from_notification_id(
    db_session: AsyncSession,
    notification_id: int,
) -> tuple[models.Notification, models.InboxObject]:
    notif = await get_notification_by_id(db_session, notification_id)
    if notif is None:
        raise ValueError(f"Notification {notification_id=} not found")

    if notif.inbox_object is None:
        raise ValueError("Should never happen")

    if ap_type := notif.inbox_object.ap_type != "Follow":
        raise ValueError(f"Unexpected {ap_type=}")

    return notif, notif.inbox_object


async def send_accept(
    db_session: AsyncSession,
    notification_id: int,
) -> None:
    notif, incoming_follow_request = await _get_incoming_follow_from_notification_id(
        db_session, notification_id
    )

    await _send_accept(
        db_session, incoming_follow_request.actor, incoming_follow_request
    )
    notif.is_accepted = True

    await db_session.commit()


async def _send_accept(
    db_session: AsyncSession,
    from_actor: models.Actor,
    inbox_object: models.InboxObject,
) -> None:

    follower = models.Follower(
        actor_id=from_actor.id,
        inbox_object_id=inbox_object.id,
        ap_actor_id=from_actor.ap_id,
    )
    try:
        db_session.add(follower)
        await db_session.flush()
    except IntegrityError:
        pass  # TODO update the existing followe

    # Reply with an Accept
    reply_id = allocate_outbox_id()
    reply = {
        "@context": ap.AS_CTX,
        "id": outbox_object_id(reply_id),
        "type": "Accept",
        "actor": ID,
        "object": inbox_object.ap_id,
    }
    outbox_activity = await save_outbox_object(
        db_session, reply_id, reply, relates_to_inbox_object_id=inbox_object.id
    )
    if not outbox_activity.id:
        raise ValueError("Should never happen")
    await new_outgoing_activity(db_session, from_actor.inbox_url, outbox_activity.id)

    notif = models.Notification(
        notification_type=models.NotificationType.NEW_FOLLOWER,
        actor_id=from_actor.id,
    )
    db_session.add(notif)


async def send_reject(
    db_session: AsyncSession,
    notification_id: int,
) -> None:
    notif, incoming_follow_request = await _get_incoming_follow_from_notification_id(
        db_session, notification_id
    )

    await _send_reject(
        db_session, incoming_follow_request.actor, incoming_follow_request
    )
    notif.is_rejected = True
    await db_session.commit()


async def _send_reject(
    db_session: AsyncSession,
    from_actor: models.Actor,
    inbox_object: models.InboxObject,
) -> None:
    # Reply with an Accept
    reply_id = allocate_outbox_id()
    reply = {
        "@context": ap.AS_CTX,
        "id": outbox_object_id(reply_id),
        "type": "Reject",
        "actor": ID,
        "object": inbox_object.ap_id,
    }
    outbox_activity = await save_outbox_object(
        db_session, reply_id, reply, relates_to_inbox_object_id=inbox_object.id
    )
    if not outbox_activity.id:
        raise ValueError("Should never happen")
    await new_outgoing_activity(db_session, from_actor.inbox_url, outbox_activity.id)

    notif = models.Notification(
        notification_type=models.NotificationType.REJECTED_FOLLOWER,
        actor_id=from_actor.id,
    )
    db_session.add(notif)


async def _handle_undo_activity(
    db_session: AsyncSession,
    from_actor: models.Actor,
    undo_activity: models.InboxObject,
    ap_activity_to_undo: models.InboxObject,
) -> None:
    if from_actor.ap_id != ap_activity_to_undo.actor.ap_id:
        logger.warning(
            "Actor mismatch between the activity and the object: "
            f"{from_actor.ap_id}/{ap_activity_to_undo.actor.ap_id}"
        )
        return

    ap_activity_to_undo.undone_by_inbox_object_id = undo_activity.id
    ap_activity_to_undo.is_deleted = True

    if ap_activity_to_undo.ap_type == "Follow":
        logger.info(f"Undo follow from {from_actor.ap_id}")
        await db_session.execute(
            delete(models.Follower).where(
                models.Follower.inbox_object_id == ap_activity_to_undo.id
            )
        )
        notif = models.Notification(
            notification_type=models.NotificationType.UNFOLLOW,
            actor_id=from_actor.id,
        )
        db_session.add(notif)

    elif ap_activity_to_undo.ap_type == "Like":
        if not ap_activity_to_undo.activity_object_ap_id:
            raise ValueError("Like without object")
        liked_obj = await get_outbox_object_by_ap_id(
            db_session,
            ap_activity_to_undo.activity_object_ap_id,
        )
        if not liked_obj:
            logger.warning(
                "Cannot find liked object: "
                f"{ap_activity_to_undo.activity_object_ap_id}"
            )
            return

        liked_obj.likes_count = models.OutboxObject.likes_count - 1
        notif = models.Notification(
            notification_type=models.NotificationType.UNDO_LIKE,
            actor_id=from_actor.id,
            outbox_object_id=liked_obj.id,
            inbox_object_id=ap_activity_to_undo.id,
        )
        db_session.add(notif)

    elif ap_activity_to_undo.ap_type == "Announce":
        if not ap_activity_to_undo.activity_object_ap_id:
            raise ValueError("Announce witout object")
        announced_obj_ap_id = ap_activity_to_undo.activity_object_ap_id
        logger.info(
            f"Undo for announce {ap_activity_to_undo.ap_id}/{announced_obj_ap_id}"
        )
        if announced_obj_ap_id.startswith(BASE_URL):
            announced_obj_from_outbox = await get_outbox_object_by_ap_id(
                db_session, announced_obj_ap_id
            )
            if announced_obj_from_outbox:
                logger.info("Found in the oubox")
                announced_obj_from_outbox.announces_count = (
                    models.OutboxObject.announces_count - 1
                )
                notif = models.Notification(
                    notification_type=models.NotificationType.UNDO_ANNOUNCE,
                    actor_id=from_actor.id,
                    outbox_object_id=announced_obj_from_outbox.id,
                    inbox_object_id=ap_activity_to_undo.id,
                )
                db_session.add(notif)
    else:
        logger.warning(f"Don't know how to undo {ap_activity_to_undo.ap_type} activity")

    # commit will be perfomed in save_to_inbox


async def _handle_move_activity(
    db_session: AsyncSession,
    from_actor: models.Actor,
    move_activity: models.InboxObject,
) -> None:
    logger.info("Processing Move activity")

    # Ensure the object matches the actor
    old_actor_id = ap.get_object_id(move_activity.ap_object)
    if old_actor_id != from_actor.ap_id:
        logger.warning(
            f"Object does not match the actor: {old_actor_id}/{from_actor.ap_id}"
        )
        return None

    # Fetch the target account
    target = move_activity.ap_object.get("target")
    if not target:
        logger.warning("Missing target")
        return None

    new_actor_id = ap.get_id(target)
    new_actor = await fetch_actor(db_session, new_actor_id)

    logger.info(f"Moving {old_actor_id} to {new_actor_id}")

    # Ensure the target account references the old account
    if old_actor_id not in (aks := new_actor.ap_actor.get("alsoKnownAs", [])):
        logger.warning(
            f"New account does not have have an alias for the old account: {aks}"
        )
        return None

    # Unfollow the old account
    following = (
        await db_session.execute(
            select(models.Following)
            .where(models.Following.ap_actor_id == old_actor_id)
            .options(joinedload(models.Following.outbox_object))
        )
    ).scalar_one_or_none()
    if not following:
        logger.warning("Not following the Move actor")
        return

    await _send_undo(db_session, following.outbox_object.ap_id)

    # Follow the new one
    if not (
        await db_session.execute(
            select(models.Following).where(models.Following.ap_actor_id == new_actor_id)
        )
    ).scalar():
        await _send_follow(db_session, new_actor_id)
    else:
        logger.info(f"Already following target {new_actor_id}")

    notif = models.Notification(
        notification_type=models.NotificationType.MOVE,
        actor_id=new_actor.id,
        inbox_object_id=move_activity.id,
    )
    db_session.add(notif)


async def _handle_update_activity(
    db_session: AsyncSession,
    from_actor: models.Actor,
    update_activity: models.InboxObject,
) -> None:
    logger.info("Processing Update activity")
    wrapped_object = await ap.get_object(update_activity.ap_object)
    if wrapped_object["type"] in ap.ACTOR_TYPES:
        logger.info("Updating actor")

        updated_actor = RemoteActor(wrapped_object)
        if (
            from_actor.ap_id != updated_actor.ap_id
            or ap.as_list(from_actor.ap_type)[0] not in ap.ACTOR_TYPES
            or ap.as_list(updated_actor.ap_type)[0] not in ap.ACTOR_TYPES
            or from_actor.handle != updated_actor.handle
        ):
            raise ValueError(
                f"Invalid Update activity {from_actor.ap_actor}/"
                f"{updated_actor.ap_actor}"
            )

        # Update the actor
        from_actor.ap_actor = updated_actor.ap_actor
    elif (ap_type := wrapped_object["type"]) in [
        "Question",
        "Note",
        "Article",
        "Page",
        "Video",
    ]:
        logger.info(f"Updating {ap_type}")
        existing_object = await get_inbox_object_by_ap_id(
            db_session, wrapped_object["id"]
        )
        if not existing_object:
            logger.info(f"{ap_type} not found in the inbox")
        elif existing_object.actor.ap_id != from_actor.ap_id:
            logger.warning(
                f"Update actor does not match the {ap_type} actor {from_actor.ap_id}"
                f"/{existing_object.actor.ap_id}"
            )
        else:
            # Everything looks correct, update the object in the inbox
            logger.info(f"Updating {existing_object.ap_id}")
            existing_object.ap_object = wrapped_object
    else:
        # TODO(ts): support updating objects
        logger.info(f'Cannot update {wrapped_object["type"]}')


async def _handle_create_activity(
    db_session: AsyncSession,
    from_actor: models.Actor,
    create_activity: models.InboxObject,
    forwarded_by_actor: models.Actor | None = None,
) -> None:
    logger.info("Processing Create activity")
    wrapped_object = ap.unwrap_activity(create_activity.ap_object)
    if create_activity.actor.ap_id != ap.get_actor_id(wrapped_object):
        raise ValueError("Object actor does not match activity")

    ro = RemoteObject(wrapped_object, actor=from_actor)

    # Check if we already received a delete for this object (happens often
    # with forwarded replies)
    delete_object = await get_inbox_delete_for_activity_object_ap_id(
        db_session,
        ro.ap_id,
    )
    if delete_object:
        if delete_object.actor.ap_id != from_actor.ap_id:
            logger.warning(
                f"Got a Delete for {ro.ap_id} from {delete_object.actor.ap_id}??"
            )
            return None
        else:
            logger.info("Already received a Delete for this object, deleting activity")
            create_activity.is_deleted = True
            await db_session.flush()
            return None

    await _process_note_object(
        db_session,
        create_activity,
        from_actor,
        ro,
        forwarded_by_actor=forwarded_by_actor,
    )


async def _handle_read_activity(
    db_session: AsyncSession,
    from_actor: models.Actor,
    read_activity: models.InboxObject,
) -> None:
    logger.info("Processing Read activity")

    # Honk uses Read activity to propagate replies, fetch the read object
    # from the remote server
    wrapped_object = await ap.fetch(ap.get_id(read_activity.ap_object["object"]))

    wrapped_object_actor = await fetch_actor(
        db_session, ap.get_actor_id(wrapped_object)
    )
    if not wrapped_object_actor.is_blocked:
        ro = RemoteObject(wrapped_object, actor=wrapped_object_actor)

        # Then process it likes it's coming from a forwarded activity
        await _process_note_object(db_session, read_activity, wrapped_object_actor, ro)


async def _process_note_object(
    db_session: AsyncSession,
    parent_activity: models.InboxObject,
    from_actor: models.Actor,
    ro: RemoteObject,
    forwarded_by_actor: models.Actor | None = None,
    process_quoted_url: bool = True,
) -> models.InboxObject:
    if process_quoted_url and parent_activity.quote_url == ro.ap_id:
        logger.info(f"Processing quoted URL for {parent_activity.ap_id}")
    elif parent_activity.ap_type not in ["Create", "Read"]:
        raise ValueError(f"Unexpected parent activity {parent_activity.ap_id}")

    ap_published_at = now()
    if "published" in ro.ap_object:
        ap_published_at = parse_isoformat(ro.ap_object["published"])

    following = await _get_following(db_session)

    is_from_following = ro.actor.ap_id in {f.ap_actor_id for f in following}
    is_reply = bool(ro.in_reply_to)
    is_local_reply = (
        ro.in_reply_to
        and ro.in_reply_to.startswith(BASE_URL)
        and ro.content  # Hide votes from Question
    )
    is_mention = False
    tags = ro.ap_object.get("tag", [])
    for tag in ap.as_list(tags):
        if tag.get("name") == LOCAL_ACTOR.handle or tag.get("href") == LOCAL_ACTOR.url:
            is_mention = True

    inbox_object = models.InboxObject(
        server=urlparse(ro.ap_id).hostname,
        actor_id=from_actor.id,
        ap_actor_id=from_actor.ap_id,
        ap_type=ro.ap_type,
        ap_id=ro.ap_id,
        ap_context=ro.ap_context,
        conversation=await fetch_conversation_root(db_session, ro),
        ap_published_at=ap_published_at,
        ap_object=ro.ap_object,
        visibility=ro.visibility,
        relates_to_inbox_object_id=parent_activity.id,
        relates_to_outbox_object_id=None,
        activity_object_ap_id=ro.activity_object_ap_id,
        og_meta=await opengraph.og_meta_from_note(db_session, ro),
        # Hide replies from the stream
        is_hidden_from_stream=not (
            (not is_reply and is_from_following) or is_mention or is_local_reply
        ),
        # We may already have some replies in DB
        replies_count=await _get_replies_count(db_session, ro.ap_id),
        quoted_inbox_object_id=None,
    )

    db_session.add(inbox_object)
    await db_session.flush()
    await db_session.refresh(inbox_object)

    parent_activity.relates_to_inbox_object_id = inbox_object.id

    if inbox_object.in_reply_to:
        replied_object = await get_anybox_object_by_ap_id(
            db_session, inbox_object.in_reply_to
        )
        if replied_object:
            if replied_object.is_from_outbox:
                if replied_object.ap_type == "Question" and inbox_object.ap_object.get(
                    "name"
                ):
                    await _handle_vote_answer(
                        db_session,
                        inbox_object,
                        replied_object,  # type: ignore  # outbox check below
                    )
                else:
                    new_replies_count = await _get_replies_count(
                        db_session, replied_object.ap_id
                    )

                    await db_session.execute(
                        update(models.OutboxObject)
                        .where(
                            models.OutboxObject.id == replied_object.id,
                        )
                        .values(replies_count=new_replies_count)
                    )
            else:
                new_replies_count = await _get_replies_count(
                    db_session, replied_object.ap_id
                )

                await db_session.execute(
                    update(models.InboxObject)
                    .where(
                        models.InboxObject.id == replied_object.id,
                    )
                    .values(replies_count=new_replies_count)
                )

        # This object is a reply of a local object, we may need to forward it
        # to our followers (we can only forward JSON-LD signed activities)
        if (
            parent_activity.ap_type == "Create"
            and replied_object
            and replied_object.is_from_outbox
            and replied_object.ap_type != "Question"
            and parent_activity.has_ld_signature
        ):
            logger.info("Forwarding Create activity as it's a local reply")
            skip_actors = [parent_activity.actor]
            if forwarded_by_actor:
                skip_actors.append(forwarded_by_actor)
            recipients = await _get_followers_recipients(
                db_session,
                skip_actors=skip_actors,
            )
            for rcp in recipients:
                await new_outgoing_activity(
                    db_session,
                    rcp,
                    outbox_object_id=None,
                    inbox_object_id=parent_activity.id,
                )

    if is_mention:
        notif = models.Notification(
            notification_type=models.NotificationType.MENTION,
            actor_id=from_actor.id,
            inbox_object_id=inbox_object.id,
        )
        db_session.add(notif)

    await db_session.flush()

    if ro.quote_url and process_quoted_url:
        try:
            quoted_raw_object = await ap.fetch(ro.quote_url)
            quoted_object_actor = await fetch_actor(
                db_session, ap.get_actor_id(quoted_raw_object)
            )
            quoted_ro = RemoteObject(quoted_raw_object, quoted_object_actor)
            quoted_inbox_object = await _process_note_object(
                db_session,
                inbox_object,
                from_actor=quoted_object_actor,
                ro=quoted_ro,
                process_quoted_url=False,
            )
            inbox_object.quoted_inbox_object_id = quoted_inbox_object.id
        except Exception:
            logger.exception("Failed to process quoted object")

    return inbox_object


async def _handle_vote_answer(
    db_session: AsyncSession,
    answer: models.InboxObject,
    question: models.OutboxObject,
) -> None:
    logger.info(f"Processing poll answer for {question.ap_id}: {answer.ap_id}")

    if question.is_poll_ended:
        logger.warning("Poll is ended, discarding answer")
        return

    if not question.poll_items:
        raise ValueError("Should never happen")

    answer_name = answer.ap_object["name"]
    if answer_name not in {pi["name"] for pi in question.poll_items}:
        logger.warning(f"Invalid answer {answer_name=}")
        return

    answer.is_transient = True
    poll_answer = models.PollAnswer(
        outbox_object_id=question.id,
        poll_type="oneOf" if question.is_one_of_poll else "anyOf",
        inbox_object_id=answer.id,
        actor_id=answer.actor.id,
        name=answer_name,
    )
    db_session.add(poll_answer)
    await db_session.flush()

    voters_count = await db_session.scalar(
        select(func.count(func.distinct(models.PollAnswer.actor_id))).where(
            models.PollAnswer.outbox_object_id == question.id
        )
    )

    all_answers = await db_session.execute(
        select(
            func.count(models.PollAnswer.name).label("answer_count"),
            models.PollAnswer.name,
        )
        .where(models.PollAnswer.outbox_object_id == question.id)
        .group_by(models.PollAnswer.name)
    )
    all_answers_count = {a["name"]: a["answer_count"] for a in all_answers}

    logger.info(f"{voters_count=}")
    logger.info(f"{all_answers_count=}")

    question_ap_object = dict(question.ap_object)
    question_ap_object["votersCount"] = voters_count
    items_key = "oneOf" if question.is_one_of_poll else "anyOf"
    question_ap_object[items_key] = [
        {
            "type": "Note",
            "name": item["name"],
            "replies": {
                "type": "Collection",
                "totalItems": all_answers_count.get(item["name"], 0),
            },
        }
        for item in question.poll_items
    ]
    updated = now().replace(microsecond=0).isoformat().replace("+00:00", "Z")
    question_ap_object["updated"] = updated
    question.ap_object = question_ap_object

    logger.info(f"Updated question: {question.ap_object}")

    await db_session.flush()

    # Finally send an update
    recipients = await _compute_recipients(db_session, question.ap_object)
    for rcp in recipients:
        await new_outgoing_activity(db_session, rcp, question.id)


async def _handle_announce_activity(
    db_session: AsyncSession,
    actor: models.Actor,
    announce_activity: models.InboxObject,
    relates_to_outbox_object: models.OutboxObject | None,
    relates_to_inbox_object: models.InboxObject | None,
):
    if relates_to_outbox_object:
        # This is an announce for a local object
        relates_to_outbox_object.announces_count = (
            models.OutboxObject.announces_count + 1
        )

        notif = models.Notification(
            notification_type=models.NotificationType.ANNOUNCE,
            actor_id=actor.id,
            outbox_object_id=relates_to_outbox_object.id,
            inbox_object_id=announce_activity.id,
        )
        db_session.add(notif)
    else:
        # Only show the announce in the stream if it comes from an actor
        # in the following collection
        followings = await _get_following(db_session)
        is_from_following = announce_activity.actor.ap_id in {
            f.ap_actor_id for f in followings
        }

        # This is announce for a maybe unknown object
        if relates_to_inbox_object:
            # We already know about this object, show the announce in the
            # stream if it's not already there, from an followed actor
            # and if we haven't seen it recently
            skip_delta = timedelta(hours=1)
            delta_from_original = now() - as_utc(
                relates_to_inbox_object.ap_published_at  # type: ignore
            )
            dup_count = 0
            if (
                not relates_to_inbox_object.is_hidden_from_stream
                and delta_from_original < skip_delta
            ) or (
                dup_count := (
                    await db_session.scalar(
                        select(func.count(models.InboxObject.id)).where(
                            models.InboxObject.ap_type == "Announce",
                            models.InboxObject.ap_published_at > now() - skip_delta,
                            models.InboxObject.relates_to_inbox_object_id
                            == relates_to_inbox_object.id,
                            models.InboxObject.is_hidden_from_stream.is_(False),
                        )
                    )
                )
            ) > 0:
                logger.info(f"Deduping Announce {delta_from_original=}/{dup_count=}")
                announce_activity.is_hidden_from_stream = True
            else:
                announce_activity.is_hidden_from_stream = not is_from_following

        else:
            # Save it as an inbox object
            if not announce_activity.activity_object_ap_id:
                raise ValueError("Should never happen")
            announced_raw_object = await ap.fetch(
                announce_activity.activity_object_ap_id
            )

            # Some software return objects wrapped in a Create activity (like
            # python-federation)
            if ap.as_list(announced_raw_object["type"])[0] == "Create":
                announced_raw_object = await ap.get_object(announced_raw_object)

            announced_actor = await fetch_actor(
                db_session, ap.get_actor_id(announced_raw_object)
            )
            if not announced_actor.is_blocked:
                announced_object = RemoteObject(announced_raw_object, announced_actor)
                announced_inbox_object = models.InboxObject(
                    server=urlparse(announced_object.ap_id).hostname,
                    actor_id=announced_actor.id,
                    ap_actor_id=announced_actor.ap_id,
                    ap_type=announced_object.ap_type,
                    ap_id=announced_object.ap_id,
                    ap_context=announced_object.ap_context,
                    ap_published_at=announced_object.ap_published_at,
                    ap_object=announced_object.ap_object,
                    visibility=announced_object.visibility,
                    og_meta=await opengraph.og_meta_from_note(
                        db_session, announced_object
                    ),
                    is_hidden_from_stream=True,
                )
                db_session.add(announced_inbox_object)
                await db_session.flush()
                announce_activity.relates_to_inbox_object_id = announced_inbox_object.id
                announce_activity.is_hidden_from_stream = not is_from_following


async def _handle_like_activity(
    db_session: AsyncSession,
    actor: models.Actor,
    like_activity: models.InboxObject,
    relates_to_outbox_object: models.OutboxObject | None,
    relates_to_inbox_object: models.InboxObject | None,
):
    if not relates_to_outbox_object:
        logger.info(
            "Received a like for an unknown activity: "
            f"{like_activity.activity_object_ap_id}, deleting the activity"
        )
        await db_session.delete(like_activity)
    else:
        relates_to_outbox_object.likes_count = models.OutboxObject.likes_count + 1

        notif = models.Notification(
            notification_type=models.NotificationType.LIKE,
            actor_id=actor.id,
            outbox_object_id=relates_to_outbox_object.id,
            inbox_object_id=like_activity.id,
        )
        db_session.add(notif)


async def _process_transient_object(
    db_session: AsyncSession,
    raw_object: ap.RawObject,
    from_actor: models.Actor,
) -> None:
    # TODO: track featured/pinned objects for actors
    ap_type = raw_object["type"]
    if ap_type in ["Add", "Remove"]:
        logger.info(f"Dropping unsupported {ap_type} object")
    else:
        # FIXME(ts): handle transient create
        logger.warning(f"Received unknown {ap_type} object")

    return None


async def save_to_inbox(
    db_session: AsyncSession,
    raw_object: ap.RawObject,
    sent_by_ap_actor_id: str,
) -> None:
    # Special case for server sending the actor as a payload (like python-federation)
    if ap.as_list(raw_object["type"])[0] in ap.ACTOR_TYPES:
        if ap.get_id(raw_object) == sent_by_ap_actor_id:
            updated_actor = RemoteActor(raw_object)

            try:
                actor = await fetch_actor(db_session, sent_by_ap_actor_id)
            except ap.ObjectNotFoundError:
                logger.warning("Actor not found")
                return

            # Update the actor
            actor.ap_actor = updated_actor.ap_actor
            await db_session.commit()
            return

        else:
            logger.warning(
                f"Reveived an actor payload {raw_object} from " f"{sent_by_ap_actor_id}"
            )
            return

    try:
        actor = await fetch_actor(db_session, ap.get_id(raw_object["actor"]))
    except ap.ObjectNotFoundError:
        logger.warning("Actor not found")
        return
    except httpx.HTTPStatusError:
        logger.exception("Failed to fetch actor")
        return

    if actor.server in BLOCKED_SERVERS:
        logger.warning(f"Server {actor.server} is blocked")
        return

    if "id" not in raw_object or not raw_object["id"]:
        await _process_transient_object(db_session, raw_object, actor)
        return None

    if actor.is_blocked:
        logger.warning("Actor {actor.ap_id} is blocked, ignoring object")
        return None

    raw_object_id = ap.get_id(raw_object)
    forwarded_by_actor = None

    # Ensure forwarded activities have a valid LD sig
    if sent_by_ap_actor_id != actor.ap_id:
        logger.info(
            f"Processing a forwarded activity {sent_by_ap_actor_id=}/{actor.ap_id}"
        )
        forwarded_by_actor = await fetch_actor(db_session, sent_by_ap_actor_id)

        is_sig_verified = False
        try:
            is_sig_verified = await ldsig.verify_signature(db_session, raw_object)
        except Exception:
            logger.exception("Failed to verify LD sig")

        if not is_sig_verified:
            logger.warning(
                f"Failed to verify LD sig, fetching remote object {raw_object_id}"
            )

            # Try to fetch the remote object since we failed to verify the LD sig
            try:
                raw_object = await ap.fetch(raw_object_id)
            except Exception:
                raise fastapi.HTTPException(status_code=401, detail="Invalid LD sig")

            # Transient activities from Mastodon like Like are not fetchable and
            # will return the actor instead
            if raw_object["id"] != raw_object_id:
                logger.info(f"Unable to fetch {raw_object_id}")
                return None

    if (
        await db_session.scalar(
            select(func.count(models.InboxObject.id)).where(
                models.InboxObject.ap_id == raw_object_id
            )
        )
        > 0
    ):
        logger.info(
            f'Received duplicate {raw_object["type"]} activity: {raw_object_id}'
        )
        return

    ap_published_at = now()
    if "published" in raw_object:
        ap_published_at = parse_isoformat(raw_object["published"])

    activity_ro = RemoteObject(raw_object, actor=actor)

    relates_to_inbox_object: models.InboxObject | None = None
    relates_to_outbox_object: models.OutboxObject | None = None
    if activity_ro.activity_object_ap_id:
        if activity_ro.activity_object_ap_id.startswith(BASE_URL):
            relates_to_outbox_object = await get_outbox_object_by_ap_id(
                db_session,
                activity_ro.activity_object_ap_id,
            )
        else:
            relates_to_inbox_object = await get_inbox_object_by_ap_id(
                db_session,
                activity_ro.activity_object_ap_id,
            )

    inbox_object = models.InboxObject(
        server=urlparse(activity_ro.ap_id).hostname,
        actor_id=actor.id,
        ap_actor_id=actor.ap_id,
        ap_type=activity_ro.ap_type,
        ap_id=activity_ro.ap_id,
        ap_context=activity_ro.ap_context,
        ap_published_at=ap_published_at,
        ap_object=activity_ro.ap_object,
        visibility=activity_ro.visibility,
        relates_to_inbox_object_id=relates_to_inbox_object.id
        if relates_to_inbox_object
        else None,
        relates_to_outbox_object_id=relates_to_outbox_object.id
        if relates_to_outbox_object
        else None,
        activity_object_ap_id=activity_ro.activity_object_ap_id,
        is_hidden_from_stream=True,
    )

    db_session.add(inbox_object)
    await db_session.flush()
    await db_session.refresh(inbox_object)

    if activity_ro.ap_type == "Create":
        await _handle_create_activity(
            db_session, actor, inbox_object, forwarded_by_actor=forwarded_by_actor
        )
    elif activity_ro.ap_type == "Read":
        await _handle_read_activity(db_session, actor, inbox_object)
    elif activity_ro.ap_type == "Update":
        await _handle_update_activity(db_session, actor, inbox_object)
    elif activity_ro.ap_type == "Move":
        await _handle_move_activity(db_session, actor, inbox_object)
    elif activity_ro.ap_type == "Delete":
        await _handle_delete_activity(
            db_session,
            actor,
            inbox_object,
            relates_to_inbox_object,
            forwarded_by_actor=forwarded_by_actor,
        )
    elif activity_ro.ap_type == "Follow":
        await _handle_follow_follow_activity(db_session, actor, inbox_object)
    elif activity_ro.ap_type == "Undo":
        if relates_to_inbox_object:
            await _handle_undo_activity(
                db_session, actor, inbox_object, relates_to_inbox_object
            )
        else:
            logger.info("Received Undo for an unknown activity")
    elif activity_ro.ap_type in ["Accept", "Reject"]:
        if not relates_to_outbox_object:
            logger.info(
                f"Received {raw_object['type']} for an unknown activity: "
                f"{activity_ro.activity_object_ap_id}"
            )
        else:
            if relates_to_outbox_object.ap_type == "Follow":
                notif_type = (
                    models.NotificationType.FOLLOW_REQUEST_ACCEPTED
                    if activity_ro.ap_type == "Accept"
                    else models.NotificationType.FOLLOW_REQUEST_REJECTED
                )
                notif = models.Notification(
                    notification_type=notif_type,
                    actor_id=actor.id,
                    inbox_object_id=inbox_object.id,
                )
                db_session.add(notif)

                if activity_ro.ap_type == "Accept":
                    following = models.Following(
                        actor_id=actor.id,
                        outbox_object_id=relates_to_outbox_object.id,
                        ap_actor_id=actor.ap_id,
                    )
                    db_session.add(following)

                    # Pre-fetch the latest activities
                    try:
                        await _prefetch_actor_outbox(db_session, actor)
                    except Exception:
                        logger.exception(f"Failed to prefetch outbox for {actor.ap_id}")
                elif activity_ro.ap_type == "Reject":
                    maybe_following = (
                        await db_session.scalars(
                            select(models.Following).where(
                                models.Following.ap_actor_id == actor.ap_id,
                            )
                        )
                    ).one_or_none()
                    if maybe_following:
                        logger.info("Removing actor from following")
                        await db_session.delete(maybe_following)

            else:
                logger.info(
                    "Received an Accept for an unsupported activity: "
                    f"{relates_to_outbox_object.ap_type}"
                )
    elif activity_ro.ap_type == "EmojiReact":
        if not relates_to_outbox_object:
            logger.info(
                "Received a reaction for an unknown activity: "
                f"{activity_ro.activity_object_ap_id}"
            )
            await db_session.delete(inbox_object)
        else:
            # TODO(ts): support reactions
            pass
    elif activity_ro.ap_type == "Like":
        await _handle_like_activity(
            db_session,
            actor,
            inbox_object,
            relates_to_outbox_object,
            relates_to_inbox_object,
        )
    elif activity_ro.ap_type == "Announce":
        await _handle_announce_activity(
            db_session,
            actor,
            inbox_object,
            relates_to_outbox_object,
            relates_to_inbox_object,
        )
    elif activity_ro.ap_type == "View":
        # View is used by Peertube, there's nothing useful we can do with it
        await db_session.delete(inbox_object)
    else:
        logger.warning(f"Received an unknown {inbox_object.ap_type} object")

    await db_session.commit()


async def _prefetch_actor_outbox(
    db_session: AsyncSession,
    actor: models.Actor,
) -> None:
    """Try to fetch some notes to fill the stream"""
    saved = 0
    outbox = await ap.parse_collection(actor.outbox_url, limit=20)
    for activity in outbox[:20]:
        activity_id = ap.get_id(activity)
        raw_activity = await ap.fetch(activity_id)
        if ap.as_list(raw_activity["type"])[0] == "Create":
            obj = await ap.get_object(raw_activity)
            saved_inbox_object = await get_inbox_object_by_ap_id(
                db_session, ap.get_id(obj)
            )
            if not saved_inbox_object:
                saved_inbox_object = await save_object_to_inbox(db_session, obj)

            if not saved_inbox_object.in_reply_to:
                saved_inbox_object.is_hidden_from_stream = False

            saved += 1

        if saved >= 5:
            break

    # commit is performed by the called


async def save_object_to_inbox(
    db_session: AsyncSession,
    raw_object: ap.RawObject,
) -> models.InboxObject:
    """Used to save unknown object before intetacting with them, i.e. to like
    an object that was looked up, or prefill the inbox when an actor accepted
    a follow request."""
    obj_actor = await fetch_actor(db_session, ap.get_actor_id(raw_object))

    ro = RemoteObject(raw_object, actor=obj_actor)

    ap_published_at = now()
    if "published" in ro.ap_object:
        ap_published_at = parse_isoformat(ro.ap_object["published"])

    inbox_object = models.InboxObject(
        server=urlparse(ro.ap_id).hostname,
        actor_id=obj_actor.id,
        ap_actor_id=obj_actor.ap_id,
        ap_type=ro.ap_type,
        ap_id=ro.ap_id,
        ap_context=ro.ap_context,
        conversation=await fetch_conversation_root(db_session, ro),
        ap_published_at=ap_published_at,
        ap_object=ro.ap_object,
        visibility=ro.visibility,
        relates_to_inbox_object_id=None,
        relates_to_outbox_object_id=None,
        activity_object_ap_id=ro.activity_object_ap_id,
        og_meta=await opengraph.og_meta_from_note(db_session, ro),
        is_hidden_from_stream=True,
    )

    db_session.add(inbox_object)
    await db_session.flush()
    await db_session.refresh(inbox_object)
    return inbox_object


async def public_outbox_objects_count(db_session: AsyncSession) -> int:
    return await db_session.scalar(
        select(func.count(models.OutboxObject.id)).where(
            models.OutboxObject.visibility == ap.VisibilityEnum.PUBLIC,
            models.OutboxObject.is_deleted.is_(False),
        )
    )


async def fetch_actor_collection(db_session: AsyncSession, url: str) -> list[Actor]:
    if url.startswith(config.BASE_URL):
        if url == config.BASE_URL + "/followers":
            followers = (
                (
                    await db_session.scalars(
                        select(models.Follower).options(
                            joinedload(models.Follower.actor)
                        )
                    )
                )
                .unique()
                .all()
            )
            return [follower.actor for follower in followers]
        else:
            raise ValueError(f"internal collection for {url}) not supported")

    return [RemoteActor(actor) for actor in await ap.parse_collection(url)]


@dataclass
class ReplyTreeNode:
    ap_object: AnyboxObject
    children: list["ReplyTreeNode"]
    is_requested: bool = False
    is_root: bool = False


async def get_replies_tree(
    db_session: AsyncSession,
    requested_object: AnyboxObject,
    is_current_user_admin: bool,
) -> ReplyTreeNode:
    # XXX: PeerTube video don't use context
    tree_nodes: list[AnyboxObject] = []
    if requested_object.conversation is None:
        tree_nodes = [requested_object]
    else:
        allowed_visibility = [ap.VisibilityEnum.PUBLIC, ap.VisibilityEnum.UNLISTED]
        if is_current_user_admin:
            allowed_visibility = list(ap.VisibilityEnum)

        tree_nodes.extend(
            (
                await db_session.scalars(
                    select(models.InboxObject)
                    .where(
                        models.InboxObject.conversation
                        == requested_object.conversation,
                        models.InboxObject.ap_type.in_(["Note", "Page", "Article"]),
                        models.InboxObject.is_deleted.is_(False),
                        models.InboxObject.visibility.in_(allowed_visibility),
                    )
                    .options(joinedload(models.InboxObject.actor))
                )
            )
            .unique()
            .all()
        )
        tree_nodes.extend(
            (
                await db_session.scalars(
                    select(models.OutboxObject)
                    .where(
                        models.OutboxObject.conversation
                        == requested_object.conversation,
                        models.OutboxObject.is_deleted.is_(False),
                        models.OutboxObject.ap_type.in_(["Note", "Page", "Article"]),
                        models.OutboxObject.visibility.in_(allowed_visibility),
                    )
                    .options(
                        joinedload(
                            models.OutboxObject.outbox_object_attachments
                        ).options(joinedload(models.OutboxObjectAttachment.upload))
                    )
                )
            )
            .unique()
            .all()
        )
    nodes_by_in_reply_to = defaultdict(list)
    for node in tree_nodes:
        nodes_by_in_reply_to[node.in_reply_to].append(node)
    logger.info(nodes_by_in_reply_to)

    if len(nodes_by_in_reply_to.get(None, [])) > 1:
        raise ValueError(f"Invalid replies tree: {[n.ap_object for n in tree_nodes]}")

    def _get_reply_node_children(
        node: ReplyTreeNode,
        index: defaultdict[str | None, list[AnyboxObject]],
    ) -> list[ReplyTreeNode]:
        children = []
        for child in index.get(node.ap_object.ap_id, []):  # type: ignore
            child_node = ReplyTreeNode(
                ap_object=child,
                is_requested=child.ap_id == requested_object.ap_id,  # type: ignore
                children=[],
            )
            child_node.children = _get_reply_node_children(child_node, index)
            children.append(child_node)

        return sorted(
            children,
            key=lambda node: node.ap_object.ap_published_at,  # type: ignore
        )

    if None in nodes_by_in_reply_to:
        root_ap_object = nodes_by_in_reply_to[None][0]
    else:
        root_ap_object = sorted(
            tree_nodes,
            key=lambda ap_obj: ap_obj.ap_published_at,  # type: ignore
        )[0]

    root_node = ReplyTreeNode(
        ap_object=root_ap_object,
        is_root=True,
        is_requested=root_ap_object.ap_id == requested_object.ap_id,
        children=[],
    )
    root_node.children = _get_reply_node_children(root_node, nodes_by_in_reply_to)
    return root_node
