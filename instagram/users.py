"""User endpoints: resolve, profile, posts, reels, highlights and similar
accounts.

`user` is {"id": ..., "username": ...} from refs.resolve_user (a username,
@username, numeric id or profile link). Every endpoint first looks the
user up with the logged-out profile root query (an ~8 KB answer by
username; a numeric id is first turned into the username with the web
profile query), which both proves the account exists (a null root is a
404) and supplies the numeric id / Facebook id the other calls need.
Lookups are memoised in-process for LOOKUP_TTL seconds so a paging client
does not pay for it on every page.

Surfaces:
  posts   the logged-out profile grid,
          PolarisLoggedOutDesktopWWWProfilePostsTabContentQuery: 12 thin
          posts per page (code, caption, image, like + play counts) and a
          cursor ("AQ…") that pages the whole timeline. Each post is then
          filled in with its full media item (posts.full_item:
          PolarisPostRootQuery, FANOUT_WORKERS at a time), so a page
          carries every field; the grid's play count is kept (the full
          item has none for reels). Since 2026-10-28 the web
          PolarisProfilePostsQuery answers 401 "Please wait a few minutes"
          (require_login) to anonymous callers and the app's
          feed/user/<username>/username/ answered 403 in testing; the app
          feed stays as the first-page fallback, and still pages an app
          cursor ("<media_pk>_<user_pk>") handed out before the switch.
  reels   the app's POST clips/user/ — 12 reels per page, opaque max_id.
          Fallback (first page only: the logged-out reels tab has no
          pagination query): PolarisLoggedOutDesktopWWWProfileReelsTabContentQuery,
          thin items with like / comment / play counts.
  highlights  the tray preloaded with the profile root (first page; its
          pagination query is login-walled).
  similar     PolarisAYMLFollowChainingListLoggedOutQuery, up to 44
          accounts (empty for some large accounts upstream).
"""
import re
import threading
import time

from . import parsers as P
from . import posts as posts_api
from .fetch import (
    InstagramBadRequest, InstagramExecutionError, InstagramFieldException, InstagramNotFound,
    InstagramUpstreamError, graphql, mobile, run_parallel,
)
from .ssr import og_counts

LOOKUP_TTL = 600
PAGE_SIZE = 12
INVALID_CURSOR = "invalid cursor: pass the next_cursor value from the previous page unchanged"
POSTS_GRID_QUERY = "PolarisLoggedOutDesktopWWWProfilePostsTabContentQuery"
# the app feed's next_max_id; the grid's cursors are opaque "AQ…" strings
_APP_CURSOR_RE = re.compile(r"\d+_\d+")
STALE_APP_CURSOR = ("this cursor is from an earlier page format that can no longer be continued: "
                    "request the first page again without a cursor")

_lookup_lock = threading.Lock()
_lookups = {}           # ("id"|"username", value) -> (expires, raw root user)


def _describe(user):
    return f"@{user['username']}" if user.get("username") else f"user id {user.get('id')}"


def _root_by_username(username):
    data = graphql("PolarisLoggedOutDesktopWWWProfileRootContentQuery", username=username)
    raw = data.get("xig_user_by_username")
    return raw if isinstance(raw, dict) and raw.get("pk") else None


def _username_by_id(user_id):
    """Numeric user id -> username, None when no such account.

    The web profile query answers by id and carries the username; a missing
    id answers field_exception (ids 1 and 99999999999999, 2026-10-16). The
    app API's users/<id>/info answered "We're sorry, but something went
    wrong" (status fail) to every unsigned call on 2026-10-16, so it is only
    the second opinion when the web query itself fails."""
    try:
        data = graphql("PolarisProfilePageContentQuery", id=user_id, enable_integrity_filters=True)
    except InstagramFieldException:
        return None
    except InstagramUpstreamError:
        try:
            info = mobile(f"users/{user_id}/info/")
        except InstagramNotFound:
            return None
        return P.clean(P._dict(info.get("user")).get("username"))
    return P.clean(P._dict(P._dict(data).get("user")).get("username"))


def lookup(user):
    """The raw profile root for a resolved ref; InstagramNotFound if the
    account does not exist."""
    key = ("username", user["username"]) if user.get("username") else ("id", str(user.get("id")))
    now = time.time()
    with _lookup_lock:
        hit = _lookups.get(key)
        if hit and hit[0] > now:
            return hit[1]
    if key[0] == "username":
        raw = _root_by_username(key[1])
    else:
        username = _username_by_id(key[1])
        raw = _root_by_username(username.lower()) if username else None
    if raw is None:
        raise InstagramNotFound(f"{_describe(user)} not found on Instagram")
    with _lookup_lock:
        if len(_lookups) > 5000:
            _lookups.clear()
        _lookups[key] = (now + LOOKUP_TTL, raw)
        _lookups[("id", str(raw.get("pk")))] = (now + LOOKUP_TTL, raw)
        if raw.get("username"):
            _lookups[("username", str(raw["username"]).lower())] = (now + LOOKUP_TTL, raw)
    return raw


