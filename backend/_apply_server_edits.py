"""
STEP 2.5 — Apply server.py edits for Upstox V3 feed auth fix.

Run:  python backend/_apply_server_edits.py

Changes:
1. get_user_upstox_gateway() — load refresh_token from env/DB, pass to UpstoxGateway
2. upstox_callback() — capture refresh_token from exchange_code response, store in DB, pass to gateway
3. get_user_upstox_status() — auto-refresh token on validation failure using refresh_token
"""

import re
import sys
from pathlib import Path

SERVER_PATH = Path(__file__).parent / "server.py"


def read_server() -> str:
    with open(SERVER_PATH, "r", encoding="utf-8") as f:
        return f.read()


def write_server(content: str) -> None:
    with open(SERVER_PATH, "w", encoding="utf-8") as f:
        f.write(content)


def edit_1_get_gateway(content: str) -> tuple[str, bool]:
    """Add refresh_token loading and pass to UpstoxGateway constructor."""
    old = (
        "    access_token = os.environ.get(\"UPSTOX_ACCESS_TOKEN\") or (decrypt_secret(keys.get(\"access_token\")) if keys else None)\n"
        "    redirect_uri = _upstox_redirect_uri(None, keys)\n"
        "    if not api_key and not access_token:\n"
        "        logger.warning(\"Upstox gateway not initialized for user=%s: no_keys\", user_id)\n"
        "        return None\n"
        "    if access_token:\n"
        "        logger.info(\"Loaded Upstox access token from %s for user=%s\", \"env\" if os.environ.get(\"UPSTOX_ACCESS_TOKEN\") else \"storage\", user_id)\n"
        "    else:\n"
        "        logger.warning(\"Upstox gateway initialized without access token user=%s; reconnect required\", user_id)\n"
        "    gateway = UpstoxGateway(\n"
        "        api_key=api_key,\n"
        "        api_secret=api_secret,\n"
        "        access_token=access_token,\n"
        "        redirect_uri=redirect_uri,\n"
        "        sandbox=bool(keys.get(\"is_sandbox\")) if keys else False,\n"
        "    )\n"
        "    _UPSTOX_GATEWAYS[user_id] = gateway\n"
        "    return gateway\n"
    )
    new = (
        "    access_token = os.environ.get(\"UPSTOX_ACCESS_TOKEN\") or (decrypt_secret(keys.get(\"access_token\")) if keys else None)\n"
        "    refresh_token = os.environ.get(\"UPSTOX_REFRESH_TOKEN\") or (decrypt_secret(keys.get(\"refresh_token\")) if keys else None)\n"
        "    redirect_uri = _upstox_redirect_uri(None, keys)\n"
        "    if not api_key and not access_token:\n"
        "        logger.warning(\"Upstox gateway not initialized for user=%s: no_keys\", user_id)\n"
        "        return None\n"
        "    if access_token:\n"
        "        logger.info(\"Loaded Upstox access token from %s for user=%s\", \"env\" if os.environ.get(\"UPSTOX_ACCESS_TOKEN\") else \"storage\", user_id)\n"
        "    else:\n"
        "        logger.warning(\"Upstox gateway initialized without access token user=%s; reconnect required\", user_id)\n"
        "    gateway = UpstoxGateway(\n"
        "        api_key=api_key,\n"
        "        api_secret=api_secret,\n"
        "        access_token=access_token,\n"
        "        refresh_token=refresh_token,\n"
        "        redirect_uri=redirect_uri,\n"
        "        sandbox=bool(keys.get(\"is_sandbox\")) if keys else False,\n"
        "    )\n"
        "    _UPSTOX_GATEWAYS[user_id] = gateway\n"
        "    return gateway\n"
    )
    if old not in content:
        print("  [SKIP] Edit 1 (get_user_upstox_gateway): pattern not found. Already applied?")
        return content, False
    content = content.replace(old, new, 1)
    print("  [OK]   Edit 1 (get_user_upstox_gateway): refresh_token loading added.")
    return content, True


def edit_2_upstox_callback(content: str) -> tuple[str, bool]:
    """Store refresh_token in DB and pass to gateway after token exchange."""
    old = (
        "    access_token = token_response.get(\"access_token\")\n"
        "    if not access_token:\n"
        "        raise HTTPException(status_code=400, detail=\"Upstox token response did not include access_token.\")\n"
        "    await db.broker_keys.update_one(\n"
        "        {\"user_id\": user_id, \"broker\": \"upstox\"},\n"
        "        {\n"
        "            \"$set\": {\n"
        "                \"access_token\": encrypt_secret(str(access_token)),\n"
        "                \"upstox_user_id\": token_response.get(\"user_id\"),\n"
    )
    new = (
        "    access_token = token_response.get(\"access_token\")\n"
        "    refresh_token = token_response.get(\"refresh_token\")\n"
        "    if not access_token:\n"
        "        raise HTTPException(status_code=400, detail=\"Upstox token response did not include access_token.\")\n"
        "    await db.broker_keys.update_one(\n"
        "        {\"user_id\": user_id, \"broker\": \"upstox\"},\n"
        "        {\n"
        "            \"$set\": {\n"
        "                \"access_token\": encrypt_secret(str(access_token)),\n"
        "                \"refresh_token\": encrypt_secret(str(refresh_token)) if refresh_token else None,\n"
        "                \"upstox_user_id\": token_response.get(\"user_id\"),\n"
    )
    if old not in content:
        print("  [SKIP] Edit 2 (upstox_callback): pattern not found. Already applied?")
        return content, False
    content = content.replace(old, new, 1)
    print("  [OK]   Edit 2 (upstox_callback): refresh_token stored in DB.")
    return content, True


