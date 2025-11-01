# Claude Code OAuth - Reverse Engineered Snippets

Original code extracted from `claude.js` (v2.0.25).

## Files

- **config.js** - OAuth endpoints and client ID
- **pkce.js** - PKCE code verifier/challenge generation
- **callback-server.js** - HTTP server for localhost callback (class eR1)
- **auth-flow.js** - Main OAuth flow orchestration
- **token-exchange.js** - Token exchange and refresh

## Key Findings

**Dual Flow Architecture:**
- Automatic: `http://localhost:{random_port}/callback` (preferred)
- Manual: `https://console.anthropic.com/oauth/code/callback` (fallback)
- Both run simultaneously via Promise.race

**Authorization:**
- Endpoint: `https://claude.ai/oauth/authorize` (NOT console.anthropic.com!)
- Client ID: `9d1c250a-e61b-44d9-88ed-5944d1962f5e`
- PKCE: S256 method

**Token Exchange:**
- Endpoint: `https://console.anthropic.com/v1/oauth/token`
- Requires: `state` parameter (critical!)
- Redirect URI must match authorization request

**Success Redirect:**
- Full OAuth: `https://console.anthropic.com/oauth/code/success?app=claude-code`
- Inference-only: `https://claude.ai/oauth/code/success?app=claude-code`

**Port Selection:**
- Random port via `listen(0, "localhost")`
- OS assigns ephemeral port (49152-65535 range)

## Implementation

See `c2switcher/infrastructure/oauth.py` for working Python implementation.
