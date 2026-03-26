# Ghost

Ghost es un runtime local de desarrollo de software.

Su arquitectura tiene tres capas:

1. `Ghost Gateway`: compatibilidad OpenAI y Anthropic, auth, model registry, health, streaming y observabilidad.
2. `Ghost Runtime`: intake, task contract, retrieval, execution, verification, repair y artifacts.
3. `Ghost CLI`: comandos, modos, progreso visible y experiencia de operador.

Ghost no se define como "un chat que programa".
Ghost se define como un sistema operativo de desarrollo con ejecucion disciplinada.

## Principios del producto

- El server es estable y aburrido.
- El runtime es inteligente y estructurado.
- La CLI es rapida, visible y confiable.
- Cada tarea deja evidencia.
- La verificacion es obligatoria.
- El sistema optimiza latencia y foco, no teatro.

## Documentacion canonica

- `docs/README.md`: mapa de documentacion
- `docs/PRD.md`: definicion del producto
- `docs/ARCHITECTURE.md`: arquitectura objetivo
- `docs/CLI.md`: experiencia y contrato de la CLI
- `docs/RUNTIME.md`: loop operativo y cerebro del sistema
- `docs/GATEWAY.md`: capa de gateway y compatibilidad
- `docs/MODEL_STRATEGY.md`: routing, roles y politica de modelos
- `docs/OPERATIONS.md`: configuracion, salud, observabilidad y release gate

## Instalacion tipo CLI

GhostLLM puede exponerse como comando `ghost`.

macOS / Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/denisalbertofx/ghostllm/master/install/install.sh | bash
ghost init
ghost start
ghost doctor
ghost codex
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/denisalbertofx/ghostllm/master/install/install.ps1 | iex
ghost init
ghost start
ghost doctor
ghost codex
```

Notas:

- `ghost init` crea `configs/default.local.yaml` y mantiene `configs/default.yaml` sin secretos.
- `ghost update` hace `git pull --ff-only` y luego `uv sync`.
- El instalador usa por defecto `https://github.com/denisalbertofx/ghostllm.git`.

## Mapa del repositorio

- `apps/server/`: gateway FastAPI y provider bridge
- `apps/cli/`: CLI y runtime interactivo
- `apps/web/`: interfaz web
- `packages/py-core/`: config y utilidades compartidas
- `configs/`: configuracion y model registry
- `docs/`: documentacion canonica del producto

## Regla de producto

Ghost se construye hacia la documentacion, no al reves.
La documentacion define el producto final. El codigo converge hacia ella.
