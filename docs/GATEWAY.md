# Gateway

## Objetivo

El gateway es la capa de compatibilidad y transporte de Ghost.

Su funcion es simple:

- recibir requests
- autenticar
- resolver modelos
- traducir formatos
- transmitir streaming
- reportar estado

## El gateway si hace

- `health`
- `ready`
- `models`
- auth por API key y JWT
- bridge OpenAI compatible
- bridge Anthropic compatible
- model registry
- logs y errores estables

## El gateway no hace

- planning
- retrieval
- seleccion de archivos
- ejecucion de herramientas
- verificacion de tareas
- repair

Todo eso pertenece al runtime.

## Interfaces

La capa expone:

- `/health`
- `/ready`
- `/v1/models`
- `/v1/chat/completions`
- `/v1/messages`

## Politica de modelos

El gateway acepta:

- aliases del registry
- upstream ids completos
- aliases de compatibilidad soportados

El gateway normaliza nombres de modelo.
No decide por si solo la estrategia de routing.

## Regla de estabilidad

El gateway debe ser:

- predecible
- observable
- compatible
- facil de testear

Si el gateway se vuelve demasiado "inteligente", degrada el producto.

## Relacion con Claude Code

Ghost puede exponer compatibilidad suficiente para lanzar clientes externos, incluido Claude Code.

Eso es una capacidad secundaria.
No define la arquitectura principal del producto.
