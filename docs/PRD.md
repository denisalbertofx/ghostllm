# PRD

## Producto

Ghost es un runtime local de desarrollo de software que ejecuta tareas de ingenieria con estructura, evidencia y control operativo.

Ghost resuelve trabajo real en repos reales:

- implementacion
- modificacion
- bug fixing
- refactor controlado
- verificacion
- review tecnico

## Usuario objetivo

Primario:

- founder tecnico
- desarrollador individual avanzado
- equipo pequeno que necesita velocidad sin perder control

Secundario:

- equipo que quiere un runtime propio sobre modelos locales o NIM
- equipo que necesita artifacts, trazas y verificabilidad

## Problema que resuelve

Las herramientas agentic mas comunes fallan en al menos una de estas areas:

- exploran demasiado
- editan demasiado
- verifican tarde o mal
- no muestran evidencia
- mezclan intencion, ejecucion y cierre
- dependen demasiado del modelo en vez del sistema

Ghost resuelve eso con un runtime estructurado.

## Promesa del producto

Ghost debe sentirse como un ingeniero fuerte operando con disciplina:

- entiende rapido
- busca poco pero bien
- toca poco pero con precision
- verifica siempre
- repara cuando falla
- deja evidencia util

## No objetivos

Ghost no persigue:

- parecer Claude nativo usando modelos que no son Claude
- autonomia libre sin limites
- shell sin politicas
- multiagente caotico
- UI vistosa sin cerebro operativo

## Flujos principales

### 1. Plan

Ghost recibe una tarea, fija alcance, riesgos, criterio de exito y estrategia de ejecucion.

### 2. Implement

Ghost localiza contexto, decide archivos, aplica cambios minimos y audita el diff.

### 3. Verify

Ghost ejecuta checks del proyecto segun stack, alcance y costo.

### 4. Repair

Si falla la verificacion, Ghost prioriza el error de mayor senal y reintenta con presupuesto acotado.

### 5. Close

Ghost entrega resultado, evidencia y siguiente accion si algo queda bloqueado.

## Criterios de exito

- tiempo corto hasta la primera accion util
- pocas herramientas irrelevantes por tarea
- baja cantidad de archivos tocados por cambio
- alta tasa de verificacion antes de cerrar
- artifacts legibles y honestos
- comportamiento consistente entre tareas similares

## Diferenciacion

Ghost no gana por tener "mejor prompt".
Ghost gana por tener:

- contrato de tarea
- loop de ejecucion
- verificacion obligatoria
- routing de modelos por fase
- artifacts y trazas
- UX de operador clara
