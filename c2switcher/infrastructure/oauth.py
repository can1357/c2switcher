"""OAuth 2.0 PKCE authentication flow for Anthropic Claude."""

from __future__ import annotations

import base64
import hashlib
import secrets
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse, urlencode

import requests


class OAuthConfig:
    """OAuth endpoints and client configuration."""

    CLIENT_ID = '9d1c250a-e61b-44d9-88ed-5944d1962f5e'
    AUTHORIZE_URL = 'https://claude.ai/oauth/authorize'
    REDIRECT_URI = 'https://platform.claude.com/oauth/code/callback'
    TOKEN_URL = 'https://platform.claude.com/v1/oauth/token'
    SCOPES = ['org:create_api_key', 'user:profile', 'user:inference', 'user:sessions:claude_code', 'user:mcp_servers']


class PKCEGenerator:
    """Generate PKCE code verifier and challenge."""

    @staticmethod
    def generate(length: int = 43) -> Tuple[str, str]:
        """Generate code_verifier and code_challenge."""
        if not 43 <= length <= 128:
            raise ValueError(f'Length must be 43-128, got {length}')

        # Generate random verifier
        code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(length)).decode('utf-8')
        code_verifier = code_verifier.rstrip('=')[:length]

        # Generate S256 challenge
        digest = hashlib.sha256(code_verifier.encode('utf-8')).digest()
        code_challenge = base64.urlsafe_b64encode(digest).decode('utf-8')
        code_challenge = code_challenge.rstrip('=')

        return code_verifier, code_challenge


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler for OAuth callback."""

    authorization_code: Optional[str] = None
    state: Optional[str] = None
    error: Optional[str] = None
    success_redirect_url: Optional[str] = None
    expected_state: Optional[str] = None

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass

    def do_GET(self):
        """Handle OAuth callback."""
        parsed = urlparse(self.path)

        # Only accept /callback path
        if parsed.path != '/callback':
            self.send_response(404)
            self.end_headers()
            return

        params = parse_qs(parsed.query)

        if params.get('error'):
            OAuthCallbackHandler.error = params['error'][0]
            self.send_response(400)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(
                b"""
            <html><body style="font-family: sans-serif; padding: 40px; text-align: center;">
               <h1 style="color: #d32f2f;">Authentication Failed</h1>
               <p>You can close this window.</p>
            </body></html>
         """
            )
            return

        code = params.get('code', [None])[0]
        state = params.get('state', [None])[0]

        # Validate state parameter
        if state != self.expected_state:
            self.send_response(400)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(
                b"""
            <html><body style="font-family: sans-serif; padding: 40px; text-align: center;">
               <h1 style="color: #d32f2f;">Invalid State Parameter</h1>
               <p>You can close this window.</p>
            </body></html>
         """
            )
            return

        OAuthCallbackHandler.authorization_code = code
        OAuthCallbackHandler.state = state

        # Store but don't send response yet (will redirect after token exchange)
        # This matches Claude Code's behavior of redirecting to success page
        if self.success_redirect_url:
            self.send_response(302)
            self.send_header('Location', self.success_redirect_url)
            self.end_headers()
        else:
            # Fallback: show success page
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(
                b"""
            <html><body style="font-family: sans-serif; padding: 40px; text-align: center;">
               <h1 style="color: #4caf50;">Authentication Successful</h1>
               <p>You can close this window and return to the terminal.</p>
            </body></html>
         """
            )


class OAuthClient:
    """OAuth 2.0 client with PKCE support."""

    def __init__(self, config: OAuthConfig = None):
        self.config = config or OAuthConfig()

    def build_authorize_url(
        self,
        code_challenge: str,
        state: str,
        redirect_uri: str,
        scopes: Optional[list] = None,
    ) -> str:
        """Build OAuth authorization URL."""
        params = {
            'code': 'true',
            'response_type': 'code',
            'client_id': self.config.CLIENT_ID,
            'redirect_uri': redirect_uri,
            'scope': ' '.join(scopes or self.config.SCOPES),
            'code_challenge': code_challenge,
            'code_challenge_method': 'S256',
            'state': state,
        }
        return f'{self.config.AUTHORIZE_URL}?{urlencode(params)}'

    def exchange_code(
        self,
        code: str,
        code_verifier: str,
        redirect_uri: str,
        state: str,
    ) -> Dict:
        """Exchange authorization code for tokens."""
        response = requests.post(
            self.config.TOKEN_URL,
            json={
                'grant_type': 'authorization_code',
                'code': code,
                'code_verifier': code_verifier,
                'redirect_uri': redirect_uri,
                'client_id': self.config.CLIENT_ID,
                'state': state,
            },
            headers={'Content-Type': 'application/json'},
            timeout=10,
        )

        if response.status_code == 401:
            raise ValueError('Authentication failed: Invalid authorization code')

        if response.status_code != 200:
            raise ValueError(f'Token exchange failed ({response.status_code}): {response.text}')

        return response.json()

    def start_callback_server(self, state: str, port: int = 0) -> Tuple[HTTPServer, int]:
        """Start HTTP server for OAuth callback on random port."""
        # Set expected state for validation
        OAuthCallbackHandler.expected_state = state

        server = HTTPServer(('localhost', port), OAuthCallbackHandler)
        actual_port = server.server_address[1]
        return server, actual_port

    def set_success_redirect(self, scopes: list):
        """Set success redirect URL based on scopes."""
        # Inference-only goes to claude.ai, full OAuth goes to console
        is_inference_only = scopes == ['user:inference']
        OAuthCallbackHandler.success_redirect_url = (
            'https://claude.ai/oauth/code/success?app=claude-code'
            if is_inference_only
            else 'https://platform.claude.com/oauth/code/success?app=claude-code'
        )

    def login(self, auto_open: bool = True, use_dual_flow: bool = True) -> Dict:
        """
        Perform full OAuth login flow with dual automatic/manual support.

        Args:
           auto_open: Whether to auto-open browser
           use_dual_flow: If True, use dual flow (automatic + manual fallback)
                          If False, use manual-only flow

        Returns credentials dict in Claude Code format.
        """
        # Generate PKCE
        code_verifier, code_challenge = PKCEGenerator.generate()
        state = secrets.token_urlsafe(32)

        if use_dual_flow:
            # Dual flow: Try automatic (localhost) + manual fallback
            return self._dual_flow_login(code_verifier, code_challenge, state, auto_open)
        else:
            # Manual-only flow (original behavior)
            return self._manual_only_login(code_verifier, code_challenge, state, auto_open)

    def _dual_flow_login(self, code_verifier: str, code_challenge: str, state: str, auto_open: bool) -> Dict:
        """Dual flow: automatic localhost callback + manual fallback."""
        import threading
        import time

        # Start HTTP server on random port
        server, port = self.start_callback_server(state)
        server_thread = threading.Thread(target=server.handle_request, daemon=True)
        server_thread.start()

        # Build both URLs
        automatic_redirect = f'http://localhost:{port}/callback'
        manual_redirect = self.config.REDIRECT_URI

        automatic_url = self.build_authorize_url(code_challenge, state, automatic_redirect)
        manual_url = self.build_authorize_url(code_challenge, state, manual_redirect)

        print('\nOpening browser for authentication...')
        print("\nIf browser doesn't open or localhost fails, use this URL:")
        print(f'{manual_url}\n')

        # Open automatic URL first (preferred)
        if auto_open:
            try:
                webbrowser.open(automatic_url)
            except Exception:
                # Fallback: try manual URL
                try:
                    webbrowser.open(manual_url)
                except Exception:
                    pass

        # Race: localhost callback vs manual paste
        code = None
        used_automatic = False
        got_code = threading.Event()

        def wait_for_server():
            nonlocal code, used_automatic
            server_thread.join(timeout=120)
            if OAuthCallbackHandler.authorization_code:
                code = OAuthCallbackHandler.authorization_code
                used_automatic = True
                got_code.set()

        watcher = threading.Thread(target=wait_for_server, daemon=True)
        watcher.start()

        print('Waiting for authorization (paste code or complete in browser)...')
        try:
            while not got_code.is_set():
                # Check every 0.5s if server got it; if not, try non-blocking read
                if got_code.wait(timeout=0.5):
                    break
                # Try reading a line from stdin (blocks until user hits enter)
                import sys
                import select
                if select.select([sys.stdin], [], [], 0)[0]:
                    manual_code = sys.stdin.readline().strip()
                    if manual_code:
                        if '#' in manual_code:
                            manual_code = manual_code.split('#')[0]
                        code = manual_code
                        break
        except (EOFError, KeyboardInterrupt):
            pass

        if used_automatic:
            print('Received automatic callback')
        elif code:
            print('Using manual code')

        if not code:
            server.server_close()
            raise ValueError('No authorization code received')

        # Exchange code for tokens with correct redirect_uri
        redirect_uri = automatic_redirect if used_automatic else manual_redirect

        try:
            token_data = self.exchange_code(code, code_verifier, redirect_uri, state)

            # Set success redirect if automatic was used
            if used_automatic:
                scopes = token_data.get('scope', ' '.join(self.config.SCOPES)).split()
                self.set_success_redirect(scopes)

            # Build credentials
            import time as time_module

            expires_in = token_data.get('expires_in', 3600)
            expires_at = int(time_module.time() * 1000) + (expires_in * 1000)

            return {
                'claudeAiOauth': {
                    'accessToken': token_data['access_token'],
                    'refreshToken': token_data.get('refresh_token'),
                    'expiresAt': expires_at,
                    'scopes': token_data.get('scope', ' '.join(self.config.SCOPES)).split(),
                }
            }

        finally:
            server.server_close()

    def _manual_only_login(self, code_verifier: str, code_challenge: str, state: str, auto_open: bool) -> Dict:
        """Manual-only flow (original behavior)."""
        # Build authorization URL
        auth_url = self.build_authorize_url(code_challenge, state, self.config.REDIRECT_URI)

        print("\nBrowser didn't open? Use the url below to sign in:\n")
        print(f'{auth_url}\n')

        if auto_open:
            webbrowser.open(auth_url)

        # Prompt for code
        print('Paste code here if prompted >')
        code = input().strip()

        # Strip #state fragment if present
        if '#' in code:
            code = code.split('#')[0]

        if not code:
            raise ValueError('No authorization code provided')

        # Exchange code for tokens
        token_data = self.exchange_code(code, code_verifier, self.config.REDIRECT_URI, state)

        # Build credentials in Claude Code format
        import time

        expires_in = token_data.get('expires_in', 3600)
        expires_at = int(time.time() * 1000) + (expires_in * 1000)

        credentials = {
            'claudeAiOauth': {
                'accessToken': token_data['access_token'],
                'refreshToken': token_data.get('refresh_token'),
                'expiresAt': expires_at,
                'scopes': token_data.get('scope', ' '.join(self.config.SCOPES)).split(),
            }
        }

        return credentials
