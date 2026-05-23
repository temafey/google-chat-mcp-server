from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, RedirectResponse
import uvicorn
import signal
import asyncio
from pathlib import Path
from typing import Optional, Dict
from datetime import datetime
from google_auth_oauthlib.flow import InstalledAppFlow

from google_chat import (
    get_credentials,
    get_client_config,
    save_credentials,
    refresh_token,
    SCOPES,
    DEFAULT_CALLBACK_URL,
    token_info
)

# Store OAuth flow state
oauth_flows: Dict[str, InstalledAppFlow] = {}

# Create FastAPI app for local auth server
app = FastAPI(title="Google Chat Auth Server")

async def _start_oauth_flow(callback_url: Optional[str]):
    """Build the OAuth flow and redirect to Google's consent screen."""
    if get_credentials():
        return JSONResponse(
            content={
                "status": "already_authenticated",
                "message": "Valid credentials already exist",
            }
        )

    try:
        client_config = get_client_config()
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=str(e))

    flow = InstalledAppFlow.from_client_config(
        client_config,
        SCOPES,
        redirect_uri=callback_url or DEFAULT_CALLBACK_URL,
    )

    auth_url, state = flow.authorization_url(
        access_type='offline',
        prompt='consent',
        include_granted_scopes='true',
    )

    oauth_flows[state] = flow
    return RedirectResponse(url=auth_url)


@app.get("/auth")
async def start_auth(callback_url: Optional[str] = Query(None)):
    """Start OAuth authentication flow"""
    try:
        return await _start_oauth_flow(callback_url)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def _handle_oauth_callback(state: str, code: Optional[str], error: Optional[str]):
    """Shared logic for both /auth/callback and /oauth2callback."""
    if error:
        print(f"OAuth callback Error: {error}")
        raise HTTPException(status_code=400, detail=f"Authorization failed: {error}")

    if not code:
        print("Error: No authorization code received")
        raise HTTPException(status_code=400, detail="No authorization code received")

    flow = oauth_flows.get(state)
    if not flow:
        print("OAuth callback Error: Invalid state parameter")
        raise HTTPException(status_code=400, detail="Invalid state parameter")

    try:
        print("fetching token: ", code)
        flow.fetch_token(code=code, access_type='offline')
        print("fetched credentials: ", flow.credentials)
        creds = flow.credentials

        if not creds.refresh_token:
            print(f"Error: No refresh token in credentials: {creds}")
            raise HTTPException(
                status_code=400,
                detail="Failed to obtain refresh token. Please try again.",
            )

        print("saving credentials: ", creds)
        save_credentials(creds)
        del oauth_flows[state]

        return JSONResponse(
            content={
                "status": "success",
                "message": "Authorization successful. Long-lived token obtained. You can close this window.",
                "token_file": token_info['token_path'],
                "expires_at": creds.expiry.isoformat() if creds.expiry else None,
                "has_refresh_token": bool(creds.refresh_token),
            }
        )
    except Exception:
        oauth_flows.pop(state, None)
        raise


@app.get("/auth/callback")
async def auth_callback(
    state: str = Query(...),
    code: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
):
    """Handle OAuth callback (legacy path)."""
    try:
        return await _handle_oauth_callback(state, code, error)
    except HTTPException:
        raise
    except Exception as e:
        print(f"OAuth callback Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/oauth2callback")
async def oauth2callback(
    state: str = Query(...),
    code: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
):
    """Handle OAuth callback (GOOGLE_OAUTH_REDIRECT_URI alias path)."""
    try:
        return await _handle_oauth_callback(state, code, error)
    except HTTPException:
        raise
    except Exception as e:
        print(f"OAuth callback Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/auth/refresh")
async def manual_token_refresh():
    """Manually trigger a token refresh"""
    success, message = await refresh_token()
    if success:
        creds = token_info['credentials']
        return JSONResponse(
            content={
                "status": "success",
                "message": message,
                "expires_at": creds.expiry.isoformat() if creds.expiry else None,
                "last_refresh": token_info['last_refresh'].isoformat()
            }
        )
    else:
        raise HTTPException(
            status_code=400,
            detail=message
        )

@app.get("/status")
async def check_auth_status():
    """Check if we have valid credentials"""
    token_path = token_info['token_path']
    token_file = Path(token_path)
    if not token_file.exists():
        return JSONResponse(
            content={
                "status": "not_authenticated",
                "message": "No authentication token found",
                "token_path": str(token_path)
            }
        )
    
    try:
        creds = get_credentials()
        if creds:
            return JSONResponse(
                content={
                    "status": "authenticated",
                    "message": "Valid credentials exist",
                    "token_path": str(token_path),
                    "expires_at": creds.expiry.isoformat() if creds.expiry else None,
                    "last_refresh": token_info['last_refresh'].isoformat() if token_info['last_refresh'] else None,
                    "has_refresh_token": bool(creds.refresh_token)
                }
            )
        else:
            return JSONResponse(
                content={
                    "status": "expired",
                    "message": "Credentials exist but are expired or invalid",
                    "token_path": str(token_path)
                }
            )
    except Exception as e:
        return JSONResponse(
            content={
                "status": "error",
                "message": str(e),
                "token_path": str(token_path)
            },
            status_code=500
        )

def run_auth_server(port: int = 8000, host: str = "localhost"):
    """Run the authentication server with graceful shutdown support
    
    Args:
        port: Port to run the server on (default: 8000)
        host: Host to bind the server to (default: localhost)
    """
    server_config = uvicorn.Config(app, host=host, port=port)
    server = uvicorn.Server(server_config)
    
    # Handle graceful shutdown
    def signal_handler(signum, frame):
        print("\nReceived signal to terminate. Performing graceful shutdown...")
        asyncio.create_task(server.shutdown())
    
    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)  # Handle Ctrl+C
    signal.signal(signal.SIGTERM, signal_handler)  # Handle termination signal
    
    try:
        print(f"\nServer is running at: http://{host}:{port}")
        print(f"Default callback URL: {DEFAULT_CALLBACK_URL}")
        # Start the server
        server.run()
    except KeyboardInterrupt:
        print("\nShutting down the auth server...")
    finally:
        print("Auth server has been stopped.") 