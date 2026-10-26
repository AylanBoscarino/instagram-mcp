import os

import httpx
import pytest
import respx

from instagram_mcp import graph
from instagram_mcp.server import (
    MUTATING_TOOLS,
    authorize_write,
    delete_comment,
    get_account_insights,
    get_media,
    get_my_profile,
    mcp,
    publish_story,
    send_dm,
)

BASE = "https://graph.facebook.com/v21.0"
TEST_MEDIA_ID = "123456"


async def test_all_tools_registered():
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    expected = {
        "get_my_profile", "list_my_media", "list_all_media", "get_media",
        "list_tagged_media", "list_stories", "search_hashtag",
        "hashtag_top_media", "hashtag_recent_media", "publish_image",
        "publish_reel", "publish_story", "publish_carousel",
        "get_publish_limit", "list_comments", "get_comment_replies",
        "reply_to_comment", "hide_comment", "delete_comment",
        "list_conversations", "get_conversation", "send_dm",
        "get_account_insights", "get_media_insights",
    }
    assert expected.issubset(names), expected - names
    assert MUTATING_TOOLS == {
        "publish_image", "publish_reel", "publish_story", "publish_carousel",
        "reply_to_comment", "hide_comment", "delete_comment", "send_dm",
    }


@respx.mock
async def test_get_my_profile_happy_path():
    respx.get(f"{BASE}/1234567890").respond(
        200,
        json={"id": "1234567890", "username": "test_account", "followers_count": 42},
    )
    result = await get_my_profile()
    assert result["username"] == "test_account"
    assert result["followers_count"] == 42


@respx.mock
async def test_graph_error_returns_structured_dict():
    respx.get(f"{BASE}/{TEST_MEDIA_ID}").respond(
        400,
        json={
            "error": {
                "message": "Object does not exist",
                "code": 100,
                "error_subcode": 33,
                "fbtrace_id": "trace-abc",
            }
        },
    )
    result = await get_media(TEST_MEDIA_ID)
    assert result == {
        "error": True,
        "status": 400,
        "message": "Object does not exist",
        "code": 100,
        "subcode": 33,
        "fbtrace_id": "trace-abc",
    }


async def test_value_error_returns_structured_dict():
    result = await publish_story()
    assert result["error"] is True
    assert result["type"] == "ValueError"
    assert "exactly one" in result["message"]


async def test_write_tools_blocked_by_default(monkeypatch):
    monkeypatch.delenv("IG_ALLOW_WRITES", raising=False)
    for operation in MUTATING_TOOLS:
        with pytest.raises(PermissionError, match="Remote writes are disabled"):
            authorize_write(operation)


async def test_delete_requires_separate_opt_in(monkeypatch):
    monkeypatch.setenv("IG_ALLOW_WRITES", "true")
    monkeypatch.delenv("IG_ALLOW_DESTRUCTIVE", raising=False)
    with pytest.raises(PermissionError, match="Comment deletion is disabled"):
        authorize_write("delete_comment")


@respx.mock
async def test_enabled_write_reaches_graph(monkeypatch):
    monkeypatch.setenv("IG_ALLOW_WRITES", "true")
    route = respx.post(f"{BASE}/1234567890/messages").respond(
        200, json={"recipient_id": "987654321"}
    )
    result = await send_dm("987654321", "hello")
    assert result == {"recipient_id": "987654321"}
    assert route.called


async def test_delete_tool_blocks_without_network(monkeypatch):
    monkeypatch.setenv("IG_ALLOW_WRITES", "true")
    monkeypatch.delenv("IG_ALLOW_DESTRUCTIVE", raising=False)
    result = await delete_comment("123456")
    assert result["type"] == "WriteSafetyError"


@respx.mock
async def test_paginate_legitimate_next_url_rebuilds_request():
    first = respx.get(f"{BASE}/1234567890/media").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "data": [{"id": "1"}],
                    "paging": {
                        "next": f"{BASE}/1234567890/media?after=abc&access_token=attacker"
                    },
                },
            ),
            httpx.Response(200, json={"data": [{"id": "2"}]}),
        ]
    )
    result = await graph.paginate("1234567890/media")
    assert result["data"] == [{"id": "1"}, {"id": "2"}]
    assert first.call_count == 2
    second_request = first.calls.last.request
    assert second_request.url.params["access_token"] == "test-token"
    assert "attacker" not in str(second_request.url)


@pytest.mark.parametrize("url", [
    "http://graph.facebook.com/v21.0/123/media?after=x",
    "https://evil.example/v21.0/123/media?after=x",
    "https://graph.facebook.com:444/v21.0/123/media?after=x",
    "ftp://graph.facebook.com/v21.0/123/media?after=x",
])
@respx.mock
async def test_paginate_rejects_untrusted_next_urls(url):
    respx.get(f"{BASE}/1234567890/media").respond(200, json={"data": [], "paging": {"next": url}})
    with pytest.raises(ValueError, match="untrusted pagination"):
        await graph.paginate("1234567890/media")
    assert len(respx.calls) == 1


