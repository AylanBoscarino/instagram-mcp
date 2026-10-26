# instagram-mcp

![Demo](docs/demo.gif)

[![PyPI version](https://img.shields.io/pypi/v/instagram-mcp.svg)](https://pypi.org/project/instagram-mcp/)
[![Python versions](https://img.shields.io/pypi/pyversions/instagram-mcp.svg)](https://pypi.org/project/instagram-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A [Model Context Protocol](https://modelcontextprotocol.io) server that wraps the
**Instagram Graph API** so Claude (or any MCP client) can read, publish, comment,
DM, and pull insights from an Instagram **Business** or **Creator** account.

24 tools across five capability areas — profile/media, publishing, comments, DMs,
and insights — built on FastMCP + httpx.

## Quick install

```bash
pip install instagram-mcp
```

Or with `uv`:

```bash
uv tool install instagram-mcp
```

## Setup

### 1. Create a Meta App

1. Go to https://developers.facebook.com/apps → **Create app** → use case **Other** → type **Business**.
2. In the app dashboard, add the **Instagram** product (Add Product → Instagram → Set up).

### 2. Link an Instagram Business/Creator account

You need an Instagram account switched to **Business** or **Creator**, linked to a
Facebook **Page**. In the app dashboard, walk through **Instagram → API setup with
Facebook login → Step 1: Generate access tokens** and link your account.

### 3. Get the required permissions

Generate a token with these scopes (in Graph API Explorer or the Instagram API
setup page):

- `instagram_basic`
- `instagram_content_publish`
- `instagram_manage_comments`
- `instagram_manage_messages`
- `instagram_manage_insights`
- `pages_show_list`
- `pages_read_engagement`
- `business_management`

> While your app is in **Development mode**, only accounts in your app's Roles
> list (admins/developers/testers) can authenticate. That's fine for personal use.
> For other users, you need **App Review** with Advanced Access.

### 4. Mint a long-lived Page token

The fastest way: run the bundled helper.

```bash
instagram-mcp-get-token
```

It will ask for your short-lived user token + app ID/secret, exchange it for a
long-lived user token, list your linked IG accounts, and write `.env` for you.

Manual alternative:

```bash
# Exchange short-lived user token → long-lived (~60 days)
curl -G "https://graph.facebook.com/v21.0/oauth/access_token" \
  --data-urlencode "grant_type=fb_exchange_token" \
  --data-urlencode "client_id=YOUR_APP_ID" \
  --data-urlencode "client_secret=YOUR_APP_SECRET" \
  --data-urlencode "fb_exchange_token=SHORT_LIVED_TOKEN"

# Find your Pages and their IG accounts
curl -G "https://graph.facebook.com/v21.0/me/accounts" \
  --data-urlencode "fields=name,instagram_business_account,access_token" \
  --data-urlencode "access_token=LONG_LIVED_USER_TOKEN"
```

Use the **Page's** `access_token` (never expires) and the
`instagram_business_account.id` of the linked IG account.

### 5. Configure `.env`

```bash
IG_USER_ID=17841446575432302
IG_ACCESS_TOKEN=EAAxxxxxxxxxxxxxxxxxxx
IG_GRAPH_VERSION=v21.0
IG_GRAPH_HOST=graph.facebook.com
```

Set `IG_GRAPH_HOST=graph.instagram.com` if your token came from the **Instagram
Login** path instead of Facebook Login.

## Connect to Claude Desktop

Edit `~/.config/Claude/claude_desktop_config.json` (Linux) or
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "instagram": {
      "command": "instagram-mcp",
      "env": {
        "IG_USER_ID": "17841446575432302",
        "IG_ACCESS_TOKEN": "EAAxxxxxxxxxxxxxxxxxxx"
      }
    }
  }
}
```

Restart Claude Desktop. Read tools work by default. Write tools need an explicit
local opt-in. Add `IG_ALLOW_WRITES=true` to enable publishing, replies, comment
visibility changes, and DMs. Comment deletion also needs
`IG_ALLOW_DESTRUCTIVE=true`.

These settings must come from local configuration. A model request cannot enable
them. Keep them unset for read-only use.

## Available tools

### Profile & media

| Tool | What it does |
|---|---|
| `get_my_profile` | Profile info: bio, followers, media count, etc. |
| `list_my_media` | One page of recent posts |
| `list_all_media` | Auto-paginate through all media |
| `get_media` | Fetch a single media item |
| `list_tagged_media` | Posts the account is tagged in |
| `list_stories` | Currently-live stories (24h window) |

### Hashtags

| Tool | What it does |
|---|---|
| `search_hashtag` | Resolve a `#tag` to its ID |
| `hashtag_top_media` | Top-ranked posts for a hashtag |
| `hashtag_recent_media` | Recent posts for a hashtag (24h window) |

### Publishing

| Tool | What it does |
|---|---|
| `publish_image` | Single image post from a public URL |
| `publish_reel` | Reel (waits for container processing) |
| `publish_story` | Image or video story |
| `publish_carousel` | 2-10 item carousel |
| `get_publish_limit` | Show 24h publish quota usage |

### Comments

| Tool | What it does |
|---|---|
| `list_comments` | Top-level comments + nested replies |
| `get_comment_replies` | Replies under a specific comment |
| `reply_to_comment` | Post a reply |
| `hide_comment` | Hide / unhide |
| `delete_comment` | Delete a comment you own |

### Direct messages

| Tool | What it does |
|---|---|
| `list_conversations` | DM conversations |
| `get_conversation` | Messages in a conversation |
| `send_dm` | Send a DM (optionally with a `message_tag`) |

### Insights

| Tool | What it does |
|---|---|
| `get_account_insights` | Account-level metrics with optional `metric_type` |
| `get_media_insights` | Per-media insights |

## Notes & gotchas

- **Publishing requires public HTTPS URLs.** Meta fetches media server-side. Host
  your images/videos on a publicly accessible URL first.
- **Publish quota is 100 posts per 24h rolling window** on most Business accounts.
- **`send_dm` is response-only by default** — it only works inside the 24h
  user-initiated window. Pass a `message_tag` (e.g. `HUMAN_AGENT`) to send
  outside that window. Tags require Meta approval.
- **Some insight metrics need `metric_type='total_value'`** (Meta tightened this
  in 2024): `views`, `accounts_engaged`, `total_interactions`, `profile_views`,
  `likes`, `comments`, `shares`, `saves`. `reach` and `follower_count` don't.
- **Errors return a structured dict**, not exceptions. Look for `error: true`
  along with `status`, `message`, `code`, `subcode`, and `fbtrace_id` in the
  response — that's the Graph API error, not a Python traceback.

## Development

```bash
git clone https://github.com/AleemHaider/instagram-mcp
cd instagram-mcp
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Run the server in read-only mode:

```bash
IG_USER_ID=YOUR_NUMERIC_ACCOUNT_ID IG_ACCESS_TOKEN=YOUR_TOKEN instagram-mcp
```

Enable authorized writes in a trusted local shell:

```bash
IG_USER_ID=YOUR_NUMERIC_ACCOUNT_ID IG_ACCESS_TOKEN=YOUR_TOKEN \\
  IG_ALLOW_WRITES=true instagram-mcp
```

Enable comment deletion only when needed:

```bash
IG_ALLOW_WRITES=true IG_ALLOW_DESTRUCTIVE=true instagram-mcp
```

The token helper creates `.env` with mode `0600` on Unix. It does not overwrite
an existing `.env`. Move an existing file to a protected backup before setup.

## License

MIT — see [LICENSE](LICENSE).
