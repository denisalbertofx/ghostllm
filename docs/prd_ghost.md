PRD — GhostLLM
Runtime de desarrollo full-stack con protocolo operativo, tareas persistentes y worktrees aislados
1. Resumen ejecutivo

GhostLLM será un runtime de desarrollo full-stack diseñado para construir, reparar y evolucionar aplicaciones complejas desde cero con una operación disciplinada, verificable y segura.

Ghost no se define como “otro chat para programar”.
Ghost se define como:

un protocolo de ejecución para tareas de software

La prioridad no será “hacer más cosas”.
La prioridad será:

entender repos complejos
planear con estructura
ejecutar cambios mínimos
verificar con evidencia
aislar tareas
escalar a subagentes sin romper el repositorio

El roadmap queda formalizado en tres capas:

Ghost Core
Runtime individual excelente
Ghost Tasks
Tareas persistentes con lifecycle y artefactos
Ghost Swarm
Coordinación limitada de workers con git worktree
2. Tesis del producto

La mayoría de herramientas agentic fuertes no ganan por tener “mejor prompt”, sino por operar con una estructura visible: instrucciones persistentes, modos, políticas, herramientas con esquema, hooks, workspaces aislados y outputs verificables. Ghost adopta ese patrón, pero con una dirección más estricta y predecible.

Tesis central

Ghost debe ser el agente que:

menos improvisa
más demuestra
menos rompe
más verifica
Posicionamiento

Ghost no será:

un wrapper de Claude Code
un clon de Codex
un swarm caótico

Ghost será:

un runtime serio de desarrollo moderno
con protocolo operativo propio
con políticas claras
con artefactos verificables
con worktrees antes que “autonomía libre”
3. Problema

Los agentes actuales suelen fallar en una o varias de estas áreas:

improvisan demasiado
mezclan contexto viejo con tarea nueva
tocan demasiados archivos
ejecutan shell sin disciplina
“arreglan” el build destruyendo partes del proyecto
no separan análisis, ejecución y verificación
no aíslan tareas
no muestran evidencia suficiente de por qué hicieron lo que hicieron

Ghost debe resolver eso con una arquitectura de ejecución clara.

4. Objetivo del producto

Construir un sistema que ayude a desarrollar aplicaciones full-stack complejas desde cero y a mantenerlas con la misma disciplina.

Objetivos concretos

Ghost debe poder:

crear un proyecto full-stack nuevo desde especificación
leer y modelar la arquitectura de un repo existente
proponer planes correctos antes de editar
editar con diffs mínimos y revisables
verificar cambios con build/tests/lint según stack
operar bien en Windows y Unix
manejar tareas persistentes sin contaminación de contexto
escalar a workers aislados sin tocar el árbol principal
No objetivos

Ghost no debe:

ser autónomo total desde la fase 1
instalar dependencias indiscriminadamente
borrar archivos por defecto
usar shell libre sin políticas
mezclar múltiples cambios de alto riesgo en un solo ciclo
5. Usuario objetivo
Primario

Desarrollador individual o founder técnico que quiere construir apps full-stack modernas más rápido y con menos improvisación.

Secundario

Pequeños equipos que necesitan:

tareas claras
workers aislados
diffs auditables
ejecución segura
6. Casos de uso principales
6.1 App nueva desde cero

Ghost recibe una especificación y genera:

estructura inicial
stack
rutas
modelos
auth
DB schema
APIs
tests
plan de implementación por fases
6.2 Repo existente

Ghost:

escanea
detecta stack
identifica riesgos
propone plan
repara bugs
ejecuta cambios pequeños
verifica build/tests
6.3 Refactor controlado

Ghost:

crea task
aísla worktree
implementa cambio
resume diff
muestra evidencia
pide aprobación
6.4 Debug disciplinado

Ghost:

clasifica error
identifica causa raíz probable
ejecuta un fix mínimo
verifica que no empeoró el sistema
7. Arquitectura del producto
7.1 Capa 1 — Ghost Core

Base del sistema.

