import sys
from pathlib import Path

# Add project root to sys.path to allow running as a script
root_dir = Path(__file__).resolve().parent.parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))
if str(root_dir / "packages" / "py-core") not in sys.path:
    sys.path.insert(0, str(root_dir / "packages" / "py-core"))

from fastapi import FastAPI, HTTPException, Request, Response, Depends
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from typing import Dict, Any, List, Optional
import json
import os
import asyncio
import logging

# Core & Providers & Auth
from ghostllm_core.config import (
    load_config,
    bootstrap_enabled_models,
    resolve_upstream_model_id,
    resolve_config_path,
)
from apps.server.providers.nvidia import NvidiaProvider, NVIDIAError
from apps.server.database import (
    create_db_and_tables, log_usage, 
    engine, User, get_user_by_username, get_user_by_api_key
)
from apps.server.auth.security import (
    verify_password, create_access_token, get_password_hash, generate_api_key
)
from apps.server.auth.middleware import get_current_user, admin_required
from sqlmodel import Session

# Configure JSON Logging
class JsonFormatter(logging.Formatter):
    def format(self, record):
        log_record = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "message": record.getMessage(),
            "module": record.module
        }
        return json.dumps(log_record)

log_file = "ghost.log"
file_handler = logging.FileHandler(log_file)
file_handler.setFormatter(JsonFormatter())

stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setFormatter(JsonFormatter())

logger = logging.getLogger("ghostllm")
logger.addHandler(file_handler)
logger.addHandler(stdout_handler)
logger.setLevel(logging.INFO)

app = FastAPI(title="GhostLLM Daemon")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*", "anthropic-version", "anthropic-beta", "x-api-key", "authorization"],
)

# Initialize DB and Admin on startup
@app.on_event("startup")
def on_startup():
    create_db_and_tables()
    # Create default admin if not exists
    with Session(engine) as session:
        admin = get_user_by_username("admin")
        if not admin:
            logger.info("Creating default admin user...")
            new_admin = User(
                username="admin",
                hashed_password=get_password_hash("ghost-admin-123"),
                api_key=generate_api_key(),
                role="admin",
                quota_limit=999999999
            )
            session.add(new_admin)
            session.commit()
            logger.info(f"Admin created. Default key: {new_admin.api_key}")
    logger.info("GhostLLM Server initialized.")

# Load configuration
CONFIG_PATH = resolve_config_path("configs/default.yaml")
REGISTRY_PATH = "configs/models.yaml"
cfg = None

try:
    cfg = load_config(CONFIG_PATH)
    ENABLED_MODELS, MODEL_MAPPING, MODEL_REGISTRY_ERROR = bootstrap_enabled_models(REGISTRY_PATH)
    if MODEL_REGISTRY_ERROR:
        logger.warning(f"Model registry warning: {MODEL_REGISTRY_ERROR}")
    nvidia_provider = NvidiaProvider(api_key=cfg.upstream.nvidia_api_key, base_url=cfg.upstream.base_url)
except Exception as e:
    logger.error(f"Failed to load configurations: {e}")
    ENABLED_MODELS, MODEL_MAPPING = {}, {}
    MODEL_REGISTRY_ERROR = f"{type(e).__name__}: {e}"
    nvidia_provider = None


def _model_registry_payload() -> Dict[str, Any]:
    return {
        "healthy": MODEL_REGISTRY_ERROR is None,
        "enabled_count": len(ENABLED_MODELS),
        "error": MODEL_REGISTRY_ERROR,
        "registry_path": REGISTRY_PATH,
    }


def _provider_ready_payload() -> Dict[str, Any]:
    ready = nvidia_provider is not None
    detail = "" if ready else "NVIDIA Provider not initialized"
    return {"ready": ready, "detail": detail}

# Import translation logic
from apps.server.api.translation import (
    translate_anthropic_to_openai, 
    translate_openai_to_anthropic, 
    stream_openai_to_anthropic
)

# Anthropic claude aliases always allowed (Claude bridge)
_CLAUDE_ALIASES = frozenset({
    "sonnet", "claude-3-5-sonnet", "claude-3-5-sonnet-20241022", "claude-sonnet-4-6",
})

def is_model_allowed(model_name: str) -> bool:
    """Return True if model_name is a known alias, upstream_id, or Anthropic alias."""
    resolved = resolve_upstream_model_id(model_name, MODEL_MAPPING)
    return (
        model_name in MODEL_MAPPING           # registry name alias (kimi, planner, etc.)
        or model_name in MODEL_MAPPING.values()  # direct upstream_id
        or resolved in MODEL_MAPPING.values()    # unique basename -> upstream_id
        or model_name in _CLAUDE_ALIASES       # anthropic bridge
    )

def resolve_model(model_name: str) -> str:
    """Normalize model_name to its upstream_id (or return unchanged for known upstreams)."""
    return resolve_upstream_model_id(model_name, MODEL_MAPPING)

