# Runtime

## Rol del runtime

El runtime es el sistema que convierte una tarea en una ejecucion disciplinada.

No es un prompt grande.
No es un chat loop improvisado.
Es un pipeline con contratos, fases, presupuesto y cierre.

## Pipeline canonico

1. `Intake`
2. `Task Contract`
3. `Repo Understanding`
4. `Execution Planning`
5. `Tool Execution`
6. `Verification`
7. `Repair`
8. `Closure`

## 1. Intake

Ghost recibe una tarea y clasifica:

- tipo de trabajo
- riesgo
- necesidad de herramientas
- expectativa de cambio

La salida de intake no es narrativa. Es estructura.

## 2. Task Contract

Cada tarea genera un contrato unico con:

- objetivo
- alcance
- restricciones
- riesgo
- politica de verificacion
- politica de repair
- presupuesto

Ese contrato gobierna el resto del loop.

## 3. Repo Understanding

Ghost construye contexto util del repo:

- stack
- entrypoints
- comandos de verificacion
- capas relevantes
- archivos candidatos

La regla es: contexto suficiente, no contexto total.

## 4. Execution Planning

El runtime decide:

- que leer
- que no leer
- que editar
- que checks ejecutar
- cuando parar

El planner sirve para reducir latencia, no para introducir ceremonia.

## 5. Tool Execution

Las herramientas se usan con presupuesto y criterio.

Invariantes:

- pocas lecturas, de alta senal
- pocas escrituras, de alto impacto
- sin exploracion redundante
- sin shell caprichoso

## 6. Verification

Toda escritura requiere verificacion salvo excepcion explicita.

La verificacion:

- sale del stack profile
- usa comandos normalizados
- prioriza checks baratos con alta senal
- reporta evidencia ejecutada

## 7. Repair

Si falla un check:

- Ghost identifica el fallo dominante
- aplica un fix pequeno
- re-verifica
- respeta el limite de intentos

Repair no es una segunda tarea. Es parte del mismo loop.

## 8. Closure

La sesion cierra en uno de estos estados:

- completada
- ya implementada
- parcialmente resuelta
- fallo de verificacion
- bloqueada
- solo lectura

El cierre siempre incluye:

- que se hizo
- que se verifico
- que quedo pendiente

## Invariantes de rendimiento

- primera accion util en pocos segundos
- no mas de unas pocas acciones de exploracion sin sintesis
- no mas de un diff grande sin aprobacion o evidencia
- no mas de un repair ciego sin re-verificacion

## Artefactos

Cada tarea genera artifacts que explican:

- contrato
- cambios
- checks
- repair
- resultado final

Ghost deja rastros utiles para el operador y para el propio sistema.
