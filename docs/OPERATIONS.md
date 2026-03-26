# Operaciones

## Configuracion

Ghost se opera con cuatro grupos de configuracion:

- server
- provider
- model registry
- runtime

Fuentes:

- `configs/default.yaml`
- `configs/models.yaml`
- variables de entorno

## Secretos

Regla:

- ningun secreto real se documenta en archivos de producto
- los secretos viven en entorno local o vault
- el repositorio solo contiene placeholders y ejemplos

## Salud del sistema

Senales minimas:

- `health`: proceso y subsistemas visibles
- `ready`: provider utilizable
- `models`: registry cargado y resoluble

El operador debe poder ver en segundos si el sistema esta listo o no.

## Observabilidad

Ghost registra:

- logs
- artifacts
- trazas de sesion
- historial de verificaciones
- estado final de tarea

Eso permite:

- depurar
- comparar ejecuciones
- evitar mentiras de cierre

## Verificacion operacional

Toda release utilizable de Ghost debe pasar:

- arranque del server
- readiness del provider
- CLI doctor
- flujo basico de task runtime
- tests criticos del gateway y del runtime

Comando canonico:

- `scripts/release_validate.bat`
- o `uv run python scripts/release_validate.py`

## Release gate

Ghost no se considera estable si falla alguno de estos:

- server boot
- gateway readiness
- command surface basica
- verificacion del runtime
- coherencia del model registry

## Disciplina de documentacion

Toda decision permanente del producto termina aqui:

- PRD
- Arquitectura
- CLI
- Runtime
- Gateway
- Estrategia de Modelos
- Operaciones

No se crean notas paralelas para reemplazar esos documentos.

## Regla de crecimiento

Antes de agregar una feature nueva:

1. se valida si cabe en la arquitectura canonica
2. se define su lugar en runtime, gateway o CLI
3. se actualiza la documentacion canonica
4. luego se implementa