Responsabilidades
CLI
input layer
renderer
routing de intención
tool loop
diff generation
file inspection exacta
hooks
policy engine
MCP
safe shell execution
Tecnologías
Python 3.11+
Typer para CLI
Rich para renderer
prompt_toolkit para input avanzado/multilinea
Pydantic v2 para validación
SQLite + SQLModel para estado local
subprocess para ejecución controlada
pathlib / os / shutil para filesystem
difflib / unified diffs para parches visibles
7.2 Capa 2 — Ghost Tasks

Capa de tareas persistentes.

Responsabilidades
task objects
lifecycle
task storage
plan/execute/review/fix/research
reanudación
artefactos por tarea
Tecnologías
SQLite
JSON artifacts
Markdown reports
estado local en .ghost/
7.3 Capa 3 — Ghost Swarm

Coordinación limitada de workers.

Responsabilidades
leader
worker manager
worktree manager
inbox
board
merge/review coordinator
Tecnologías
git worktree
subprocess workers
opcional tmux en Unix más adelante
board inicial en terminal con Rich
8. Ghost Execution Protocol (GEP)

Este es el núcleo innovador del sistema.

8.1 GEP-1 — Intent Router

Ghost clasifica cada input antes de actuar.

Modos obligatorios
ask
architect
code
debug
review
fix
research
Salida del router
{
  "mode": "debug",
  "task_type": "type_error",
  "requires_tools": true,
  "requires_shell": false,
  "requires_file_scope": true
}
8.2 GEP-2 — Policy Gate

Antes de ejecutar cualquier acción, Ghost pasa por una compuerta de políticas.

Decisiones evaluadas
read
edit
shell
install
delete
multi-file change
worktree required
approval required
Regla

Nada se ejecuta sin pasar por Policy Gate.

8.3 GEP-3 — Structured Planning Contract

Ghost no salta a editar.

Cada tarea debe producir primero:

objetivo
causa raíz probable
alcance
riesgos
plan corto
criterio de éxito
Output estándar
{
  "objective": "",
  "root_cause_hypothesis": "",
  "scope": ["file1", "file2"],
  "risks": [],
  "plan": [],
  "success_criteria": []
}
8.4 GEP-4 — Minimal Change Execution

La ejecución debe ser mínima y auditable.

Reglas
tocar el menor número de archivos posible
preferir edición estructurada a reescritura total
no mezclar fixes no relacionados
no instalar paquetes sin prueba
Técnica
diffs unificados
patches pequeños
salida clara por archivo tocado
8.5 GEP-5 — Verification Contract

Ghost debe verificar siempre.

Evidencia mínima
qué cambió
archivos tocados
diff resumen
comando de verificación ejecutado
resultado
riesgos pendientes
rollback sugerido
Clasificación de errores

Antes de actuar, Ghost debe etiquetar el error como:

missing_dependency
type_error
api_mismatch
dead_component
invalid_import
runtime_exception
shell_incompatibility
8.6 GEP-6 — Memory & Learning

No memoria vaga. Memoria operacional.

Qué guarda Ghost
patrones del repo
comandos seguros
preferencias del usuario
políticas del proyecto
errores frecuentes
stack profile
decisiones aprobadas
Fuentes
GHOST.md
AGENTS.md / CLAUDE.md si existen
.ghost/policies.json
.ghost/tasks.db
9. Protocolos internos de Ghost
9.1 Ghost Modes

Sistema visible y obligatorio.

ask

Consulta simple. Sin cambios.

architect

Descompone problema y propone sistema/plan.

code

Implementa cambios.

debug

Diagnostica y corrige errores.

review

Inspecciona código, diffs, riesgo y calidad.

fix

Corrección puntual y mínima.

research

Explora arquitectura, dependencias y diseño.

9.2 Ghost Policy Pack

Conjunto fijo de políticas por defecto.

Políticas iniciales
NoDeleteWithoutProof
No borrar archivos sin demostrar que no se usan.
NoInstallWithoutEvidence
No ejecutar npm install, pnpm add, pip install salvo import roto verificable.
NoUnixShellOnWindows
Bloquear sed, head, tail, grep shell-style y & en PowerShell.
SingleIntentPerCycle
Un ciclo no puede mezclar build + install + rewrite + delete.
MaxTouchedFiles
Límite configurable de archivos modificados por ciclo.
NoFullRewriteWithoutApproval
Reescritura total requiere aprobación explícita.
ShellRequiresPolicy
Shell siempre pasa por Policy Gate.
9.3 Ghost Artifact Contract

