# Documentacion de Ghost

Este directorio contiene el set canonico de documentacion del producto.

No hay notas tacticas, auditorias, planes temporales ni documentos duplicados.
Cada archivo responde a una pregunta distinta y estable.

## Orden de lectura

1. `PRD.md`
2. `ARCHITECTURE.md`
3. `CLI.md`
4. `RUNTIME.md`
5. `GATEWAY.md`
6. `MODEL_STRATEGY.md`
7. `OPERATIONS.md`

## Regla editorial

- Un documento por tema.
- Sin roadmap inflado.
- Sin lenguaje vago.
- Sin duplicar decisiones entre archivos.
- Sin describir "experimentos"; solo el sistema objetivo.

## Preguntas que responde cada documento

- `PRD.md`: que producto es Ghost y para quien existe
- `ARCHITECTURE.md`: como esta dividido el sistema y que invariantes no se rompen
- `CLI.md`: como se siente usar Ghost y que promete la experiencia
- `RUNTIME.md`: que ocurre dentro del cerebro del sistema en cada tarea
- `GATEWAY.md`: que hace y que no hace la capa de compatibilidad
- `MODEL_STRATEGY.md`: como se eligen y orquestan modelos
- `OPERATIONS.md`: como se configura, observa, verifica y libera el producto