def resolve(user):
    """Username <-> numeric id, with the basic account facts."""
    raw = lookup(user)
    out = P.user_ref(raw)
    out["facebook_id"] = P.to_id(raw.get("id")) if str(raw.get("id")) != str(raw.get("pk")) else None
    out["follower_count"] = P.to_int(raw.get("follower_count"))
    out["following_count"] = P.to_int(raw.get("following_count"))
    return out


def _quiet(fn):
    """Run an enrichment call; its failure must not fail the profile."""
    try:
        return fn()
    except (InstagramUpstreamError, InstagramBadRequest, InstagramNotFound):
        return None


def get_profile(user):
    """The full profile: bio, bio links, category, counts, highlights."""
    raw = lookup(user)
    pk = str(raw.get("pk"))
    username = str(raw.get("username"))

    def extra():
        data = graphql("PolarisProfilePageContentQuery", id=pk, enable_integrity_filters=True)
        return data.get("user")

    def og():
        from .fetch import get_page
        html, _ = get_page(f"/{username}/", label=f"@{username}")
        return og_counts(html)

    extra_raw, counts = run_parallel([lambda: _quiet(extra), lambda: _quiet(og)])
    parsed = P.profile(raw, extra_raw, counts)
    if parsed is None:
        raise InstagramNotFound(f"{_describe(user)} not found on Instagram")
    return parsed


# ---- posts ---------------------------------------------------------------------------------

def _grid_page(raw_user, cursor):
    """One page of the logged-out posts grid -> (thin raw nodes, next_cursor,
    has_more). A cursor the grid cannot decode (garbage, or another
    surface's) answers a null connection with field_exception."""
    variables = {"first": PAGE_SIZE, "username": str(raw_user.get("username"))}
    if cursor:
        variables["after"] = cursor
    try:
        data = graphql(POSTS_GRID_QUERY, retry_execution_errors=not cursor, **variables)
    except (InstagramExecutionError, InstagramFieldException):
        if cursor:
            raise InstagramBadRequest(INVALID_CURSOR)
        raise
    root = data.get("xig_user_by_username")
    if not isinstance(root, dict):
        raise InstagramNotFound(f"@{raw_user.get('username')} not found on Instagram")
    connection = root.get("polaris_ordered_timeline_connection")
    if not isinstance(connection, dict):
        if cursor:
            raise InstagramBadRequest(INVALID_CURSOR)
        if P.to_bool(raw_user.get("is_private")):
            return [], None, False
        raise InstagramUpstreamError(f"{POSTS_GRID_QUERY} answered no timeline")
    nodes = [P._dict(edge).get("node") for edge in P._list(connection.get("edges"))]
    nodes = [n for n in nodes if isinstance(n, dict) and (n.get("pk") or n.get("code"))]
    next_cursor, has_more = P.page_info(connection)
    return nodes, next_cursor, has_more


def _filled_in(node):
    """A grid node -> its full media item, keeping the grid's play count
    (the full item carries none for reels). A post the full queries cannot
    serve keeps its grid fields rather than failing the page."""
    code = P.clean(node.get("code"))
    media_id = P.to_id(node.get("pk"))
    if not code or not media_id:
        return node
    try:
        item = posts_api.full_item(media_id, code)
    except Exception as e:      # one post must not fail the page
        print(f"instagram users/posts: post {code} kept its grid fields ({type(e).__name__}: {e})")
        return node
    item = dict(item)
    if P._play_count(item) is None and P.to_int(node.get("play_count")) is not None:
        item["play_count"] = node["play_count"]
    return item


def _web_posts(raw_user, cursor):
    nodes, next_cursor, has_more = _grid_page(raw_user, cursor)
    items = run_parallel([lambda node=node: _filled_in(node) for node in nodes])
    posts = P.media_list(items, owner=P.user_ref(raw_user))
    return posts, next_cursor, has_more


def _app_posts(raw_user, cursor):
    username = str(raw_user.get("username"))
    params = {"count": PAGE_SIZE}
    if cursor:
        params["max_id"] = cursor
    try:
        data = mobile(f"feed/user/{username}/username/", params=params)
    except InstagramBadRequest:
        if cursor:
            raise InstagramBadRequest(INVALID_CURSOR)
        raise
    posts = P.media_list(data.get("items"), owner=P.user_ref(raw_user))
    has_more = bool(P.to_bool(data.get("more_available")))
    next_cursor = P.clean(data.get("next_max_id")) if has_more else None
    return posts, next_cursor, has_more