Cada tarea genera artefactos legibles.

Artefactos por tarea
plan.md
root_cause.md
diff_summary.md
verification.md
rollback.md
next_action.md
Objetivo

Que Ghost no solo “actúe”, sino que deje evidencia útil.

9.4 Ghost Worktree Protocol

Toda tarea de riesgo medio/alto debe poder ir a un worktree.

Regla

Una tarea importante = un worktree aislado.

Ventajas
aislamiento real
rollback fácil
menor riesgo sobre el repo principal
base natural para workers
Tecnología
git worktree add
ramas efímeras por task
10. Tecnologías y técnicas concretas
10.1 CLI y runtime
Python 3.11+
Typer
Rich
prompt_toolkit
Pydantic v2
10.2 Estado local
SQLite
SQLModel
artifacts Markdown + JSON en .ghost/
10.3 Búsqueda y análisis
ripgrep para búsqueda textual rápida
pathlib y tree walk para estructura
AST-aware extraction para Python
Tree-sitter como fase futura para JS/TS/Python patching estructurado
10.4 Diff y edición
unified diffs
patch mínimo
edición estructurada
review antes de aplicar
10.5 Workspaces aislados
git worktree
.ghost/worktrees/
10.6 Hooks

Hooks internos por evento:

PreToolUse
PostToolUse
PreEdit
PostEdit
TaskStart
TaskComplete
TaskFailed
10.7 MCP

Ghost debe hablar con herramientas externas vía MCP.

Objetivo
navegar docs
conectar tools externas
extender el runtime sin reescribirlo
10.8 Verificación

Adapters por stack para:

npm run build
npm run lint
pnpm build
pytest
uv run pytest
playwright
alembic upgrade head o equivalente
build adapters por proyecto
11. Ghost Stack Profiles

Ghost debe soportar perfiles de stack explícitos.

11.1 TS Full-Stack Profile
Next.js
TypeScript
Tailwind CSS
shadcn/ui
Zod
Drizzle ORM
PostgreSQL
Auth.js o Clerk
Playwright
Vitest
11.2 Python + TS Full-Stack Profile
FastAPI
Pydantic v2
SQLAlchemy 2 o SQLModel
Alembic
PostgreSQL
Redis opcional
Next.js
Tailwind
Playwright
pytest
11.3 BaaS Profile
Next.js
Supabase
TypeScript
Tailwind
shadcn/ui
Postgres
Edge/server actions cuando aplique

Ghost debe detectar y fijar un stack_profile por task.

12. UX del producto
Comandos principales
ghost dev
ghost plan "..."
ghost do "..."
ghost review
ghost fix
ghost research
ghost doctor
ghost hooks
ghost mcp
Comandos de tareas
ghost task create "fix auth flow"
ghost task list
ghost task show task_001
ghost task resume task_001
ghost task close task_001
Comandos de swarm
ghost swarm start
ghost worker spawn architect --task task_001
ghost worker spawn coder --task task_001
ghost worker spawn reviewer --task task_001
ghost board
ghost merge task_001
ghost rollback task_001
13. Full-stack creation workflow
Objetivo central del producto

Ghost debe ayudar a crear apps full-stack complejas desde cero.

Flujo ideal
Etapa 1 — Product Blueprint

Ghost produce:

arquitectura
stack profile
domain model
auth strategy
data model
API surface
UI map
milestones
Etapa 2 — Bootstrap

Ghost crea:

monorepo o repo simple
estructura inicial
config base
env template
CI base
lint/test/build scripts
Etapa 3 — Vertical Slices

Ghost implementa por slices:

auth
user profile
dashboard
CRUD feature
roles/permissions
billing
notifications
admin
Etapa 4 — Hardening

Ghost revisa:

