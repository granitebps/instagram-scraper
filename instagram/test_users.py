"""Offline tests for users/posts (the logged-out profile grid, each post
filled in with its full item), numeric-id lookups, and how a GraphQL
"Rate limit exceeded" is handled — no network.

    python -m pytest instagram/test_users.py -q
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import config  # noqa: E402
from instagram import fetch as F  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _grid_fixture():
    with open(os.path.join(FIXTURES, "posts_grid_nike.json"), encoding="utf-8") as handle:
        return json.load(handle)["data"]


def _raise(err):
    def fn(*args, **kwargs):
        raise err
    return fn


# ---- users/posts: which surface serves a page ---------------------------------

def test_posts_come_from_the_grid_first_and_the_app_feed_only_stands_in_for_page_one(monkeypatch):
    from instagram import users as U
    raw = {"pk": "1", "username": "nike", "is_private": False}
    monkeypatch.setattr(U, "lookup", lambda user: raw)
    calls = []

    def web(raw_user, cursor, window=slice(None)):
        calls.append(("web", cursor))
        return [{"id": "w"}], "AQnext", True, 1

    def app(raw_user, cursor, window=slice(None)):
        calls.append(("app", cursor))
        return [{"id": "a"}], "9_1", True, 1

    monkeypatch.setattr(U, "_web_posts", web)
    monkeypatch.setattr(U, "_app_posts", app)
    out = U.get_posts({"username": "nike"})
    assert out["posts"] == [{"id": "w"}] and out["next_cursor"] == "AQnext" and calls == [("web", None)]

    # the grid failing on page 1: the app feed stands in; when the stand-in
    # fails too (refused, 404 or 400), the grid's own error surfaces
    for err in (F.InstagramBlocked("403"), F.InstagramGated("401"), F.InstagramUpstreamError("SSL")):
        monkeypatch.setattr(U, "_web_posts", _raise(err))
        monkeypatch.setattr(U, "_app_posts", app)
        assert U.get_posts({"username": "nike"})["posts"] == [{"id": "a"}]
        for app_err in (F.InstagramBlocked("app 403"), F.InstagramNotFound("app 404"), F.InstagramBadRequest("app 400")):
            monkeypatch.setattr(U, "_app_posts", _raise(app_err))
            with pytest.raises(type(err)) as info:
                U.get_posts({"username": "nike"})
            assert info.value is err


def test_a_grid_cursor_never_falls_back_to_the_app_feed(monkeypatch):
    # the app feed answers page 1 again for a max_id it cannot decode
    from instagram import users as U
    monkeypatch.setattr(U, "lookup", lambda user: {"pk": "1", "username": "nike"})
    monkeypatch.setattr(U, "_web_posts", _raise(F.InstagramBlocked("x")))
    monkeypatch.setattr(U, "_app_posts", lambda raw_user, cursor, window: pytest.fail("app feed asked with a grid cursor"))
    with pytest.raises(F.InstagramBlocked):
        U.get_posts({"username": "nike"}, cursor="AQHTEV_lmxEgvDx9KS")


def test_an_app_cursor_pages_the_app_feed_or_asks_for_a_restart(monkeypatch):
    from instagram import users as U
    monkeypatch.setattr(U, "lookup", lambda user: {"pk": "1", "username": "nike"})
    monkeypatch.setattr(U, "_web_posts", lambda raw_user, cursor, window: pytest.fail("grid asked with an app cursor"))
    monkeypatch.setattr(U, "_app_posts", lambda raw_user, cursor, window: ([{"id": "a"}], None, False, 1))
    assert U.get_posts({"username": "nike"}, cursor="4016750272540708224_13460080")["posts"] == [{"id": "a"}]
    monkeypatch.setattr(U, "_app_posts", _raise(F.InstagramBlocked("403")))
    with pytest.raises(F.InstagramBadRequest, match="first page again"):
        U.get_posts({"username": "nike"}, cursor="4016750272540708224_13460080")


# ---- users/posts: the logged-out grid, each post filled in ---------------------

def test_grid_page_is_filled_in_and_keeps_the_grid_play_count(monkeypatch):
    from instagram import users as U, queries as Q
    asked = {}

    def gql(name, retry_execution_errors=True, **variables):
        asked[name] = variables
        return _grid_fixture()

    full = {"De-XRdaPg2A": {"pk": "4016750272540708224", "code": "De-XRdaPg2A", "media_type": 2,
                            "product_type": "clips", "taken_at": 1793053465, "like_count": 51600,
                            "comment_count": 333, "video_versions": [{"url": "https://v/1.mp4", "width": 720, "height": 1280}]},
            "DeySgo5Bn_M": {"pk": "4013351625707388876", "code": "DeySgo5Bn_M", "media_type": 1,
                            "taken_at": 1792700000, "like_count": 289300, "comment_count": 1200}}

    def full_item(media_id, code):
        if code not in full:
            raise F.InstagramGated("401 on both post queries")
        assert full[code]["pk"] == media_id
        return full[code]

    monkeypatch.setattr(U, "graphql", gql)
    monkeypatch.setattr(U.posts_api, "full_item", full_item)
    posts, cursor, more, size = U._web_posts({"pk": "13460080", "username": "nike"}, None)
    assert asked[U.POSTS_GRID_QUERY] == {"first": 12, "username": "nike"}
    assert Q.variables_for(U.POSTS_GRID_QUERY)["__relay_internal__pv__PolarisSEOIsCrawlerrelayprovider"] is True
    assert [p["code"] for p in posts] == ["De-XRdaPg2A", "DeySgo5Bn_M", "Dek_RAKjMDJ"]
    reel, image, carousel = posts
    # filled in: date, comments, video from the full item; play count from the grid
    assert reel["published_at"] and reel["stats"]["comment_count"] == 333 and reel["video"]["link"] == "https://v/1.mp4"
    assert reel["stats"]["play_count"] == 3636501 and reel["product_type"] == "reel"
    assert image["stats"]["like_count"] == 289300 and image["stats"]["play_count"] is None
    # a post the full queries refused keeps its grid fields
    assert carousel["type"] == "carousel" and carousel["stats"]["like_count"] == 42505
    assert carousel["published_at"] is None and carousel["image"]
    assert cursor and cursor.startswith("AQ") and more is True and size == 3


def test_a_window_fills_in_only_the_posts_it_returns(monkeypatch):
    from instagram import users as U
    monkeypatch.setattr(U, "graphql", lambda name, retry_execution_errors=True, **v: _grid_fixture())
    filled = []

    def full_item(media_id, code):
        filled.append(code)
        raise F.InstagramGated("keep the grid fields")

    monkeypatch.setattr(U.posts_api, "full_item", full_item)
    monkeypatch.setattr(U, "lookup", lambda user: {"pk": "13460080", "username": "nike", "is_private": False})
    page = U.get_posts({"username": "nike"}, offset=1, limit=1)
    assert [p["code"] for p in page["posts"]] == ["DeySgo5Bn_M"] and filled == ["DeySgo5Bn_M"]
    assert page["post_count"] == 1 and page["page_post_count"] == 3 and page["next_cursor"].startswith("AQ")
    assert "page_post_count" not in U.get_posts({"username": "nike"})     # the REST answer is unchanged


def test_grid_cursor_errors_private_accounts_and_missing_users(monkeypatch):
    from instagram import users as U
    dead = {"xig_user_by_username": {"pk": "1", "id": "2", "polaris_ordered_timeline_connection": None}}
    monkeypatch.setattr(U, "graphql", lambda name, retry_execution_errors=True, **v: dead)
    with pytest.raises(F.InstagramBadRequest, match="invalid cursor"):
        U._grid_page({"pk": "1", "username": "nike"}, "garbage")
    assert U._grid_page({"pk": "1", "username": "x", "is_private": True}, None) == ([], None, False)
    with pytest.raises(F.InstagramUpstreamError):
        U._grid_page({"pk": "1", "username": "nike", "is_private": False}, None)
    monkeypatch.setattr(U, "graphql", lambda name, retry_execution_errors=True, **v: {"xig_user_by_username": None})
    with pytest.raises(F.InstagramNotFound):
        U._grid_page({"pk": "1", "username": "gone"}, None)


def test_post_details_fall_back_to_the_logged_out_root_when_the_full_query_refuses(monkeypatch):
    from instagram import posts as PO
    asked = []

    def gql(name, **v):
        asked.append(name)
        if name == "PolarisPostRootQuery":
            raise F.InstagramGated("PolarisPostRootQuery is login-gated (HTTP 401)")
        return {"xig_polaris_media": {"if_not_gated_logged_out": {"pk": "5", "code": "C"}}}

    monkeypatch.setattr(PO, "graphql", gql)
    assert PO.full_item("5", "C") == {"pk": "5", "code": "C"}
    assert asked == ["PolarisPostRootQuery", "PolarisLoggedOutDesktopWWWPostRootContentQuery"]


# ---- numeric user ids ------------------------------------------------------------

def test_id_lookup_asks_the_web_profile_query_first(monkeypatch):
    from instagram import users as U
    U._lookups.clear()
    asked, app_calls = {}, []

    def web(name, **variables):
        asked[name] = variables
        return {"user": {"username": "instagram", "pk": "25025320"}}

    monkeypatch.setattr(U, "mobile", lambda path, **kw: app_calls.append(path))
    monkeypatch.setattr(U, "graphql", web)
    monkeypatch.setattr(U, "_root_by_username", lambda name: {"pk": "25025320", "username": name})
    raw = U.lookup({"id": "25025320"})
    assert raw["username"] == "instagram"
    assert asked["PolarisProfilePageContentQuery"]["id"] == "25025320"
    assert app_calls == []                       # the app's users/<id>/info is not tried
    U._lookups.clear()


def test_id_lookup_missing_id_is_not_found(monkeypatch):
    from instagram import users as U
    U._lookups.clear()
    monkeypatch.setattr(U, "graphql", _raise(F.InstagramFieldException("field_exception")))
    with pytest.raises(F.InstagramNotFound):
        U.lookup({"id": "99999999999999"})


def test_id_lookup_uses_the_app_api_only_when_the_web_query_fails(monkeypatch):
    from instagram import users as U
    U._lookups.clear()
    monkeypatch.setattr(U, "graphql", _raise(F.InstagramRateLimited("Rate limit exceeded")))
    monkeypatch.setattr(U, "mobile", lambda path, **kw: {"user": {"username": "Nike"}})
    monkeypatch.setattr(U, "_root_by_username", lambda name: {"pk": "13460080", "username": name})
    assert U.lookup({"id": "13460080"})["username"] == "nike"
    U._lookups.clear()


# ---- "Rate limit exceeded" ---------------------------------------------------------

class _Resp:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status


def _fake_session(monkeypatch, bodies):
    sent = []

    class Sess:
        _ig_lsd, _ig_csrf = "AVtoken", "csrf"

        def post(self, url, data=None, headers=None, timeout=None):
            sent.append(data["fb_api_req_friendly_name"])
            return _Resp(bodies[min(len(sent), len(bodies)) - 1])

    monkeypatch.setattr(F, "_session", lambda: Sess())
    monkeypatch.setattr(F, "_escalate", lambda: None)
    monkeypatch.setattr(F.time, "sleep", lambda s: None)
    return sent


RATE_LIMITED = json.dumps({"errors": [{"message": "Rate limit exceeded", "severity": "CRITICAL", "code": 1675004}]})


def test_rate_limit_is_retried_like_a_block(monkeypatch):
    sent = _fake_session(monkeypatch, [RATE_LIMITED, json.dumps({"data": {"ok": True}})])
    escalated = []
    monkeypatch.setattr(F, "_escalate", lambda: escalated.append(1))
    assert F.graphql("PolarisLoggedOutDesktopWWWProfileRootContentQuery", username="nike") == {"ok": True}
    assert len(sent) == 2 and escalated == [1]


def test_rate_limit_fails_fast_where_it_is_a_standing_wall(monkeypatch):
    sent = _fake_session(monkeypatch, [RATE_LIMITED])
    with pytest.raises(F.InstagramRateLimited):
        F.graphql("PolarisLocationPageTabContentQuery", retry_rate_limit=False, location_id="1", tab="ranked")
    assert len(sent) == 1
    monkeypatch.setattr(config, "MAX_RETRIES", 3)
    sent.clear()
    with pytest.raises(F.InstagramRateLimited):
        F.graphql("PolarisLoggedOutDesktopWWWProfileRootContentQuery", username="nike")
    assert len(sent) == 3                          # retried, then given up


def test_location_posts_say_they_are_login_only(monkeypatch):
    from instagram import discovery as D
    monkeypatch.setattr(D, "graphql", _raise(F.InstagramRateLimited("Rate limit exceeded")))
    with pytest.raises(F.InstagramGated, match="logged-in users"):
        D.location_posts("212988663")
