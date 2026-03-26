# CLI

## Objetivo

La CLI de Ghost debe sentirse rapida, sobria y confiable.

La sensacion correcta es esta:

- Ghost entiende la tarea rapido
- muestra el siguiente paso
- usa herramientas con intencion
- no se queda "pensando" sin producir senal
- verifica antes de cerrar

## Comandos nucleares

- `ghost doctor`
- `ghost dev`
- `ghost chat`
- `ghost plan "<task>"`
- `ghost do "<task>"`
- `ghost edit <path>`
- `ghost fix`
- `ghost claude`

## Modos

- `Chat`: consulta operativa con herramientas cuando hace falta
- `Plan`: investigacion y estrategia sin cambios
- `Execute`: implementacion con autonomia controlada
- `Patch`: edicion puntual
- `Fix`: diagnostico y reparacion

## Contrato de experiencia

La CLI debe cumplir estas reglas:

- siempre mostrar un primer paso util
- nunca esconder por completo lo que esta haciendo
- nunca pasar demasiados turnos explorando sin sintetizar
- siempre declarar que validacion corrio
- siempre dejar claro si termino, fallo o quedo bloqueado

## Sensacion de uso

Programar con Ghost debe sentirse asi:

- escribes una tarea
- Ghost clasifica el trabajo
- Ghost te dice que va a mirar
- ves pocas lecturas, no veinte vueltas inutiles
- ves el cambio
- ves la verificacion
- recibes cierre con evidencia

No debe sentirse asi:

- un minuto por herramienta
- diez minutos de silencio mental
- listados de carpetas repetidos
- texto largo sin acciones
- final ambiguo sin checks

## Salidas visibles

La CLI muestra:

- modo actual
- modelo o rol usado
- herramientas ejecutadas
- archivos tocados
- checks ejecutados
- estado final

## Tono operativo

Ghost comunica progreso en frases cortas, tecnicas y utiles.

No rellena.
No anima.
No dramatiza.
No vende humo.

## Regla de satisfaccion

El usuario debe sentir que el sistema:

- tiene direccion
- tiene criterio
- tiene ritmo
- tiene control

Si la CLI no transmite eso, la arquitectura todavia no esta llegando al producto.
