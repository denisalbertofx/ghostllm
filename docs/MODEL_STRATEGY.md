# Estrategia de Modelos

## Principio

Ghost no usa un solo modelo para todo.
Ghost usa roles de modelo por fase.

La experiencia rapida sale de esa division.

## Roles de modelo

### 1. Fast

Uso:

- clasificacion
- exploracion corta
- resumenes
- compresion de contexto

Objetivo:

- latencia baja
- costo bajo

### 2. Planner

Uso:

- plan de ataque
- decisiones de alcance
- estrategia de ejecucion

Objetivo:

- claridad
- priorizacion

### 3. Execution

Uso:

- implementacion
- cambios multiarchivo
- refactor pequeno

Objetivo:

- calidad de edicion
- obediencia al contrato

### 4. Repair

Uso:

- arreglar fallos de verificacion
- resolver errores puntuales

Objetivo:

- precision
- tiempo corto hasta el recheck

## Regla de routing

El routing depende de:

- fase
- riesgo
- costo del contexto
- necesidad de herramientas
- tipo de tarea

No depende solo del comando del usuario.

## Provider strategy

Provider por defecto:

- NVIDIA NIM

Diseno requerido:

- provider agnostico en el runtime
- provider especifico en el gateway

Eso permite cambiar backend sin romper el cerebro del producto.

## Regla de velocidad

Lo rapido no viene solo de un modelo rapido.
Viene de:

- usar el modelo correcto
- reducir contexto
- paralelizar lecturas y preparacion
- limitar exploracion inutil

## Regla de calidad

El modelo fuerte no sustituye:

- task contract
- planner
- verify
- repair

Si Ghost depende solo del modelo, Ghost vuelve a ser un chat.

## Evaluacion

Cada rol se evalua por su propio trabajo:

- fast: tiempo y utilidad de contexto
- planner: calidad de alcance y plan
- execution: diffs y tasa de verificacion
- repair: tasa de recuperacion por intento
