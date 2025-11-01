// Main OAuth Flow
// Source: claude.js lines 286667-286749

import OAuthCallbackServer from "./callback-server.js";
import { generateCodeVerifier, generateCodeChallenge, generateState } from "./pkce.js";
import { OAUTH_CONFIG } from "./config.js";

class OAuthFlow {
    constructor() {
        this.codeVerifier = generateCodeVerifier();
        this.authCodeListener = null;
        this.port = 0;
        this.manualAuthCodeResolver = null;
    }

    // Build authorization URL
    buildAuthorizeUrl({ codeChallenge, state, port, isManual, loginWithClaudeAi, inferenceOnly, orgUUID }) {
        const baseUrl = loginWithClaudeAi
            ? OAUTH_CONFIG.CLAUDE_AI_AUTHORIZE_URL
            : OAUTH_CONFIG.CONSOLE_AUTHORIZE_URL;

        const redirectUri = isManual
            ? OAUTH_CONFIG.MANUAL_REDIRECT_URL
            : `http://localhost:${port}/callback`;

        const scopes = inferenceOnly
            ? ["user:inference"]
            : OAUTH_CONFIG.SCOPES;

        const params = new URLSearchParams({
            code: "true",
            response_type: "code",
            client_id: OAUTH_CONFIG.CLIENT_ID,
            redirect_uri: redirectUri,
            scope: scopes.join(" "),
            code_challenge: codeChallenge,
            code_challenge_method: "S256",
            state: state,
        });

        if (orgUUID) {
            params.append("organization_uuid", orgUUID);
        }

        return `${baseUrl}?${params}`;
    }

    // Main flow: automatic + manual fallback
    async startOAuthFlow(displayUrlCallback, options) {
        // 1. Start HTTP server
        this.authCodeListener = new OAuthCallbackServer();
        this.port = await this.authCodeListener.start();

        // 2. Generate PKCE
        const codeChallenge = generateCodeChallenge(this.codeVerifier);
        const state = generateState();

        // 3. Build both URLs
        const params = {
            codeChallenge,
            state,
            port: this.port,
            loginWithClaudeAi: options?.loginWithClaudeAi,
            inferenceOnly: options?.inferenceOnly,
            orgUUID: options?.orgUUID,
        };

        const manualUrl = this.buildAuthorizeUrl({ ...params, isManual: true });
        const automaticUrl = this.buildAuthorizeUrl({ ...params, isManual: false });

        // 4. Wait for EITHER automatic OR manual
        const authCode = await this.waitForAuthorizationCode(state, async () => {
            await displayUrlCallback(manualUrl);  // Show fallback URL
            await openBrowser(automaticUrl);      // Open automatic URL
        });

        // 5. Check if we got automatic callback
        const wasAutomatic = this.authCodeListener?.hasPendingResponse() ?? false;

        try {
            // 6. Exchange code for tokens
            const tokens = await exchangeCodeForTokens(
                authCode,
                state,
                this.codeVerifier,
                this.port,
                wasAutomatic,
                options?.expiresIn
            );

            // 7. Redirect browser to success page (if automatic)
            if (wasAutomatic) {
                const scopes = tokens.scope.split(" ");
                this.authCodeListener?.handleSuccessRedirect(scopes);
            }

            return this.formatTokens(tokens);

        } catch (err) {
            if (wasAutomatic) {
                this.authCodeListener?.handleErrorRedirect();
            }
            throw err;

        } finally {
            this.authCodeListener?.close();
        }
    }

    // Wait for code from EITHER source
    async waitForAuthorizationCode(state, openBrowserCallback) {
        return new Promise((resolve, reject) => {
            // Manual resolver (for paste)
            this.manualAuthCodeResolver = resolve;

            // Automatic resolver (for HTTP callback)
            this.authCodeListener
                .waitForAuthorization(state, openBrowserCallback)
                .then(code => {
                    this.manualAuthCodeResolver = null;
                    resolve(code);
                })
                .catch(reject);
        });
    }

    // Handle manual code input
    handleManualAuthCodeInput(input) {
        if (this.manualAuthCodeResolver) {
            this.manualAuthCodeResolver(input.authorizationCode);
            this.manualAuthCodeResolver = null;
            this.authCodeListener?.close();
        }
    }

    formatTokens(tokenData) {
        return {
            accessToken: tokenData.access_token,
            refreshToken: tokenData.refresh_token,
            expiresAt: Date.now() + tokenData.expires_in * 1000,
            scopes: tokenData.scope.split(" "),
            subscriptionType: tokenData.subscription_type,
        };
    }

    cleanup() {
        this.authCodeListener?.close();
        this.manualAuthCodeResolver = null;
    }
}

module.exports = OAuthFlow;
