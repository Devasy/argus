"""Optional GitHub App installation authentication, isolated from review jobs."""
import asyncio
import base64
import json
import time
from pathlib import Path

import httpx


def app_jwt(app_id, key_path):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    encode = lambda data: base64.urlsafe_b64encode(data).rstrip(b"=")
    now = int(time.time())
    header = encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = encode(json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}).encode())
    unsigned = header + b"." + claims
    private_key = serialization.load_pem_private_key(Path(key_path).read_bytes(), password=None)
    signature = private_key.sign(unsigned, padding.PKCS1v15(), hashes.SHA256())
    return (unsigned + b"." + encode(signature)).decode()


class InstallationAuth:
    def __init__(self, settings):
        self.settings = settings
        self._token, self._expires, self._identity = None, 0, None
        self._lock = asyncio.Lock()

    async def token(self):
        async with self._lock:
            if self._token and time.time() < self._expires:
                return self._token
            jwt = app_jwt(self.settings.github_app_id, self.settings.github_app_private_key_path)
            async with httpx.AsyncClient(base_url="https://api.github.com", timeout=30,
                headers={"Authorization": f"Bearer {jwt}", "Accept": "application/vnd.github+json"}) as client:
                r = await client.post(f"/app/installations/{self.settings.github_installation_id}/access_tokens")
                r.raise_for_status()
                data = r.json()
                from datetime import datetime
                self._expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp() - 120
                app = await client.get("/app")
                app.raise_for_status()
                slug = app.json()["slug"]
                bot = await client.get(f"/users/{slug}[bot]")
                bot.raise_for_status()
                self._identity = bot.json()
                self._token = data["token"]
            return self._token

    async def identity(self):
        await self.token()
        return self._identity