types
error handling
loading states
tests
build
dead code
deployment readiness
14. Ghost Tasks
Task object
{
  "task_id": "task_001",
  "title": "Fix operator status typing",
  "mode": "fix",
  "status": "in_progress",
  "owner": "leader",
  "assigned_worker": null,
  "scope": {
    "repo": "current",
    "files_allowed": ["app/operator/page.tsx"]
  },
  "artifacts": {
    "plan": "",
    "root_cause": "",
    "diff_summary": "",
    "verification": ""
  },
  "risks": [],
  "created_at": "",
  "updated_at": ""
}
Lifecycle
created
planned
approved
executing
verifying
blocked
done
failed
rolled_back
15. Ghost Swarm v0

Ghost Swarm no será libre. Será mínimo y seguro.

Roles iniciales
architect
analiza
diseña
no edita
coder
implementa
toca pocos archivos
trabaja en worktree aislado
reviewer
revisa diff
detecta riesgo
no mergea
Límites
máximo 3 workers
una tarea cerrada por worker
sin shell libre
sin installs
sin deletes
worktree obligatorio
Output estructurado

Cada worker devuelve:

{
  "task_id": "task_001",
  "role": "coder",
  "summary": "",
  "files_touched": [],
  "diff_summary": "",
  "risks": [],
  "status": "done"
}
16. Roadmap de implementación
Fase 1 — Ghost Core
Entregables
intent router
policy gate
exact file inspection
multiline robusto
renderer profesional
hooks
MCP base
Windows safe execution
verification adapters
Criterio de aceptación
no contaminación de contexto
no Unix shell inválido en Windows
no reasoning filtrado
inspección exacta 100%
feedback visual vivo
Fase 2 — Ghost Tasks
Entregables
task objects persistentes
task lifecycle
resume/reopen
artifacts por tarea
root cause reports
Criterio de aceptación
tareas aisladas
outputs auditables
reanudación confiable
Fase 3 — Ghost Swarm v0
Entregables
leader
architect/coder/reviewer
worktrees
board
inbox protocol
merge with approval
Criterio de aceptación
workers aislados
cero edición directa sobre repo principal
board usable
Fase 4 — Autonomous Batch
Entregables
retry policy
rollback policy
merge policy
pipeline multi-task
failure thresholds
Criterio de aceptación
no loops infinitos
rollback probado
autonomía limitada pero confiable
17. Diferenciación real de Ghost

Ghost no se va a diferenciar por “ser más brillante”.
Se va a diferenciar por:

17.1 Policy Engine más serio

Más estricto, más claro, más predecible.

17.2 Artifacts más útiles

No solo logs.
Plan, causa raíz, diff, verificación, rollback.

17.3 Worktrees antes que hype multiagente

Más aislamiento, menos caos.

17.4 Runtime antes que chat

Ghost no es solo UI conversacional.
Es protocolo de ejecución.

17.5 Full-stack creation desde cero

No solo corregir bugs.
También levantar productos completos con stack profiles claros.

18. Riesgos
Riesgo

Meter swarm demasiado pronto.

Mitigación

No pasar de fase sin acceptance clara.

Riesgo

Fixes agresivos que “compilan” rompiendo producto.

Mitigación

Policy Gate + Reviewer + Verification Contract.

Riesgo

Windows degradado frente a Unix.

Mitigación

Safe mode específico PowerShell/CMD.

Riesgo

Demasiadas heurísticas y poco protocolo.

Mitigación

Protocolos explícitos y outputs estructurados.

19. Decisión de producto

GhostLLM se construirá como:

un runtime de desarrollo full-stack moderno, con protocolo operativo, tareas persistentes, worktrees aislados y verificación obligatoria.

No como:

un chat glorificado
un enjambre libre
una herramienta que improvisa demasiado
20. Cierre

La idea central queda así:

Ghost primero será excelente como agente individual.
Luego será excelente manejando tareas.
Luego coordinará workers.
Solo después será autónomo por lotes.

Y su valor diferencial será este:

Ghost no promete magia. Ghost promete estructura, evidencia y ejecución seria.

Si quieres, el siguiente paso te lo convierto en un plan técnico por sprint con prompts exactos para tu agente, empezando por Fase 1 — Ghost Core.