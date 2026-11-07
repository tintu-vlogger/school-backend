"""
Send push notifications via Firebase Cloud Messaging (FCM) v1 API.
Uses service account credentials — no Expo push service needed.
"""
import json
import os
import time
import httpx
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GoogleAuthRequest

FCM_SCOPES = ["https://www.googleapis.com/auth/firebase.messaging"]
FCM_URL = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"

_credentials = None
_project_id = None


def _get_credentials():
    global _credentials, _project_id

    if _credentials and _credentials.valid:
        return _credentials, _project_id

    # Try service account file
    sa_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "firebase-service-account.json")
    if os.path.exists(sa_path):
        _credentials = service_account.Credentials.from_service_account_file(sa_path, scopes=FCM_SCOPES)
        with open(sa_path) as f:
            _project_id = json.load(f).get("project_id", "")
    else:
        # Try JSON from environment variable
        sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "")
        if sa_json:
            info = json.loads(sa_json)
            _credentials = service_account.Credentials.from_service_account_info(info, scopes=FCM_SCOPES)
            _project_id = info.get("project_id", "")
        else:
            return None, None

    _credentials.refresh(GoogleAuthRequest())
    return _credentials, _project_id


async def send_fcm_push(token: str, title: str, body: str, data: dict | None = None, debug: bool = True) -> bool:
    """Send a push notification to a single FCM token."""
    creds, project_id = _get_credentials()
    if not creds or not project_id:
        print(f"[FCM] No credentials found. Check firebase-service-account.json")
        return False

    if not creds.valid:
        creds.refresh(GoogleAuthRequest())

    print(f"[FCM] Sending to token: {token[:20]}... project: {project_id}")

    message = {
        "message": {
            "token": token,
            "notification": {
                "title": title,
                "body": body,
            },
            "android": {
                "priority": "high",
                "notification": {
                    "sound": "default",
                    "channel_id": "default",
                },
            },
        }
    }
    if data:
        message["message"]["data"] = {k: str(v) for k, v in data.items()}

    url = FCM_URL.format(project_id=project_id)

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            url,
            json=message,
            headers={
                "Authorization": f"Bearer {creds.token}",
                "Content-Type": "application/json",
            },
        )
        print(f"[FCM] Response: {resp.status_code} {resp.text[:200]}")
        return resp.status_code == 200


async def send_fcm_to_user_tokens(tokens: list[str], title: str, body: str, data: dict | None = None) -> int:
    """Send push to multiple FCM tokens. Returns count of successful sends."""
    sent = 0
    for token in tokens:
        if token and await send_fcm_push(token, title, body, data):
            sent += 1
    return sent
