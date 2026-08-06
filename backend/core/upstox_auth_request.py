"""Upstox scheduled-approval token flow (pure helpers).

WHY THIS EXISTS
---------------
The Upstox access token expires at 03:30 IST every day regardless of when it was
issued, and Upstox does NOT issue refresh tokens to this app (the code exchange
returns a token-only grant, and `db.broker_keys` has never held a `refresh_token`).
So the token must be re-obtained daily.

On 2026-08-04 that re-auth landed at 09:34 IST — 19 minutes after the open. The
live 1-minute capture recorded nothing until then, and because every intraday
regime feature is anchored on the FIRST captured bar, the whole day's regime read
was anchored 19 minutes late (CLAUDE.md §28.3). That is the single highest-value
operational leak in the platform.

WHY NOT AUTOMATED TOTP LOGIN
----------------------------
Upstox's own staff state that "login shouldn't be automatically done and user must
login daily manually", citing SEBI. Storing the account password + TOTP seed on the
VPS and replaying them would violate the broker's terms, put the real brokerage
account at risk, and cut against the SEBI PMS/AIF registration path in the roadmap
(§10). It is also strictly worse operationally: it puts full account credentials on
an internet-facing box to save one tap.

THE SANCTIONED FLOW (what this module implements)
-------------------------------------------------
Upstox documents a scheduled-approval flow built for exactly this case:

  1. POST /v3/login/auth/token/request/{client_id}  body {"client_secret": ...}
     -> {"status":"success","data":{"authorization_expiry":<ms>,"notifier_url":...}}
  2. Upstox pushes an in-app / WhatsApp approval prompt to the account holder.
  3. On approval, Upstox POSTs the access token to the app's configured notifier
     webhook:
       {"client_id","user_id","access_token","token_type","expires_at",
        "issued_at","message_type":"access_token"}

So QuantG fires step 1 automatically before the open, the founder taps approve on
their phone, and the token arrives by webhook. No credential is stored anywhere,
the human approval SEBI asks for still happens, and it is done before 09:15.

SECURITY NOTE
-------------
Upstox requires the notifier endpoint to be UNAUTHENTICATED and does not sign the
payload, so the endpoint is internet-facing and anyone may POST to it. This module
therefore treats the payload as fully untrusted: the caller must match `client_id`
against our own stored API key (constant-time) and must independently verify the
token against Upstox before storing it. A forged POST then cannot do better than
waste a request.
"""
from __future__ import annotations

import hmac
import os
from typing import Any, Dict, Optional, Tuple

# Upstox scheduled-approval endpoints (v3).
AUTH_TOKEN_REQUEST_URL = os.environ.get(
    "UPSTOX_AUTH_TOKEN_REQUEST_URL",
    "https://api.upstox.com/v3/login/auth/token/request/{client_id}",
)

# IST minute-of-day at which the daily auth request is fired. 08:45 leaves 30
# minutes of slack before the 09:15 open for the approval tap to happen.
AUTH_REQUEST_MINUTE_IST = int(os.environ.get("UPSTOX_AUTH_REQUEST_MINUTE_IST", str(8 * 60 + 45)))
# IST minute at which a still-missing token is escalated as a loud failure. 09:05
# is late enough that the approval has plainly not happened, early enough to act.
AUTH_ALARM_MINUTE_IST = int(os.environ.get("UPSTOX_AUTH_ALARM_MINUTE_IST", str(9 * 60 + 5)))
AUTH_REQUEST_ENABLED = os.environ.get(
    "UPSTOX_AUTH_REQUEST_ENABLED", "true").strip().lower() == "true"

# Optional shared secret appended to the notifier path as defence in depth. Upstox
# will not authenticate, but a secret path segment at least keeps drive-by scanners
# out of the handler. Empty = the plain path is accepted.
NOTIFIER_PATH_SECRET = os.environ.get("UPSTOX_NOTIFIER_PATH_SECRET", "").strip()


class NotifierRejected(Exception):
    """The notifier payload is not a usable Upstox access-token delivery."""


def build_auth_request_url(client_id: str) -> str:
    return AUTH_TOKEN_REQUEST_URL.format(client_id=str(client_id))


