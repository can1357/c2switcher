// Token Exchange and Refresh
// Source: claude.js lines 245380-245448

import axios from "axios";
import { OAUTH_CONFIG } from "./config.js";

// Exchange authorization code for tokens
async function exchangeCodeForTokens(code, state, codeVerifier, port, isManual = false, expiresIn) {
    const body = {
        grant_type: "authorization_code",
        code: code,
        redirect_uri: isManual
            ? OAUTH_CONFIG.MANUAL_REDIRECT_URL
            : `http://localhost:${port}/callback`,
        client_id: OAUTH_CONFIG.CLIENT_ID,
        code_verifier: codeVerifier,
        state: state,  // ← CRITICAL: Must include state!
    };

    if (expiresIn !== undefined) {
        body.expires_in = expiresIn;
    }

    const response = await axios.post(
        OAUTH_CONFIG.TOKEN_URL,
        body,
        { headers: { "Content-Type": "application/json" } }
    );

    if (response.status !== 200) {
        throw new Error(
            response.status === 401
                ? "Authentication failed: Invalid authorization code"
                : `Token exchange failed (${response.status}): ${response.statusText}`
        );
    }

    return response.data;
}

// Refresh access token
async function refreshToken(refreshToken) {
    const body = {
        grant_type: "refresh_token",
        refresh_token: refreshToken,
        client_id: OAUTH_CONFIG.CLIENT_ID,
    };

    try {
        const response = await axios.post(
            OAUTH_CONFIG.TOKEN_URL,
            body,
            { headers: { "Content-Type": "application/json" } }
        );

        if (response.status !== 200) {
            throw new Error(`Token refresh failed: ${response.statusText}`);
        }

        const data = response.data;
        const {
            access_token,
            refresh_token = refreshToken,  // Reuse old if not provided
            expires_in
        } = data;

        const expiresAt = Date.now() + expires_in * 1000;
        const scopes = data.scope.split(" ");

        return {
            accessToken: access_token,
            refreshToken: refresh_token,
            expiresAt: expiresAt,
            scopes: scopes,
        };

    } catch (err) {
        throw err;
    }
}

// Check if token needs refresh (5 min buffer)
function needsRefresh(expiresAt) {
    if (expiresAt === null) return false;
    const buffer = 300000;  // 5 minutes
    return Date.now() + buffer >= expiresAt;
}

module.exports = {
    exchangeCodeForTokens,
    refreshToken,
    needsRefresh,
};