@respx.mock
async def test_paginate_redirect_is_not_followed():
    initial = respx.get(f"{BASE}/1234567890/media").respond(
        200, json={"data": [], "paging": {"next": "https://evil.example/steal"}}
    )
    with pytest.raises(ValueError, match="untrusted pagination"):
        await graph.paginate("1234567890/media")
    assert initial.called
    assert not any("evil.example" in str(call.request.url) for call in respx.calls)


@pytest.mark.parametrize("payload", [
    {"data": {}, "paging": {}},
    {"data": [], "paging": []},
    {"data": [], "paging": {"next": 42}},
])
@respx.mock
async def test_paginate_malformed_data_rejected(payload):
    respx.get(f"{BASE}/1234567890/media").respond(200, json=payload)
    with pytest.raises(ValueError, match="malformed pagination"):
        await graph.paginate("1234567890/media")


@respx.mock
async def test_paginate_repeated_url_rejected():
    next_url = f"{BASE}/1234567890/media?after=x"
    respx.get(f"{BASE}/1234567890/media", params={"access_token": "test-token"}).respond(
        200, json={"data": [], "paging": {"next": next_url}}
    )
    respx.get(
        f"{BASE}/1234567890/media",
        params={"after": "x", "access_token": "test-token"},
    ).respond(
        200, json={"data": [], "paging": {"next": next_url}}
    )
    with pytest.raises(ValueError, match="repeated pagination"):
        await graph.paginate("1234567890/media", max_pages=4)


@respx.mock
async def test_paginate_stops_at_page_limit():
    route = respx.get(f"{BASE}/1234567890/media").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "data": [{"page": 1}],
                    "paging": {"next": f"{BASE}/1234567890/media?after=one"},
                },
            ),
            httpx.Response(
                200,
                json={
                    "data": [{"page": 2}],
                    "paging": {"next": f"{BASE}/1234567890/media?after=two"},
                },
            ),
        ]
    )
    result = await graph.paginate("1234567890/media", max_pages=2)
    assert result["pages_fetched"] == 2
    assert result["has_more"] is True
    assert route.call_count == 2


@pytest.mark.parametrize("max_pages", [0, 101, -1])
async def test_paginate_page_count_is_bounded(max_pages):
    with pytest.raises(ValueError, match="max_pages"):
        await graph.paginate("1234567890/media", max_pages=max_pages)


@respx.mock
async def test_graph_errors_redact_tokens():
    respx.get(f"{BASE}/{TEST_MEDIA_ID}").respond(
        400,
        json={
            "error": {
                "message": "failed test-token?access_token=test-token&client_secret=secret-value"
            }
        },
    )
    result = await get_media(TEST_MEDIA_ID)
    assert "test-token" not in str(result)
    assert "secret-value" not in str(result)


@respx.mock
async def test_graph_error_does_not_return_url_or_token():
    respx.get(f"{BASE}/{TEST_MEDIA_ID}").respond(
        400,
        json={
            "error": {
                "message": "Request failed at https://evil.example/?access_token=test-token"
            }
        },
    )
    result = await get_media(TEST_MEDIA_ID)
    assert "https://" not in str(result)
    assert "test-token" not in str(result)


@respx.mock
async def test_graph_rejects_oversized_response():
    respx.get(f"{BASE}/{TEST_MEDIA_ID}").respond(200, content=b" " * (graph.MAX_RESPONSE_BYTES + 1))
    result = await get_media(TEST_MEDIA_ID)
    assert result["message"] == "Graph API response is too large"


async def test_write_validation_happens_before_authorization_or_network():
    result = await send_dm("not-an-id", "hello")
    assert result["type"] == "ValueError"
    assert "numeric" in result["message"]


def test_token_setup_creates_private_file(tmp_path, monkeypatch):
    from instagram_mcp.scripts import get_token

    path = tmp_path / ".env"
    monkeypatch.chdir(tmp_path)
    prompts = iter(["app-id", "app-secret", "user-token", "1"])
    monkeypatch.setattr(get_token, "_prompt", lambda label, secret=False: next(prompts))
    monkeypatch.setattr(get_token, "_api", lambda path, **params: {
        "access_token": "long-token"
    } if path == "oauth/access_token" else {
        "data": [{
            "name": "Page",
            "access_token": "page-token",
            "instagram_business_account": {"id": "123456789", "username": "sample"},
        }]
    })
    get_token.main()
    assert path.stat().st_mode & 0o777 == 0o600
    assert "page-token" in path.read_text()


@respx.mock
async def test_none_params_are_dropped():
    """Ensure get_account_insights doesn't pass `since=None` to the API."""
    route = respx.get(f"{BASE}/1234567890/insights").respond(200, json={"data": []})
    await get_account_insights(metrics="reach", period="day")
    sent = dict(route.calls.last.request.url.params)
    assert "since" not in sent
    assert "until" not in sent
    assert "metric_type" not in sent
    assert sent["metric"] == "reach"
    assert sent["access_token"] == "test-token"


def test_missing_token_raises():
    import importlib
    os.environ.pop("IG_ACCESS_TOKEN", None)
    importlib.reload(graph)
    with pytest.raises(RuntimeError, match="IG_ACCESS_TOKEN"):
        graph._config()
