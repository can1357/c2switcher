// PKCE Generation
// Source: claude.js lines 245707-245727

import * as crypto from "crypto";

function base64UrlEncode(buffer) {
    return buffer
        .toString("base64")
        .replace(/\+/g, "-")
        .replace(/\//g, "_")
        .replace(/=/g, "");
}

// Generate code verifier
function generateCodeVerifier() {
    return base64UrlEncode(crypto.randomBytes(32));
}

// Generate code challenge from verifier
function generateCodeChallenge(verifier) {
    const hash = crypto.createHash("sha256");
    hash.update(verifier);
    return base64UrlEncode(hash.digest());
}

// Generate state parameter
function generateState() {
    return base64UrlEncode(crypto.randomBytes(32));
}

module.exports = { generateCodeVerifier, generateCodeChallenge, generateState };
