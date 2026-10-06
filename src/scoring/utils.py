import hmac, hashlib
from src.core.config import settings

HMAC_KEY = settings.SW_HMAC_KEY.encode()  # 32+ random bytes, from a secrets manager in production
CURRENT_HMAC_KEY_VERSION = "v1"

def pseudonymize(recipient_id: str, version: str = CURRENT_HMAC_KEY_VERSION) -> str:
    msg = f"{recipient_id}".encode()
    return f"{version}:{hmac.new(HMAC_KEY, msg, hashlib.sha256).hexdigest()}"