def edit_3_upstox_callback_gateway(content: str) -> tuple[str, bool]:
    """Pass refresh_token to gateway instance after OAuth callback."""
    old = (
        "    gateway.access_token = access_token\n"
        "    user = await db.users.find_one({\"id\": user_id}, {\"_id\": 0, \"password_hash\": 0})\n"
    )
    new = (
        "    gateway.access_token = access_token\n"
        "    if refresh_token:\n"
        "        gateway._refresh_token = refresh_token\n"
        "        logger.info(\"Refresh token stored on gateway for user=%s\", user_id)\n"
        "    else:\n"
        "        logger.warning(\"No refresh_token returned from Upstox for user=%s\", user_id)\n"
        "    user = await db.users.find_one({\"id\": user_id}, {\"_id\": 0, \"password_hash\": 0})\n"
    )
    if old not in content:
        print("  [SKIP] Edit 3 (upstox_callback gateway): pattern not found. Already applied?")
        return content, False
    content = content.replace(old, new, 1)
    print("  [OK]   Edit 3 (upstox_callback gateway): refresh_token passed to gateway.")
    return content, True


def edit_4_status_auto_refresh(content: str) -> tuple[str, bool]:
    """Add auto-refresh logic to get_user_upstox_status when token validation fails."""
    old_block_start = "        validator = gateway or UpstoxGateway("
    old_block_end = (
        "            token_validation_error = f\"Token validation failed for user={user_id}: {exc}\"\n"
        "            token_valid = False\n"
        "            logger.warning(\"Token validation failed for user=%s: %s\", user_id, exc)\n"
    )
    new_block_end = (
        "            token_validation_error = f\"Token validation failed for user={user_id}: {exc}\"\n"
        "            token_valid = False\n"
        "            logger.warning(\"Token validation failed for user=%s: %s\", user_id, exc)\n"
        "        # Auto-refresh token if refresh_token is available\n"
        "        if not token_valid:\n"
        "            try:\n"
        "                gw = gateway or _UPSTOX_GATEWAYS.get(user_id)\n"
        "                if gw and gw._refresh_token:\n"
        "                    logger.info(\"Attempting token refresh for user=%s via refresh_token\", user_id)\n"
        "                    refreshed = await asyncio.to_thread(gw.refresh_access_token)\n"
        "                    if refreshed:\n"
        "                        token_valid = True\n"
        "                        token_state = \"refreshed\"\n"
        "                        token_validation_error = None\n"
        "                        new_token = gw.access_token\n"
        "                        if new_token:\n"
        "                            await db.broker_keys.update_one(\n"
        "                                {\"user_id\": user_id, \"broker\": \"upstox\"},\n"
        "                                {\"$set\": {\n"
        "                                    \"access_token\": encrypt_secret(str(new_token)),\n"
        "                                    \"access_token_obtained_at\": datetime.now(timezone.utc).isoformat(),\n"
        "                                }}\n"
        "                            )\n"
        "                        logger.info(\"Token refreshed successfully for user=%s\", user_id)\n"
        "                else:\n"
        "                    logger.warning(\"No refresh_token available for user=%s; reconnect required\", user_id)\n"
        "            except Exception as refresh_exc:\n"
        "                logger.warning(\"Token refresh also failed for user=%s: %s\", user_id, refresh_exc)\n"
    )
    if old_block_start not in content or old_block_end not in content:
        print("  [SKIP] Edit 4 (status auto-refresh): pattern not found. Already applied?")
        return content, False
    content = content.replace(old_block_end, new_block_end, 1)
    print("  [OK]   Edit 4 (get_user_upstox_status): auto-refresh on validation failure added.")
    return content, True


# Edit 5 removed: BrokerKeyReq has no refresh_token field.
# Refresh tokens come from the OAuth exchange_code response (edit 2/3).


def main():
    print("Applying server.py edits for Upstox V3 feed auth fix...\n")
    content = read_server()
    changes = []

    content, ok = edit_1_get_gateway(content)
    changes.append(ok)
    content, ok = edit_2_upstox_callback(content)
    changes.append(ok)
    content, ok = edit_3_upstox_callback_gateway(content)
    changes.append(ok)
    content, ok = edit_4_status_auto_refresh(content)
    changes.append(ok)

    if any(changes):
        write_server(content)
        print(f"\n{'='*60}")
        print(f"✅ {sum(changes)} of {len(changes)} edits applied successfully.")
        print(f"✅ server.py updated.")
        print(f"\nNext: Restart the backend to pick up changes.")
    else:
        print(f"\n{'='*60}")
        print("No edits applied — all patterns already present?")

    # Verify no corruption
    try:
        import py_compile
        py_compile.compile(str(SERVER_PATH), doraise=True)
        print("✅ server.py syntax check passed.")
    except py_compile.PyCompileError as e:
        print(f"❌ SYNTAX ERROR in server.py: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
