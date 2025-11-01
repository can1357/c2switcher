// OAuth Callback Server (class eR1)
// Source: claude.js lines 245492-245629

import * as http from "http";
import * as url from "url";

class OAuthCallbackServer {
    constructor() {
        this.localServer = http.createServer();
        this.port = 0;
        this.promiseResolver = null;
        this.promiseRejecter = null;
        this.expectedState = null;
        this.pendingResponse = null;
    }

    // Start server on random port
    async start() {
        return new Promise((resolve, reject) => {
            this.localServer.once("error", (err) => {
                reject(new Error(`Failed to start OAuth callback server: ${err.message}`));
            });

            // Port 0 = OS assigns random port
            this.localServer.listen(0, "localhost", () => {
                const addr = this.localServer.address();
                this.port = addr.port;
                resolve(this.port);
            });
        });
    }

    getPort() {
        return this.port;
    }

    hasPendingResponse() {
        return this.pendingResponse !== null;
    }

    // Wait for authorization code
    async waitForAuthorization(expectedState, openBrowserCallback) {
        return new Promise((resolve, reject) => {
            this.promiseResolver = resolve;
            this.promiseRejecter = reject;
            this.expectedState = expectedState;
            this.startLocalListener(openBrowserCallback);
        });
    }

    // Redirect browser to success page
    handleSuccessRedirect(scopes) {
        if (!this.pendingResponse) return;

        const isInferenceOnly = scopes.includes("user:inference") && scopes.length === 1;
        const successUrl = isInferenceOnly
            ? "https://claude.ai/oauth/code/success?app=claude-code"
            : "https://console.anthropic.com/oauth/code/success?app=claude-code";

        this.pendingResponse.writeHead(302, { Location: successUrl });
        this.pendingResponse.end();
        this.pendingResponse = null;
    }

    handleErrorRedirect() {
        if (!this.pendingResponse) return;

        const errorUrl = "https://claude.ai/oauth/code/success?app=claude-code";
        this.pendingResponse.writeHead(302, { Location: errorUrl });
        this.pendingResponse.end();
        this.pendingResponse = null;
    }

    startLocalListener(openBrowserCallback) {
        this.localServer.on("request", this.handleRedirect.bind(this));
        this.localServer.on("error", this.handleError.bind(this));
        openBrowserCallback();
    }

    handleRedirect(req, res) {
        const parsed = url.parse(req.url || "", true);

        // Only accept /callback path
        if (parsed.pathname !== "/callback") {
            res.writeHead(404);
            res.end();
            return;
        }

        const code = parsed.query.code;
        const state = parsed.query.state;

        this.validateAndRespond(code, state, res);
    }

    validateAndRespond(code, state, res) {
        if (!code) {
            res.writeHead(400);
            res.end("Authorization code not found");
            this.reject(new Error("No authorization code received"));
            return;
        }

        if (state !== this.expectedState) {
            res.writeHead(400);
            res.end("Invalid state parameter");
            this.reject(new Error("Invalid state parameter"));
            return;
        }

        // Store response to redirect later
        this.pendingResponse = res;
        this.resolve(code);
    }

    handleError(err) {
        console.error(err);
        this.close();
        this.reject(err);
    }

    resolve(code) {
        if (this.promiseResolver) {
            this.promiseResolver(code);
            this.promiseResolver = null;
            this.promiseRejecter = null;
        }
    }

    reject(err) {
        if (this.promiseRejecter) {
            this.promiseRejecter(err);
            this.promiseResolver = null;
            this.promiseRejecter = null;
        }
    }

    close() {
        if (this.pendingResponse) {
            this.handleErrorRedirect();
        }
        if (this.localServer) {
            this.localServer.removeAllListeners();
            this.localServer.close();
        }
    }
}

module.exports = OAuthCallbackServer;
