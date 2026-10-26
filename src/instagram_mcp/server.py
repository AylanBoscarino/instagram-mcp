from __future__ import annotations

import asyncio
import functools
import json
import os
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from urllib.parse import urlsplit

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from . import graph
from .graph import GraphError, ig_user_id

load_dotenv()

mcp = FastMCP("instagram-mcp")

DEFAULT_PROFILE_FIELDS = (
    "id,username,name,biography,followers_count,follows_count,"
    "media_count,profile_picture_url,website"
)
DEFAULT_MEDIA_FIELDS = (
    "id,caption,media_type,media_product_type,media_url,thumbnail_url,"
    "permalink,timestamp,like_count,comments_count"
)
DEFAULT_HASHTAG_MEDIA_FIELDS = (
    "id,caption,media_type,permalink,timestamp,like_count,comments_count"
)

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])

MUTATING_TOOLS = frozenset({
    "publish_image", "publish_reel", "publish_story", "publish_carousel",
    "reply_to_comment", "hide_comment", "delete_comment", "send_dm",
})
DESTRUCTIVE_TOOLS = frozenset({"delete_comment"})


class WriteSafetyError(PermissionError):
    """A remote write is disabled by local configuration."""


def authorize_write(operation: str) -> None:
    """Central policy for all tools that change remote Instagram state."""
    if operation not in MUTATING_TOOLS:
        raise WriteSafetyError("Unknown write operation")
    if os.environ.get("IG_ALLOW_WRITES", "").lower() != "true":
        raise WriteSafetyError(
            "Remote writes are disabled. Set IG_ALLOW_WRITES=true locally to enable them."
        )
    destructive_enabled = os.environ.get("IG_ALLOW_DESTRUCTIVE", "").lower() == "true"
    if operation in DESTRUCTIVE_TOOLS and not destructive_enabled:
        raise WriteSafetyError(
            "Comment deletion is disabled. Set IG_ALLOW_DESTRUCTIVE=true locally to enable it."
        )


