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

## Instalacion y uso

GhostLLM puede exponerse como comando `ghost` en macOS, Linux y Windows.

### Requisitos de produccion

- Git
- Python 3.11+
- `uv`
- Node.js 20+ y `npm`

Node es obligatorio si quieres que Ghost cree, repare y verifique proyectos web o full stack con builds y tests reales.

### macOS / Linux - instalacion rapida

```bash
curl -fsSL https://raw.githubusercontent.com/denisalbertofx/ghostllm/master/install/install.sh | bash
ghost init
ghost start
ghost doctor
ghost dev
```

### macOS / Linux - desde un clone local

```bash
git clone https://github.com/denisalbertofx/ghostllm.git
cd ghostllm
uv sync
chmod +x ghost
./ghost init
./ghost start
./ghost doctor
./ghost dev
```

Si quieres exponer el comando globalmente desde el clone:

```bash
mkdir -p ~/.local/bin
ln -sf "$(pwd)/ghost" ~/.local/bin/ghost
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

### Windows PowerShell - instalacion rapida

```powershell
irm https://raw.githubusercontent.com/denisalbertofx/ghostllm/master/install/install.ps1 | iex
ghost init
ghost start
ghost doctor
ghost dev
```

### Configuracion inicial

`ghost init` crea `configs/default.local.yaml` y deja `configs/default.yaml` sin secretos. Si no pasas la clave por bandera, el comando te la pedira de forma interactiva.

Ejemplo no interactivo:

```bash
ghost init --nvidia-api-key "$NVIDIA_API_KEY"
```

Para usar el gateway con NVIDIA u otro upstream, configura tus credenciales en ese archivo local o en variables de entorno antes de ejecutar cargas reales.

### Comandos base

```bash
ghost start
ghost doctor
ghost dev
ghost plan "audita el repo"
ghost do "crea una API con auth y SQLite"
ghost fix "repara los tests y verifica todo"
ghost update
```

### Flujo recomendado en tu Mac

```bash
ghost init
ghost start
ghost doctor
ghost dev
```

Dentro de Ghost:

```text
/plan audita el repo y encuentra los riesgos principales
/do crea una app full stack con autenticacion, base de datos y tests
/fix arregla lo que falte y verifica build, typecheck y tests
```

Notas:

- `ghost update` hace `git pull --ff-only` y luego `uv sync`.
- El instalador usa por defecto `https://github.com/denisalbertofx/ghostllm.git`.
- Para scaffolds web o full stack, Ghost espera poder usar `npm run build`, `npx tsc --noEmit` y `npm run test` dentro del proyecto generado.

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
