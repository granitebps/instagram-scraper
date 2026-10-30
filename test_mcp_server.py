"""Offline tests for the MCP tool layer (mcp_server.py) and the fetch
deadline — no network. Upstream calls are replaced with the captured
payloads in instagram/fixtures/, so these run the real parsers.

    python -m pytest test_mcp_server.py -q
"""
import json
import os
import time

import anyio
import pytest

import mcp_server
from instagram import discovery, fetch, posts, users
from instagram.fetch import (InstagramGated, InstagramNotFound, InstagramRateLimited, InstagramTimeout,
                             InstagramUpstreamError)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instagram", "fixtures")
REEL_CODE = "DdcI4o0Prsz"


def load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return json.load(handle)


def call(tool, **kwargs):
    result = anyio.run(lambda: tool(**kwargs))
    assert json.loads(result.content[0].text) == result.structured_content
    return result


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(mcp_server, "_cache", mcp_server.TTLCache(100))
    users._lookups.clear()


class Upstream:
    """Fake fetch.graphql: answers by query name and counts the calls."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, name, retry_execution_errors=True, retry_rate_limit=True, **variables):
        self.calls.append((name, variables))
        answer = self.answers[name]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _raise(err):
    def fn(*args, **kwargs):
        raise err
    return fn


# ---- compact -------------------------------------------------------------------------

def test_compact_drops_empty_values_but_keeps_top_level_and_falsy_scalars():
    data = {"posts": [], "next_cursor": None, "has_more": False,
            "post": {"caption": "", "location": None, "items": [], "stats": {"like_count": 0}, "audio": {},
                     "owner": {"username": "nike", "profile_picture": "https://cdn/x.jpg"}}}
    assert mcp_server.compact(data) == {"posts": [], "has_more": False,
                                        "post": {"stats": {"like_count": 0}, "owner": {"username": "nike"}}}


# ---- tools ---------------------------------------------------------------------------

def test_get_post_parses_fixture_and_caches_across_input_forms(monkeypatch):
    upstream = Upstream({"PolarisPostRootQuery": load("post_root_reel.json")})
    monkeypatch.setattr(posts, "graphql", upstream)

    first = call(mcp_server.get_post, post=REEL_CODE)
    assert not first.is_error
    assert first.structured_content["code"] == REEL_CODE
    assert None not in first.structured_content.values()
    assert "profile_picture" not in first.content[0].text

    second = call(mcp_server.get_post, post=f"https://www.instagram.com/reel/{REEL_CODE}/?igsh=abc")
    assert second.structured_content == first.structured_content
    assert len(upstream.calls) == 1          # the link resolved to the same cache key


def test_get_post_comments_pages_with_cursor(monkeypatch):
    upstream = Upstream({"PolarisLoggedOutDesktopWWWPostCommentsPaginationQuery": load("comments_page.json")})
    monkeypatch.setattr(posts, "graphql", upstream)

    page = call(mcp_server.get_post_comments, post=REEL_CODE).structured_content
    assert page["code"] == REEL_CODE and page["comment_count"] == len(page["comments"]) > 0
    call(mcp_server.get_post_comments, post=REEL_CODE, cursor="abc")
    assert [v["after"] for _, v in upstream.calls] == [None, "abc"]


def test_get_profile_survives_a_failed_enrichment(monkeypatch):
    upstream = Upstream({
        "PolarisLoggedOutDesktopWWWProfileRootContentQuery": load("profile_root_nike.json"),
        "PolarisProfilePageContentQuery": load("profile_page_content_nike.json"),
    })
    monkeypatch.setattr(users, "graphql", upstream)
    monkeypatch.setattr(fetch, "get_page", _raise(InstagramUpstreamError("HTTP 502")))

    result = call(mcp_server.get_profile, username="@Nike")
    assert not result.is_error
    assert result.structured_content["username"] == "nike"
    assert "profile_picture" not in result.content[0].text


def test_get_profile_reels_uses_the_app_feed(monkeypatch):
    upstream = Upstream({"PolarisLoggedOutDesktopWWWProfileRootContentQuery": load("profile_root_nike.json")})
    monkeypatch.setattr(users, "graphql", upstream)
    monkeypatch.setattr(users, "mobile", lambda path, **kwargs: load("mobile_clips_nike.json"))

    page = call(mcp_server.get_profile_reels, username="nike").structured_content
    assert page["user"]["username"] == "nike"
    assert page["reels"]


def test_search_and_hashtag_share_the_popular_page_parser(monkeypatch):
    upstream = Upstream({"PolarisLoggedOutPopularSearchPageQuery": load("popular_travel.json")})
    monkeypatch.setattr(discovery, "graphql", upstream)

    search = call(mcp_server.search_posts, query="travel").structured_content
    assert search["query"] == "travel" and search["post_count"] == len(search["posts"]) > 0
    tag = call(mcp_server.get_hashtag_posts, tag="#Travel").structured_content
    assert tag["tag"] == "travel" and tag["posts"] == search["posts"]
    assert [v["keyword"] for _, v in upstream.calls] == ["travel", "travel"]


def test_invalid_input_is_a_tool_error_without_upstream_call(monkeypatch):
    upstream = Upstream({})
    monkeypatch.setattr(posts, "graphql", upstream)

    result = call(mcp_server.get_post, post="not a post!")
    assert result.is_error
    error = result.structured_content["error"]
    assert error["code"] == "invalid_input" and error["retryable"] is False
    assert "post" in error["message"]
    assert upstream.calls == []


@pytest.mark.parametrize("raised, code, retryable", [
    (InstagramNotFound("@nobody not found on Instagram"), "not_found", False),
    (InstagramTimeout("ran out of time"), "timeout", True),
    (InstagramGated("login-gated"), "upstream_rejected", False),
    (InstagramRateLimited("rate limit exceeded"), "upstream_rejected", True),
    (InstagramUpstreamError("HTTP 502"), "upstream_unavailable", True),
    (RuntimeError("boom"), "internal_error", False),
])
def test_scraper_failures_map_to_error_codes(monkeypatch, raised, code, retryable):
    monkeypatch.setattr(users, "graphql", Upstream({"PolarisLoggedOutDesktopWWWProfileRootContentQuery": raised}))

    result = call(mcp_server.get_profile, username="nobody")
    assert result.is_error
    assert result.structured_content["error"]["code"] == code
    assert result.structured_content["error"]["retryable"] is retryable


def test_failures_are_not_cached(monkeypatch):
    upstream = Upstream({"PolarisPostRootQuery": InstagramUpstreamError("HTTP 502")})
    monkeypatch.setattr(posts, "graphql", upstream)
    assert call(mcp_server.get_post, post=REEL_CODE).is_error
    upstream.answers["PolarisPostRootQuery"] = load("post_root_reel.json")
    assert not call(mcp_server.get_post, post=REEL_CODE).is_error


def test_cache_is_bounded_and_expires():
    from datetime import timedelta
    cache = mcp_server.TTLCache(2)
    for key in "abc":
        cache.set(key, key, timedelta(minutes=1))
    assert cache.get("a") is None and cache.get("c") == "c"
    cache.set("d", "d", timedelta(seconds=-1))
    assert cache.get("d") is None


# ---- deadline ------------------------------------------------------------------------

def test_deadline_stops_retries_instead_of_sleeping_through_backoff():
    attempts = []

    def failing():
        attempts.append(1)
        raise InstagramUpstreamError("HTTP 500")

    started = time.monotonic()
    with fetch.deadline(0.5), pytest.raises(InstagramTimeout):
        fetch._retrying(failing)
    assert time.monotonic() - started < 1
    assert len(attempts) == 1                # the 2 s backoff would overrun the budget


def test_deadline_caps_request_timeout_and_reaches_parallel_workers():
    assert fetch._timeout(30) == 30          # no deadline: unchanged
    with fetch.deadline(5):
        assert fetch._timeout(30) <= 5
        assert all(t <= 5 for t in fetch.run_parallel([lambda: fetch._timeout(30)] * 2))
    with fetch.deadline(-1), pytest.raises(InstagramTimeout):
        fetch._timeout(30)


def test_a_timeout_is_not_wrapped_as_a_transport_error(monkeypatch):
    # the request timeout is computed before the try / except around the
    # request, so a spent budget surfaces as InstagramTimeout
    with fetch.deadline(-1), pytest.raises(InstagramTimeout):
        fetch.graphql("PolarisPostRootQuery", shortcode=REEL_CODE)


def test_a_timeout_on_an_old_app_cursor_is_not_reported_as_a_stale_cursor(monkeypatch):
    monkeypatch.setattr(users, "lookup", lambda user: {"pk": "1", "username": "nike", "is_private": False})
    monkeypatch.setattr(users, "_app_posts", _raise(InstagramTimeout("ran out of time")))
    with pytest.raises(InstagramTimeout):
        users.get_posts({"username": "nike"}, cursor="3456789012345678901_13460080")