def _validate_id(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.isdigit() or len(value) > 64:
        raise ValueError(f"{name} must be a numeric Graph API ID")


def _validate_limit(value: int) -> None:
    if type(value) is not int or not 1 <= value <= 100:
        raise ValueError("limit must be between 1 and 100")


def _validate_text(value: str, name: str, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must contain 1 to {maximum} characters")


def _validate_caption(value: str | None) -> None:
    if value is not None and (not isinstance(value, str) or len(value) > 2200):
        raise ValueError("caption must contain at most 2200 characters")


def _validate_url(value: str | None, name: str, optional: bool = False) -> None:
    if optional and value is None:
        return
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError(f"{name} must be a public HTTPS URL")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"{name} must be a public HTTPS URL")


def tool(fn: F) -> F:
    """Register an MCP tool, mapping GraphError / ValueError into a structured dict."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except GraphError as e:
            return e.to_dict()
        except WriteSafetyError as e:
            return {"error": True, "message": str(e), "type": "WriteSafetyError"}
        except (ValueError, TypeError) as e:
            return {"error": True, "message": str(e), "type": type(e).__name__}

    return mcp.tool()(wrapper)  # type: ignore[return-value]


# ---------- Profile & media ----------

@tool
async def get_my_profile(fields: str | None = None) -> dict[str, Any]:
    """Profile info for the configured Instagram Business/Creator account."""
    _validate_fields(fields, DEFAULT_PROFILE_FIELDS)
    return await graph.get(ig_user_id(), fields=fields or DEFAULT_PROFILE_FIELDS)


def _validate_fields(value: str | None, default: str) -> None:
    if value is None:
        return
    allowed = set(default.replace("{", ",").replace("}", "").split(","))
    fields = value.split(",")
    if len(fields) > 30 or any(field.strip() not in allowed for field in fields):
        raise ValueError("fields contains unsupported values")


@tool
async def list_my_media(
    limit: int = 25,
    after: str | None = None,
    fields: str | None = None,
) -> dict[str, Any]:
    """List one page of recent media. `after` is the paging cursor from a prior response."""
    _validate_limit(limit)
    _validate_fields(fields, DEFAULT_MEDIA_FIELDS)
    return await graph.get(
        f"{ig_user_id()}/media",
        fields=fields or DEFAULT_MEDIA_FIELDS,
        limit=limit,
        after=after,
    )


@tool
async def list_all_media(
    max_pages: int = 5,
    fields: str | None = None,
) -> dict[str, Any]:
    """Walk media pages up to max_pages (default 5 = up to ~125 items)."""
    _validate_fields(fields, DEFAULT_MEDIA_FIELDS)
    if type(max_pages) is not int or not 1 <= max_pages <= graph.MAX_PAGES:
        raise ValueError(f"max_pages must be between 1 and {graph.MAX_PAGES}")
    return await graph.paginate(
        f"{ig_user_id()}/media",
        max_pages=max_pages,
        fields=fields or DEFAULT_MEDIA_FIELDS,
    )


@tool
async def get_media(media_id: str, fields: str | None = None) -> dict[str, Any]:
    """Fetch one media item by ID."""
    _validate_id(media_id, "media_id")
    _validate_fields(fields, DEFAULT_MEDIA_FIELDS)
    return await graph.get(media_id, fields=fields or DEFAULT_MEDIA_FIELDS)


@tool
async def list_tagged_media(limit: int = 25, fields: str | None = None) -> dict[str, Any]:
    """List media the account is tagged in."""
    _validate_limit(limit)
    _validate_fields(fields, DEFAULT_MEDIA_FIELDS)
    return await graph.get(
        f"{ig_user_id()}/tags",
        fields=fields or DEFAULT_MEDIA_FIELDS,
        limit=limit,
    )


@tool
async def list_stories() -> dict[str, Any]:
    """List the account's currently-live stories (expire after 24h)."""
    return await graph.get(
        f"{ig_user_id()}/stories",
        fields="id,media_type,media_url,permalink,timestamp",
    )


# ---------- Hashtags ----------

@tool
async def search_hashtag(query: str) -> dict[str, Any]:
    """Resolve a hashtag string to an IG hashtag ID."""
    hashtag = query.lstrip("#") if isinstance(query, str) else ""
    if not 1 <= len(hashtag) <= 100 or not hashtag.isalnum():
        raise ValueError("query must be a hashtag name")
    return await graph.get("ig_hashtag_search", user_id=ig_user_id(), q=query.lstrip("#"))


@tool
async def hashtag_top_media(hashtag_id: str, fields: str | None = None) -> dict[str, Any]:
    """Top-ranked media for a hashtag."""
    _validate_id(hashtag_id, "hashtag_id")
    _validate_fields(fields, DEFAULT_HASHTAG_MEDIA_FIELDS)
    return await graph.get(
        f"{hashtag_id}/top_media",
        user_id=ig_user_id(),
        fields=fields or DEFAULT_HASHTAG_MEDIA_FIELDS,
    )


@tool
async def hashtag_recent_media(hashtag_id: str, fields: str | None = None) -> dict[str, Any]:
    """Recent media for a hashtag (rolling 24h window)."""
    _validate_id(hashtag_id, "hashtag_id")
    _validate_fields(fields, DEFAULT_HASHTAG_MEDIA_FIELDS)
    return await graph.get(
        f"{hashtag_id}/recent_media",
        user_id=ig_user_id(),
        fields=fields or DEFAULT_HASHTAG_MEDIA_FIELDS,
    )


# ---------- Publishing ----------

async def _wait_container(container_id: str, timeout_s: int = 300) -> None:
    """Poll a media container until status_code=FINISHED. Required for video/reels."""
    elapsed = 0
    while elapsed < timeout_s:
        res = await graph.get(container_id, fields="status_code,status")
        code = res.get("status_code")
        if code == "FINISHED":
            return
        if code in ("ERROR", "EXPIRED"):
            raise GraphError(
                0,
                {"error": {"message": f"container {container_id} status={code}", "code": -1}},
            )
        await asyncio.sleep(3)
        elapsed += 3
    raise TimeoutError(f"container {container_id} did not finish within {timeout_s}s")


async def _publish(container_id: str) -> dict[str, Any]:
    return await graph.post(f"{ig_user_id()}/media_publish", creation_id=container_id)


@tool
async def publish_image(
    image_url: str,
    caption: str | None = None,
    location_id: str | None = None,
) -> dict[str, Any]:
    """Publish a single image post. image_url must be a public HTTPS URL."""
    _validate_url(image_url, "image_url")
    _validate_caption(caption)
    authorize_write("publish_image")
    container = await graph.post(
        f"{ig_user_id()}/media",
        image_url=image_url,
        caption=caption,
        location_id=location_id,
    )
    return await _publish(container["id"])


@tool
async def publish_reel(
    video_url: str,
    caption: str | None = None,
    share_to_feed: bool = True,
    cover_url: str | None = None,
    thumb_offset: int | None = None,
) -> dict[str, Any]:
    """Publish a Reel. video_url must be a public HTTPS MP4."""
    _validate_url(video_url, "video_url")
    _validate_url(cover_url, "cover_url", optional=True)
    _validate_caption(caption)
    authorize_write("publish_reel")
    container = await graph.post(
        f"{ig_user_id()}/media",
        media_type="REELS",
        video_url=video_url,
        caption=caption,
        share_to_feed=str(share_to_feed).lower(),
        cover_url=cover_url,
        thumb_offset=thumb_offset,
    )
    await _wait_container(container["id"])
    return await _publish(container["id"])


@tool
async def publish_story(
    image_url: str | None = None,
    video_url: str | None = None,
) -> dict[str, Any]:
    """Publish a Story. Provide exactly one of image_url or video_url."""
    if bool(image_url) == bool(video_url):
        raise ValueError("Provide exactly one of image_url or video_url")
    _validate_url(image_url, "image_url", optional=True)
    _validate_url(video_url, "video_url", optional=True)
    authorize_write("publish_story")
    container = await graph.post(
        f"{ig_user_id()}/media",
        media_type="STORIES",
        image_url=image_url,
        video_url=video_url,
    )
    if video_url:
        await _wait_container(container["id"])
    return await _publish(container["id"])


@tool
async def publish_carousel(
    items: list[dict[str, str]],
    caption: str | None = None,
) -> dict[str, Any]:
    """Publish a 2-10 item carousel. Each item is {image_url: "..."} or {video_url: "..."}."""
    if not 2 <= len(items) <= 10:
        raise ValueError("Carousel must contain between 2 and 10 items")
    _validate_caption(caption)
    for item in items:
        valid_item = isinstance(item, dict) and len(item) == 1
        if not valid_item or set(item) not in ({"image_url"}, {"video_url"}):
            raise ValueError("Each carousel item must contain one image_url or video_url")
        _validate_url(item[next(iter(item))], "carousel media URL")
    authorize_write("publish_carousel")

    child_ids: list[str] = []
    for item in items:
        if "image_url" in item:
            child = await graph.post(
                f"{ig_user_id()}/media",
                is_carousel_item="true",
                image_url=item["image_url"],
            )
        elif "video_url" in item:
            child = await graph.post(
                f"{ig_user_id()}/media",
                is_carousel_item="true",
                media_type="VIDEO",
                video_url=item["video_url"],
            )
            await _wait_container(child["id"])
        child_ids.append(child["id"])

    parent = await graph.post(
        f"{ig_user_id()}/media",
        media_type="CAROUSEL",
        children=",".join(child_ids),
        caption=caption,
    )
    return await _publish(parent["id"])


@tool
async def get_publish_limit() -> dict[str, Any]:
    """Show publish quota usage in the rolling 24h window."""
    return await graph.get(
        f"{ig_user_id()}/content_publishing_limit",
        fields="quota_usage,config",
    )


# ---------- Comments ----------

@tool
async def list_comments(media_id: str, limit: int = 25) -> dict[str, Any]:
    """List top-level comments on a media item, including their replies."""
    _validate_id(media_id, "media_id")
    _validate_limit(limit)
    return await graph.get(
        f"{media_id}/comments",
        fields="id,text,username,timestamp,like_count,replies{id,text,username,timestamp}",
        limit=limit,
    )


@tool
async def get_comment_replies(comment_id: str, limit: int = 25) -> dict[str, Any]:
    """Fetch replies under a specific comment."""
    _validate_id(comment_id, "comment_id")
    _validate_limit(limit)
    return await graph.get(
        f"{comment_id}/replies",
        fields="id,text,username,timestamp,like_count",
        limit=limit,
    )


@tool
async def reply_to_comment(comment_id: str, message: str) -> dict[str, Any]:
    """Reply to a comment."""
    _validate_id(comment_id, "comment_id")
    _validate_text(message, "message", 2000)
    authorize_write("reply_to_comment")
    return await graph.post(f"{comment_id}/replies", message=message)


@tool
async def hide_comment(comment_id: str, hide: bool = True) -> dict[str, Any]:
    """Hide (or unhide) a comment."""
    _validate_id(comment_id, "comment_id")
    authorize_write("hide_comment")
    return await graph.post(comment_id, hide=str(hide).lower())


@tool
async def delete_comment(comment_id: str) -> dict[str, Any]:
    """Delete a comment owned by the account."""
    _validate_id(comment_id, "comment_id")
    authorize_write("delete_comment")
    return await graph.delete(comment_id)


# ---------- Direct messages ----------

@tool
async def list_conversations(limit: int = 20) -> dict[str, Any]:
    """List Instagram DM conversations for the account."""
    _validate_limit(limit)
    return await graph.get(
        f"{ig_user_id()}/conversations",
        platform="instagram",
        fields="id,updated_time,participants",
        limit=limit,
    )


@tool
async def get_conversation(conversation_id: str, message_limit: int = 25) -> dict[str, Any]:
    """Fetch messages in a conversation."""
    _validate_id(conversation_id, "conversation_id")
    _validate_limit(message_limit)
    return await graph.get(
        conversation_id,
        fields=f"messages.limit({message_limit}){{id,created_time,from,to,message}}",
    )


@tool
async def send_dm(
    recipient_igsid: str,
    text: str,
    message_tag: str | None = None,
) -> dict[str, Any]:
    """Send a DM. Without a tag this is `RESPONSE` (only valid inside the 24h
    user-initiated window). Pass message_tag (e.g. HUMAN_AGENT) to escape that
    window — requires Meta approval for the tag."""
    _validate_id(recipient_igsid, "recipient_igsid")
    _validate_text(text, "text", 1000)
    allowed_tags = {"HUMAN_AGENT", "ACCOUNT_UPDATE", "CONFIRMED_EVENT_UPDATE"}
    if message_tag is not None and message_tag not in allowed_tags:
        raise ValueError("message_tag is not an allowed value")
    authorize_write("send_dm")
    payload: dict[str, Any] = {
        "recipient": json.dumps({"id": recipient_igsid}),
        "message": json.dumps({"text": text}),
    }
    if message_tag:
        payload["messaging_type"] = "MESSAGE_TAG"
        payload["tag"] = message_tag
    else:
        payload["messaging_type"] = "RESPONSE"
    return await graph.post(f"{ig_user_id()}/messages", **payload)


# ---------- Insights ----------

@tool
async def get_account_insights(
    metrics: str = "reach,follower_count",
    period: str = "day",
    metric_type: str | None = None,
    since: int | None = None,
    until: int | None = None,
) -> dict[str, Any]:
    """Account-level insights. Modern metrics (views, accounts_engaged,
    total_interactions, profile_views, likes, comments, shares, saves) require
    metric_type='total_value'."""
    allowed_periods = {"day", "week", "days_28", "month", "lifetime"}
    allowed_metrics = {
        "reach", "follower_count", "views", "accounts_engaged", "total_interactions",
        "profile_views", "likes", "comments", "shares", "saves",
    }
    if period not in allowed_periods or not set(metrics.split(",")) <= allowed_metrics:
        raise ValueError("period or metrics contains unsupported values")
    if metric_type not in {None, "total_value"}:
        raise ValueError("metric_type must be total_value when set")
    if since is not None and (type(since) is not int or since < 0):
        raise ValueError("since must be a non-negative timestamp")
    if until is not None and (type(until) is not int or until < 0):
        raise ValueError("until must be a non-negative timestamp")
    if since is not None and until is not None and since > until:
        raise ValueError("since must not exceed until")
    return await graph.get(
        f"{ig_user_id()}/insights",
        metric=metrics,
        period=period,
        metric_type=metric_type,
        since=since,
        until=until,
    )


@tool
async def get_media_insights(
    media_id: str,
    metrics: str = "reach,saved,likes,comments,shares",
) -> dict[str, Any]:
    """Insights for a single media item. Valid metric set varies by media type
    (post / reel / story)."""
    _validate_id(media_id, "media_id")
    allowed_metrics = {
        "reach", "saved", "likes", "comments", "shares", "views", "total_interactions"
    }
    if not set(metrics.split(",")) <= allowed_metrics:
        raise ValueError("metrics contains unsupported values")
    return await graph.get(f"{media_id}/insights", metric=metrics)


def run() -> None:
    mcp.run()
