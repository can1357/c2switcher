// OAuth Configuration
// Source: claude.js (extracted from environment config)

const OAUTH_CONFIG = {
    // Authorization endpoints
    CONSOLE_AUTHORIZE_URL: "https://console.anthropic.com/oauth/authorize",
    CLAUDE_AI_AUTHORIZE_URL: "https://claude.ai/oauth/authorize",  // ← DEFAULT

    // Token exchange
    TOKEN_URL: "https://console.anthropic.com/v1/oauth/token",

    // API endpoints
    API_KEY_URL: "https://api.anthropic.com/api/oauth/claude_cli/create_api_key",
    ROLES_URL: "https://api.anthropic.com/api/oauth/claude_cli/roles",
    BASE_API_URL: "https://api.anthropic.com",

    // Success redirects
    CONSOLE_SUCCESS_URL: "https://console.anthropic.com/buy_credits?returnUrl=/oauth/code/success%3Fapp%3Dclaude-code",
    CLAUDEAI_SUCCESS_URL: "https://console.anthropic.com/oauth/code/success?app=claude-code",

    // Manual redirect (for copy/paste flow)
    MANUAL_REDIRECT_URL: "https://console.anthropic.com/oauth/code/callback",

    // Client credentials
    CLIENT_ID: "9d1c250a-e61b-44d9-88ed-5944d1962f5e",

    // Default scopes
    SCOPES: ["org:create_api_key", "user:profile", "user:inference"],
};

// User-Agent header
function getUserAgent() {
    return `claude-code/2.0.25`;
}

module.exports = { OAUTH_CONFIG, getUserAgent };
