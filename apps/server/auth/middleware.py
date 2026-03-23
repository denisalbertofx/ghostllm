from fastapi import Request, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional
import logging
from apps.server.database import get_user_by_api_key, get_user_by_username, User
from apps.server.auth.security import decode_token

logger = logging.getLogger("ghostllm.auth")

security = HTTPBearer(auto_error=False)

async def get_current_user(request: Request, auth: Optional[HTTPAuthorizationCredentials] = Depends(security)) -> User:
    # 1. Check API Key header
    api_key = request.headers.get("X-API-Key")
    if api_key:
        user = get_user_by_api_key(api_key)
        if user and user.is_active:
             # Check quota
            if user.quota_used >= user.quota_limit:
                 raise HTTPException(status_code=402, detail="Payment Required: User quota exceeded.")
            return user

    # 2. Check Bearer Token (can be JWT or API Key)
    if auth:
        token = auth.credentials
        # Check if it's a raw API Key first (OpenAI style)
        user = get_user_by_api_key(token)
        if user and user.is_active:
             return user

        # Fallback to JWT
        payload = decode_token(token)
        if payload:
            username = payload.get("sub")
            if username:
                user = get_user_by_username(username)
                if user and user.is_active:
                    return user

    raise HTTPException(status_code=401, detail="Invalid API Key or Token")

def admin_required(user: User = Depends(get_current_user)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Operation not permitted: Admin access required.")
    return user
