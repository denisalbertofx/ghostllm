from pydantic import BaseModel, Field
from typing import List, Optional
import yaml
import os

class ModelMapping(BaseModel):
    name: str
    upstream_id: str
    description: Optional[str] = None
    enabled: bool = True
    expensive: bool = False

class ModelRegistry(BaseModel):
    models: List[ModelMapping]

class UpstreamConfig(BaseModel):
    nvidia_api_key: str
    base_url: str = "https://integrate.api.nvidia.com/v1"

class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    api_key: Optional[str] = None

class ModelsConfig(BaseModel):
    allowlist: List[ModelMapping]

class MonitoringConfig(BaseModel):
    prometheus_port: int = 9090
    log_level: str = "INFO"
    log_format: str = "json"

class GhostConfig(BaseModel):
    server: ServerConfig
    upstream: UpstreamConfig
    models: ModelsConfig
    monitoring: MonitoringConfig

def load_config(path: str) -> GhostConfig:
    if not os.path.exists(path):
        # Create default if not exists? For now, just raise
        raise FileNotFoundError(f"Config file not found at {path}")
    with open(path, 'r') as f:
        data = yaml.safe_load(f)
    return GhostConfig(**data)

def load_registry(path: str) -> ModelRegistry:
    if not os.path.exists(path):
        return ModelRegistry(models=[])
    with open(path, 'r') as f:
        data = yaml.safe_load(f)
    return ModelRegistry(**data)

def save_config(path: str, config: GhostConfig):
    with open(path, 'w') as f:
        yaml.dump(config.model_dump(), f, default_flow_style=False)
