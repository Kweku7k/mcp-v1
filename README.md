# Presto MCP servers

MCP servers for Presto products, one Python file each. A server wraps one product's HTTP API as tools
an AI can call; the product itself doesn't change. Clients are Claude (admin/staff tools, added as a
custom connector) and Q, Presto's WhatsApp AI (customer tools, see `QV2/docs/connect-your-mcp-to-q.md`).

All servers use FastMCP over streamable HTTP and serve MCP at `/mcp`.

## Setup

```bash
uv python install 3.13
uv sync
```

Each server reads its settings from the environment (or a `.env` file next to it).

## Servers

| File | Product | Client | Auth | Default port |
|---|---|---|---|---|
| `votes_mcp.py` | PrestoVotes, read-only admin data | Claude | OAuth via PrestoVotes | 8011 |
| `votes_voter_mcp.py` | PrestoVotes, voters on WhatsApp | Q | Signed `X-Q-Context` JWT from Q | 8012 |
| `stay_mcp.py` | Presto Stay, property managers | Claude | Stay API key | 8010 |
| `stay_tenants_mcp.py` | Presto Stay, a single tenant | — | Tenant bearer token | 8010 |

Both Stay servers listen on 8010, so only one of them can run on a host at a time.

### `votes_mcp.py`: PrestoVotes admins (Claude)

Read-only tools: `whoami`, `get_platform_overview`, `list_events`, `get_event`, `get_categories`,
`get_nominees`, `get_event_transactions`, `search_transactions`, `list_organisers`. What a caller
sees is decided by PrestoVotes:

- Presto staff see the whole platform.
- An organiser sees only their own event, and voter phone numbers come back masked.

People connect from `prestovotes.com/connect-claude`, which signs them in through PrestoVotes OAuth.
This server only verifies the token, by calling PrestoVotes `/api/v1/mcp/me`. Keys from
`flask mcp-key create` also work as bearer tokens.

| Env | Example |
|---|---|
| `PRESTO_VOTES_URL` | `https://prestovotes.com` |
| `PRESTO_VOTES_MCP_API_URL` | defaults to `$PRESTO_VOTES_URL/api/v1/mcp` |
| `MCP_PUBLIC_URL` | `https://mcp.prestovotes.com`, the URL Claude reaches this server on |
| `HOST`, `PORT` | `127.0.0.1`, `8011` |

```bash
uv run votes_mcp.py
```

### `votes_voter_mcp.py`: PrestoVotes voters (WhatsApp via Q)

Tools:
- `find_events`, `get_event` and `find_nominees` look things up.
- `start_vote` sends a mobile-money prompt, or returns a card checkout link.
- `check_my_votes`, `nominate` and `file_complaint` act for the voter.

There is also a `q://context` resource with the voter's pending payments.

Every request must carry `X-Q-Context`, a short-lived ES256 JWT that Q signs with the sender's
verified WhatsApp number. The server checks it against Q's JWKS, along with `aud`, `iss` and
expiry, and rejects anything else. A voter can only act as themselves.

This server calls the PrestoVotes voter API (`/api/v1/voter`) with a platform key. Create the key on
PrestoVotes with `flask mcp-key create --platform --label voter-mcp`.

| Env | Example |
|---|---|
| `PRESTO_VOTES_URL` | `https://prestovotes.com` |
| `PRESTO_VOTES_VOTER_KEY` | `pvk_...` (the voter-mcp platform key) |
| `Q_JWKS_URL` | `https://q.prestoghana.com/.well-known/jwks.json` |
| `Q_ISSUER` | `https://q.prestoghana.com` |
| `VOTER_MCP_PUBLIC_URL` | `https://voters.mcp.prestovotes.com/mcp`. It must match the MCP URL saved on the Q agent, because it is checked as the token's `aud`. |
| `HOST`, `PORT` | `127.0.0.1`, `8012` |

```bash
uv run votes_voter_mcp.py
```

### `stay_mcp.py`: Presto Stay managers (Claude)

Tools: `set_stay_api_key`, `get_rooms`, `get_tenants`, `assign_room`, `unassign_room`. They are
scoped to the listing that owns the API key.

| Env | Example |
|---|---|
| `STAY_MCP_API_URL` | `https://stay.prestoghana.com/api/v1/mcp` |
| `STAY_API_KEY` | the listing's Stay API key (can also be set at runtime with `set_stay_api_key`) |

```bash
uv run stay_mcp.py
```

### `stay_tenants_mcp.py`: a Stay tenant

Tools: `get_my_profile`, `get_my_roommates`, `get_payment_guidance`, `get_payment_link`,
`file_complaint`. One process serves one tenant session. The tenant's token comes from Stay's OTP
flow, which runs in the WhatsApp backend, not here.

| Env | Example |
|---|---|
| `STAY_API_BASE_URL` | `https://stay.prestoghana.com` |
| `STAY_TENANT_TOKEN` | the tenant's bearer token |

## Deploying a server

Run each server under systemd, bound to `127.0.0.1`, behind nginx with TLS. For example, for the
voter server:

```ini
# /etc/systemd/system/votes-voter-mcp.service
[Unit]
Description=PrestoVotes voter MCP
After=network.target

[Service]
WorkingDirectory=/path/to/mcp-v1
EnvironmentFile=/path/to/mcp-v1/votes_voter.env
ExecStart=/root/.local/bin/uv run votes_voter_mcp.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```nginx
server {
    server_name voters.mcp.prestovotes.com;
    location / {
        proxy_pass http://127.0.0.1:8012;
        proxy_set_header Host 127.0.0.1:8012;   # MCP's DNS-rebinding check expects a local Host
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_buffering off;                    # streamable HTTP
        proxy_read_timeout 300s;
    }
}
```

Then run `sudo certbot --nginx -d voters.mcp.prestovotes.com`.

## Adding a server

Add a new `<product>_mcp.py` that calls the product's API. Pick an unused port and add the file to
the table above. For WhatsApp customers, verify `X-Q-Context` the way `votes_voter_mcp.py` does,
and never trust a phone number passed as a tool argument.

## Experiments

`main.py` (sticky notes), `tyt.py` (`add` demo), `server.py` with `presto_client.py` and `config.py`
(an early PrestoQ orders test), and `notes.txt` are learning scratch files. Nothing depends on them.
