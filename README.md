# Instagram Scraper MCP

A read-only MCP server for public Instagram content, built on a fork of
[omkarcloud/instagram-scraper](https://github.com/omkarcloud/instagram-scraper).
It talks to instagram.com's own logged-out web and app APIs over plain HTTP:
no account, no API key, no browser.

It runs locally over stdio for an MCP client, and on the homeserver over
streamable HTTP for Fami.

## Tools

| Tool | Input | Returns |
|---|---|---|
| `get_post` | `post` | One post or reel: caption, counts, author, media, carousel slides, location, tagged users, coauthors, audio. |
| `get_post_comments` | `post`, `cursor` | Comments newest first, roughly 4-15 per page. No replies to comments. |
| `get_profile` | `username` | Bio, bio links, category, follower / following / post counts, verified badge, highlights. |
| `get_profile_posts` | `username`, `cursor` | The user's grid posts, newest first, 12 per page. |
| `get_profile_reels` | `username`, `cursor` | The user's reels with play counts and audio, 12 per page. |
| `search_posts` | `query`, `cursor` | Top reels for a keyword (`/popular/<keyword>/`) and related keywords, 12 per page. |
| `get_hashtag_posts` | `tag`, `cursor` | Top reels for a hashtag, 12 per page. |

- `post` takes a shortcode (`DdcI4o0Prsz`), a numeric media id, or an
  instagram.com post, reel or share link.
- `username` takes a username, `@username`, a numeric user id, or a profile link.
- `tag` takes a hashtag with or without `#`, or an `/explore/tags/` link.
- Paged tools return `next_cursor` and `has_more`. Pass `next_cursor` back as
  `cursor` for the next page.
- Results are the scraper's JSON with null and empty fields left out. Avatar
  URLs (`profile_picture`) are dropped too: they repeat on every post owner
  and comment author.
- Media comes back as URLs (`image`, `video`, carousel `items[]`). The
  Instagram CDN URLs are signed and expire, so the client should download
  them soon after the call.
- Failures come back as an MCP tool error (`isError: true`) with
  `{"error": {"code", "message", "retryable"}}`. Codes: `invalid_input`,
  `not_found`, `upstream_rejected` (a login wall, a rate limit or a 400 from
  Instagram), `timeout`, `upstream_unavailable`, `internal_error`.

## Run locally (stdio)

Requires Python 3.12.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Point your MCP client at the venv's Python with absolute paths:

```json
{
  "mcpServers": {
    "instagram": {
      "command": "/absolute/path/to/instagram-scraper/.venv/bin/python",
      "args": ["/absolute/path/to/instagram-scraper/mcp_server.py", "--stdio"]
    }
  }
}
```

Or with Claude Code:

```bash
claude mcp add instagram -- /absolute/path/to/instagram-scraper/.venv/bin/python /absolute/path/to/instagram-scraper/mcp_server.py --stdio
```

Without `--stdio` the server speaks streamable HTTP on
`http://127.0.0.1:8000/mcp`.

## Deploy on the homeserver

The compose file joins the external `ai-routing` Docker network and
publishes no ports, so only containers on that network can reach it.

```bash
scripts/deploy
```

It pulls the current branch, creates `ai-routing` if it is missing, rebuilds
the image, restarts the container, waits for the healthcheck, checks the MCP
tool list and removes the image it replaced. `scripts/deploy --no-pull`
deploys the checkout as it is. From a laptop:
`ssh homeserver instagram-scraper/scripts/deploy`.

Fami (OpenClaw) then connects over streamable HTTP:

```json
"instagram-scraper": {
  "enabled": true,
  "url": "http://instagram-scraper:8000/mcp",
  "transport": "streamable-http",
  "toolFilter": {
    "include": ["get_post", "get_post_comments", "get_profile", "get_profile_posts",
                "get_profile_reels", "search_posts", "get_hashtag_posts"]
  }
}
```

The container runs as `nobody` on a read-only filesystem, capped at 256 MB
of memory, half a CPU and 64 processes. `GET /health` backs the Docker
healthcheck.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MCP_HOST` | `127.0.0.1` (`0.0.0.0` in the image) | HTTP bind address |
| `PORT` | `8000` | HTTP port |
| `MCP_TOOL_TIMEOUT` | `45` | Seconds one tool call may spend on instagram.com, retries included |
| `MCP_MAX_CONCURRENCY` | `4` | Tool calls running at once |
| `MCP_CACHE_SIZE` | `500` | Results kept in the in-memory cache (TTLs in `instagram/cache_config.py`) |
| `INSTAGRAM_PROXY` | none | Proxy for every upstream request, e.g. `http://user:pass@host:port` |

## REST API and Python use

The upstream REST API is still in the repo but is not started by the image.
Run it locally with `python run.py` (19 endpoints on
`http://localhost:8000`, listed in `routes.py`), or call the functions
directly as `main.py` shows. The MCP server leaves out the endpoints that
are login-gated or not useful to an agent: location posts, audio, explore,
highlights, similar accounts, the id resolvers and `/posts/media` (every
image size; `get_post` already returns the best one).

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

The tests run offline against captured payloads in `instagram/fixtures/`.

## Limits

Everything comes from the logged-out side of instagram.com. Private
accounts, stories, comment replies, likers and location posts sit behind a
login wall. Comments page newest first only, and some posts stop paging
early. The GraphQL `doc_id`s in `instagram/queries.py` are the web app's
persisted queries; when Meta rotates them, the affected calls fail with
`upstream_unavailable` until they are re-harvested.

## License

MIT, see [LICENSE](LICENSE). Use it in line with the laws that apply to you
and Instagram's terms; see [SECURITY.md](SECURITY.md).
