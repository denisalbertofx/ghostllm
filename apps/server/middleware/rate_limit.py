import time
from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
import logging

logger = logging.getLogger("ghostllm.middleware.ratelimit")

# In-memory storage for RPM (for simplicity in this v1)
# user_id -> list of timestamps
RPM_TRACKER = {}

class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith("/v1/"):
            return await call_next(request)

        # 1. Identify User (e.g., from header or API Key)
        user_id = request.headers.get("X-User-ID", "anonymous")
        
        # 2. RPM Check
        now = time.time()
        user_timestamps = RPM_TRACKER.get(user_id, [])
        # Cleanup old timestamps
        user_timestamps = [t for t in user_timestamps if now - t < 60]
        
        if len(user_timestamps) >= 60: # Limit 60 RPM
            logger.warning(f"Rate limit exceeded for user: {user_id}")
            raise HTTPException(status_code=429, detail="Global Rate Limit Exceeded (60 RPM)")
        
        user_timestamps.append(now)
        RPM_TRACKER[user_id] = user_timestamps

        # 3. Model Expensive Check (This should ideally read from the registry)
        # For now, we'll let the endpoint handle the specific model logic 
        # but the middleware can block based on general user state.
        
        response = await call_next(request)
        return response