def get_posts(user, cursor=None):
    """The user's timeline (posts, reels shared to the grid, carousels),
    newest first, 12 per page."""
    raw_user = lookup(user)
    if cursor and _APP_CURSOR_RE.fullmatch(cursor):
        # an app-feed cursor (handed out before 2026-10-28) only pages the
        # app feed; the grid cannot decode it
        try:
            posts, next_cursor, has_more = _app_posts(raw_user, cursor)
        except InstagramUpstreamError:
            raise InstagramBadRequest(STALE_APP_CURSOR)
    else:
        try:
            posts, next_cursor, has_more = _web_posts(raw_user, cursor)
        except InstagramUpstreamError as grid_error:
            # the app feed cannot continue a grid cursor (it would answer
            # page 1 again), so it only stands in for a first page. Any
            # failure of the stand-in surfaces the grid's own (retryable)
            # error: the lookup just proved the account exists, so an app
            # 404 / 400 must not turn a cursor-less page 1 into one.
            if cursor:
                raise
            try:
                posts, next_cursor, has_more = _app_posts(raw_user, None)
            except (InstagramUpstreamError, InstagramBadRequest, InstagramNotFound):
                raise grid_error
    if P.to_bool(raw_user.get("is_private")) and not posts:
        has_more = False
    return {
        "user": P.user_ref(raw_user),
        "is_private": P.to_bool(raw_user.get("is_private")),
        "post_count": len(posts),
        "posts": posts,
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


# ---- reels ---------------------------------------------------------------------------------

def _web_reels(raw_user):
    data = graphql("PolarisLoggedOutDesktopWWWProfileReelsTabContentQuery", first=PAGE_SIZE,
                   username=str(raw_user.get("username")))
    root = P._dict(data.get("xig_user_by_username"))
    connection = P._dict(root.get("polaris_clips_connection"))
    reels = P.media_list(connection.get("edges"), owner=P.user_ref(raw_user))
    for reel in reels:
        reel["product_type"] = reel["product_type"] or "reel"
        reel["link"] = reel["link"] or None
    _, has_more = P.page_info(connection)
    return reels, None, has_more


def get_reels(user, cursor=None):
    """The user's reels (the Reels tab), newest first, 12 per page."""
    raw_user = lookup(user)
    pk = str(raw_user.get("pk"))
    form = {"target_user_id": pk, "page_size": str(PAGE_SIZE), "include_feed_video": "true"}
    if cursor:
        form["max_id"] = cursor
    try:
        data = mobile("clips/user/", data=form)
    except InstagramBadRequest:
        if cursor:
            raise InstagramBadRequest(INVALID_CURSOR)
        raise
    except InstagramUpstreamError:
        if cursor:
            raise     # app-API cursors (QVFB…) have no web equivalent
        reels, next_cursor, has_more = _web_reels(raw_user)
    else:
        reels = P.media_list(data.get("items"), owner=P.user_ref(raw_user))
        paging = P._dict(data.get("paging_info"))
        has_more = bool(P.to_bool(paging.get("more_available")))
        next_cursor = P.clean(paging.get("max_id")) if has_more else None
    return {
        "user": P.user_ref(raw_user),
        "is_private": P.to_bool(raw_user.get("is_private")),
        "reel_count": len(reels),
        "reels": reels,
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


# ---- highlights / similar ------------------------------------------------------------------

def get_highlights(user):
    """The profile's highlights tray (id, title, cover). Items of one
    highlight: /instagram/highlights/items."""
    raw_user = lookup(user)
    tray = P._dict(raw_user.get("lox_highlights_connection"))
    highlights = [h for h in (P.highlight_ref(P._dict(e).get("node")) for e in P._list(tray.get("edges"))) if h]
    _, has_more = P.page_info(tray)
    return {
        "user": P.user_ref(raw_user),
        "highlight_count": len(highlights),
        "highlights": highlights,
        "has_more": has_more,
    }


def get_similar(user):
    """Accounts Instagram suggests next to this one ("accounts you might
    like"), up to 44."""
    raw_user = lookup(user)
    pk = str(raw_user.get("pk"))
    data = graphql("PolarisAYMLFollowChainingListLoggedOutQuery", owner_id=pk)
    users = P._users(P._dict(data.get("xdt_ayml_logged_out")).get("users"))
    if not users:
        fbid = str(raw_user.get("id") or pk)
        data = _quiet(lambda: graphql("PolarisLoggedOutDesktopWWWAYMLQuery", id=fbid)) or {}
        root = data.get("ayml_logged_out") or P._dict(data.get("xdt_ayml_logged_out")).get("users")
        users = P._users(root if isinstance(root, list) else P._dict(root).get("users"))
    return {
        "user": P.user_ref(raw_user),
        "similar_count": len(users),
        "similar_users": users,
    }