# --- Authentication Endpoints ---

@app.post("/auth/login")
async def login(request: Request):
    body = await request.json()
    username = body.get("username")
    password = body.get("password")
    
    user = get_user_by_username(username)
    if not user or not verify_password(password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    
    access_token = create_access_token(data={"sub": user.username})
    return {"access_token": access_token, "token_type": "bearer", "api_key": user.api_key}

# --- Admin Endpoints ---

@app.post("/admin/users")
async def create_user(request: Request, admin: User = Depends(admin_required)):
    body = await request.json()
    username = body.get("username")
    password = body.get("password")
    role = body.get("role", "user")
    quota = body.get("quota_limit", 100000)
    
    if get_user_by_username(username):
        raise HTTPException(status_code=400, detail="Username already registered")
        
    new_user = User(
        username=username,
        hashed_password=get_password_hash(password),
        api_key=generate_api_key(),
        role=role,
        quota_limit=quota
    )
    
    with Session(engine) as session:
        session.add(new_user)
        session.commit()
        session.refresh(new_user)
        
    return {"id": new_user.id, "username": new_user.username, "api_key": new_user.api_key}

# --- Server Discovery ---

@app.get("/v1/models")
async def list_models():
    """Registry-backed models list with diagnostics on bootstrap failure."""
    data = [
        {
            "id": model.name,
            "object": "model",
            "created": 123456789,
            "owned_by": "ghostllm",
            "upstream_id": model.upstream_id,
            "tool_calling": model.tool_calling,
            "expensive": model.expensive,
        }
        for model in ENABLED_MODELS.values()
    ]
    payload: Dict[str, Any] = {"object": "list", "data": data}
    if MODEL_REGISTRY_ERROR is not None:
        payload["ghost_diagnostics"] = {
            "model_registry_healthy": False,
            "registry_load_error": MODEL_REGISTRY_ERROR,
            "registry_path": REGISTRY_PATH,
        }
    return payload

# --- OpenAI-compatible Endpoints ---
@app.post("/v1/v1/chat/completions") # Catch double v1
@app.post("/v1/chat/completions")
async def chat_completions(request: Request, current_user: User = Depends(get_current_user)):
    if not nvidia_provider:
        raise HTTPException(status_code=500, detail="NVIDIA Provider not initialized")

    from apps.server.api.adapters.scheduler import TaskScheduler
    from apps.server.api.adapters.openai import OpenAIAdapter
    
    body = await request.json()
    model = body.get("model")
    
    if not is_model_allowed(model):
         raise HTTPException(status_code=403, detail=f"Model '{model}' is not enabled.")

    # 1. Detect Intent Profile
    profile = TaskScheduler.detect_intent(body, is_openai=True)
    
    # 2. Build Optimized NVIDIA Request
    upstream_model = resolve_model(model)
    optimized_body = OpenAIAdapter.build_request(
        model=upstream_model,
        messages=body.get("messages", []),
        tools=body.get("tools"),
        max_tokens=body.get("max_tokens", 4096),
        temperature=body.get("temperature", 1.0),
        stream=body.get("stream", False),
        profile=profile
    )

    logger.info(f"OpenAI Client {current_user.username} | model={upstream_model}, profile={profile}")

    try:
        if optimized_body.get("stream"):
            gen = nvidia_provider.stream_chat_completions(optimized_body)
            return StreamingResponse(gen, media_type="text/event-stream")
        else:
            resp = await nvidia_provider.chat_completions(optimized_body)
            # Log usage
            usage = resp.get("usage", {})
            in_t = usage.get("prompt_tokens", 0)
            out_t = usage.get("completion_tokens", 0)
            cost = (in_t + out_t) * 0.00001
            log_usage(current_user.id, model, in_t, out_t, cost)
            return JSONResponse(content=resp)
    except NVIDIAError as e:
        logger.error(f"NVIDIA Error: {e.message}")
        raise HTTPException(status_code=e.status_code, detail=f"GhostLLM Upstream Error: {e.message}")

# --- Anthropic-compatible Endpoints ---

@app.post("/v1/messages/count_tokens")
@app.post("/model/{forced_model}/v1/messages/count_tokens")
async def count_tokens(request: Request, forced_model: Optional[str] = None, current_user: User = Depends(get_current_user)):
    """
    Realistic token count estimate for Claude Code.
    1 token is roughly 3.2 characters for normal English, but closer to 2-3 for code.
    Using 3.0 as a balanced heuristic for coding tasks.
    Limits token count to 1,000,000 to prevent overflow in Claude's UI.
    """
    body = await request.json()
    text_content = json.dumps(body.get("messages", [])) + str(body.get("system", ""))
    
    if not text_content:
        tokens = 0
    else:
        tokens = len(text_content) // 3
    
    # Limit to 1,000,000 to prevent overflow in Claude's UI
    tokens = min(tokens, 1_000_000)
    
    return JSONResponse(content={"input_tokens": tokens})

@app.post("/v1/v1/messages") # Catch double v1
@app.post("/v1/messages")
@app.post("/v1/v1/model/{forced_model}/v1/messages") # Catch double v1
@app.post("/model/{forced_model}/v1/messages")
async def anthropic_messages(request: Request, forced_model: Optional[str] = None, current_user: User = Depends(get_current_user)):
    if not nvidia_provider:
        raise HTTPException(status_code=500, detail="NVIDIA Provider not initialized")

    body = await request.json()
    if forced_model:
        body["model"] = forced_model
    local_model = body.get("model", "")
    
    if not is_model_allowed(local_model):
        raise HTTPException(status_code=403, detail=f"Model '{local_model}' is not enabled.")

    openai_req = translate_anthropic_to_openai(body, MODEL_MAPPING)
    stream = body.get("stream", False)

    res_headers = {}
    if "anthropic-version" in request.headers:
        res_headers["anthropic-version"] = request.headers["anthropic-version"]
    if "anthropic-beta" in request.headers:
        res_headers["anthropic-beta"] = request.headers["anthropic-beta"]

    import time
    start_time = time.time()
    resp_data = None
    
    try:
        if stream:
            gen = nvidia_provider.stream_chat_completions(openai_req)
            async def wrapped_gen():
                first = True
                try:
                    async for chunk in stream_openai_to_anthropic(gen, local_model):
                        if first:
                            logger.info(f"TTFT (Bridge): {time.time() - start_time:.3f}s")
                            first = False
                        yield chunk
                except NVIDIAError as e:
                    logger.error(f"STREAMING NVIDIA ERROR: {e.message}")
                    # Yield a JSON error that our CLI can parse
                    yield f"data: {json.dumps({'error': e.message})}\n\n"
                    yield "data: [DONE]\n\n"
                except Exception as e:
                    logger.error(f"STREAMING GENERAL ERROR: {str(e)}")
                    yield f"data: {json.dumps({'error': str(e)})}\n\n"
                    yield "data: [DONE]\n\n"
                
                logger.info(f"Total stream duration: {time.time() - start_time:.3f}s")
            
            return StreamingResponse(
                wrapped_gen(), 
                media_type="text/event-stream",
                headers=res_headers
            )
        else:
            resp_data = await nvidia_provider.chat_completions(openai_req)
            if not resp_data:
                raise HTTPException(status_code=502, detail="Empty response from upstream provider")
                
            translated_resp = translate_openai_to_anthropic(resp_data, local_model)
            logger.info(f"Blocking request duration: {time.time() - start_time:.3f}s")
            
            # Log usage
            usage = resp_data.get("usage", {})
            in_t = usage.get("prompt_tokens", 0)
            out_t = usage.get("completion_tokens", 0)
            cost = (in_t + out_t) * 0.00001
            log_usage(current_user.id, local_model, in_t, out_t, cost)
            
            return JSONResponse(content=translated_resp, headers=res_headers)
    except NVIDIAError as e:
        logger.error(f"NVIDIA Upstream Error: {e.message}")
        return JSONResponse(content={"error": {"message": e.message, "type": "api_error"}}, status_code=e.status_code)
    except Exception as e:
        logger.error(f"Anthropic bridge error: {str(e)}")
        return JSONResponse(content={"error": {"message": str(e), "type": "api_error"}}, status_code=500)

@app.get("/healthz")
@app.get("/health")
async def health():
    provider = _provider_ready_payload()
    registry = _model_registry_payload()
    status = "healthy" if provider["ready"] and registry["healthy"] else "degraded"
    return {
        "status": status,
        "version": "0.1.0",
        "provider": provider,
        "model_registry": registry,
    }


@app.get("/ready")
async def ready():
    provider = _provider_ready_payload()
    if provider["ready"]:
        return provider
    return JSONResponse(content=provider, status_code=503)

@app.get("/ui")
async def ui_root():
    # Redirect /ui to /ui/index.html via StaticFiles mount
    return Response(status_code=307, headers={"Location": "/ui/index.html"})

# Mount the build output
ui_dir = "apps/web/out"
next_static_dir = os.path.join(ui_dir, "_next")
favicon_path = os.path.join(ui_dir, "favicon.ico")

if os.path.exists(next_static_dir):
    app.mount("/_next", StaticFiles(directory=next_static_dir), name="next-static")

@app.get("/favicon.ico")
async def favicon():
    if os.path.exists(favicon_path):
        return FileResponse(favicon_path)
    raise HTTPException(status_code=404, detail="favicon not found")

if os.path.exists(ui_dir):
    app.mount("/ui", StaticFiles(directory=ui_dir, html=True), name="ui")

if __name__ == "__main__":
    import uvicorn
    host = getattr(getattr(cfg, "server", None), "host", "127.0.0.1")
    port = getattr(getattr(cfg, "server", None), "port", 8000)
    uvicorn.run(app, host=host, port=port)
