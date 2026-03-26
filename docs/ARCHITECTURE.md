# Arquitectura

## Vista general

Ghost tiene tres capas mayores:

1. `Gateway`
2. `Runtime`
3. `CLI`

La inteligencia vive en el runtime.
La compatibilidad vive en el gateway.
La experiencia vive en la CLI.

## Capa 1: Gateway

Responsabilidad:

- exponer endpoints compatibles
- autenticar
- resolver modelos
- transportar requests y streaming
- reportar `health` y `ready`
- registrar telemetria basica

El gateway no decide estrategia, no planifica, no orquesta herramientas y no interpreta tareas.

## Capa 2: Runtime

Responsabilidad:

- intake
- task contract
- retrieval
- execution planning
- tool execution
- verification
- repair
- artifacts
- cierre

El runtime es el cerebro del sistema.

## Capa 3: CLI

Responsabilidad:

- comandos
- modos
- progreso visible
- feedback del operador
- renderer
- resumen final

La CLI no inventa decisiones. Expone el estado del runtime de forma clara.

## Invariantes

- una tarea produce un contrato de tarea
- una escritura exige verificacion o una excepcion explicita
- no hay exploracion infinita
- no hay cierre sin evidencia
- el server nunca absorbe logica cognitiva del runtime
- la seleccion de modelo depende de fase, no de capricho

## Estado interno canonico

Toda sesion atraviesa fases explicitas:

`intake -> explore -> act -> verify -> repair -> close`

No todas las tareas pasan por todas las fases, pero ninguna tarea opera sin fase.

## Mapa de codigo

- `apps/server/`: gateway
- `apps/cli/main.py`: superficie de comandos
- `apps/cli/assistant.py`: loop principal
- `apps/cli/runtime/`: motores y contratos
- `packages/py-core/`: config, registry y utilidades compartidas

## Regla de escalado

Para escalar el producto sin romperlo:

- se agregan providers en el gateway
- se agregan estrategias en el runtime
- se agregan comandos o vistas en la CLI

Nunca se mezclan estas tres responsabilidades en el mismo modulo por conveniencia.
