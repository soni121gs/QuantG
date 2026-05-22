import httpx
import os
from urllib.parse import urlencode

UPSTOX_API_BASE_URL = "https://api.upstox.com/v2"

async def get_upstox_auth_url(api_key: str, redirect_uri: str) -> str:
    """Generates the Upstox authorization URL."""
    params = {
        "response_type": "code",
        "client_id": api_key,
        "redirect_uri": redirect_uri,
        "state": "quantg_upstox_login", # Can be used for CSRF protection
    }
    return f"https://api.upstox.com/v2/login/authorization/dialog?{urlencode(params)}"

async def exchange_code_for_tokens(api_key: str, api_secret: str, redirect_uri: str, auth_code: str) -> dict:
    """Exchanges the authorization code for access and refresh tokens."""
    headers = {
        "accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "code": auth_code,
        "client_id": api_key,
        "client_secret": api_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(f"{UPSTOX_API_BASE_URL}/login/authorization/token", headers=headers, data=data)
        response.raise_for_status()
        return response.json()

async def refresh_upstox_access_token(api_key: str, api_secret: str, refresh_token: str) -> dict:
    """Refreshes the Upstox access token using the refresh token."""
    headers = {
        "accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "client_id": api_key,
        "client_secret": api_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(f"{UPSTOX_API_BASE_URL}/login/authorization/token", headers=headers, data=data)
        response.raise_for_status()
        return response.json()

async def get_upstox_profile(access_token: str) -> dict:
    """Fetches the user profile using the access token."""
    headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {access_token}",
    }
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{UPSTOX_API_BASE_URL}/user/profile", headers=headers)
        response.raise_for_status()
        return response.json()

# Helper to load Upstox credentials from environment
def get_upstox_credentials():
    return os.getenv("UPSTOX_API_KEY"), os.getenv("UPSTOX_API_SECRET"), os.getenv("UPSTOX_REDIRECT_URI")