def parse_auth_request_response(payload: Any) -> Dict[str, Any]:
    """Normalise the step-1 response into {ok, notifier_url, authorization_expiry, raw}.

    Never raises on shape — an unexpected body becomes ok=False with the raw payload
    kept, because this runs on a scheduler and a crash there is silent.
    """
    out: Dict[str, Any] = {
        "ok": False, "notifier_url": None, "authorization_expiry": None, "raw": payload,
    }
    if not isinstance(payload, dict):
        return out
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    notifier = data.get("notifier_url") or payload.get("notifier_url")
    expiry = data.get("authorization_expiry") or payload.get("authorization_expiry")
    status = str(payload.get("status") or "").lower()
    out["notifier_url"] = notifier
    out["authorization_expiry"] = expiry
    out["ok"] = bool(notifier) or status == "success"
    return out


def validate_notifier_payload(
    payload: Any,
    *,
    expected_client_id: Optional[str],
) -> Tuple[str, Dict[str, Any]]:
    """Validate an inbound notifier POST. Returns (access_token, meta).

    Raises NotifierRejected with a short reason otherwise. PURE — no I/O, no
    logging, so the reason can be surfaced without ever touching the token.

    The endpoint is unauthenticated by Upstox's requirement, so this is the only
    structural gate before the (separate, mandatory) live token verification:
      * message_type must be the access-token delivery, not some other notification
      * client_id must equal OUR api_key — compared constant-time
      * the token must be a plausible non-empty string

    An empty `expected_client_id` REJECTS rather than waves through: "we could not
    determine who we are" must never mean "accept anything" (§28.1 — an
    unresolvable input disables the rule, not the safeguard).
    """
    if not isinstance(payload, dict):
        raise NotifierRejected("payload is not a JSON object")

    message_type = str(payload.get("message_type") or "").strip().lower()
    if message_type and message_type != "access_token":
        raise NotifierRejected(f"ignoring message_type={message_type!r}")

    got_client = str(payload.get("client_id") or "").strip()
    want_client = str(expected_client_id or "").strip()
    if not want_client:
        raise NotifierRejected("no stored client_id to match against")
    if not got_client:
        raise NotifierRejected("payload carries no client_id")
    if not hmac.compare_digest(got_client, want_client):
        raise NotifierRejected("client_id does not match this deployment")

    token = payload.get("access_token")
    if not isinstance(token, str) or len(token.strip()) < 20:
        raise NotifierRejected("missing or implausible access_token")

    meta = {
        "upstox_user_id": payload.get("user_id"),
        "token_type": payload.get("token_type"),
        "expires_at": payload.get("expires_at"),
        "issued_at": payload.get("issued_at"),
    }
    return token.strip(), meta


def token_is_fresh(obtained_at_iso: Optional[str], *, now_ist) -> bool:
    """True when the stored token was obtained after the most recent 03:30 IST
    expiry boundary — i.e. it is valid for the current trading day.

    Upstox expires every token at 03:30 IST regardless of issue time, so "fresh"
    is not an age in hours: a token issued at 20:00 yesterday is DEAD by 09:15
    today, while one issued at 03:31 today is alive. Anything that reasons in
    elapsed hours gets this wrong twice a day.
    """
    if not obtained_at_iso:
        return False
    from datetime import datetime, timedelta, timezone as _tz
    try:
        dt = datetime.fromisoformat(str(obtained_at_iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz.utc)
    _IST = _tz(timedelta(hours=5, minutes=30))
    obtained_ist = dt.astimezone(_IST)

    # Interpret now_ist's WALL CLOCK as IST regardless of the tzinfo label it carries.
    #
    # server.py's `_ist_now()` returns `datetime.now(utc) + 5:30` — IST wall-clock fields
    # with tzinfo still UTC. Building the boundary off that produced 03:30 *UTC* = 09:00
    # IST, so every token obtained before 09:00 IST was judged stale. The 08:45 scheduled
    # request lands ~08:46, so the flow WORKING AS DESIGNED tripped the 09:05 CRITICAL
    # "token not refreshed" alarm every single day — while printing the very timestamp
    # that should have silenced it (observed 2026-08-06: stored 08:46:36 IST, alarm 09:05).
    #
    # Normalising on wall-clock fields is correct for BOTH callers: a genuine IST-aware
    # datetime keeps its own fields, and the UTC-labelled one is read as the IST it
    # actually represents. An alarm that cries wolf daily is worse than no alarm — this
    # is the one alert that has to be trustworthy at 09:05.
    now_wall = now_ist.replace(tzinfo=_IST)
    boundary = now_wall.replace(hour=3, minute=30, second=0, microsecond=0)
    if now_wall < boundary:                     # before 03:30, the boundary was yesterday
        boundary = boundary - timedelta(days=1)
    return obtained_ist >= boundary
