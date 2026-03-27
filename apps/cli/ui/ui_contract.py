"""
Contrato estable de strings de UI para Ghost CLI.

Los tests y el renderer deben preferir estas constantes frente a literales sueltos,
para que ajustes de copy visual no rompan medio repositorio.

Regla: si cambias el texto visible, actualiza aquí y los tests que importen la constante.

Variables de entorno relacionadas (presentación, no negocio):

- ``GHOST_UI_DENSITY``: ``compact`` | ``normal`` (default) | ``comfortable`` —
  controla padding, líneas visibles y recortes vía ``apps.cli.ui.visual_layout``.
- ``GHOST_ASCII_UI``: ``1`` fuerza marcadores/bordes ASCII (véase ``terminal_capability``).
- ``GHOST_UI_VERBOSE``: auditoría extendida en consola.
"""
from __future__ import annotations

# ── Marca (copy corto, estable) ─────────────────────────────────────────────
BRAND_WORDMARK = "Ghost"
BRAND_RUNNER_SUBTITLE = "coding agent · terminal-first"
BRAND_RUNNER_VALUES = "continuidad · memoria · ejecucion"
STARTUP_LABEL_WORKSPACE = "workspace"
STARTUP_LABEL_RUNTIME = "runtime"
STARTUP_LABEL_EMPIEZA = "empieza con"
STARTUP_HINT_SLASH = "escribe `/` para abrir acciones"
STARTUP_HINT_CONTROLS = "↑↓ mueve · Enter o Tab completa · Esc cierra"

# ── Phase rail (format_phase_rail / live status) ────────────────────────────
PHASE_RAIL_LABEL_EXPLORAR = "Explorar"
PHASE_RAIL_LABEL_ACTUAR = "Actuar"
PHASE_RAIL_LABEL_VERIFICAR = "Verificar"
PHASE_RAIL_LABEL_CIERRE = "Cierre"
# Rail /plan (solo lectura): sin paso «Actuar» de escritura.
PHASE_RAIL_LABEL_REVISAR = "Revisar"

# Perfiles del rail vivo (assistant → renderer.session_status)
LIVE_RAIL_PROFILE_IMPLEMENT = "implement"
LIVE_RAIL_PROFILE_READONLY = "readonly"

# ── Línea viva: estado por defecto (sin herramienta concreta) ───────────────
LIVE_STATE_THINKING = "Modelo en curso — diseñando el siguiente movimiento"
LIVE_STATE_WAITING_MODEL = "Enlace con el modelo — cola o red"
LIVE_STATE_TOOL_GENERIC = "Herramientas sobre el workspace"
LIVE_STATE_TOOL_RESULT = "Integrando salida de herramientas"
LIVE_STATE_STREAMING = "Ensamblando respuesta (stream)"
LIVE_STATE_CLOSING = "Cerrando sesión con síntesis"
LIVE_STATE_READY = "Listo para input"
LIVE_STATE_WORKING = "En progreso"

# ── Línea viva: acciones con contexto corto ─────────────────────────────────
LIVE_ACTION_READING = "Leyendo"
LIVE_ACTION_PATCHING = "Parcheando"
LIVE_ACTION_WRITING = "Escribiendo"
LIVE_ACTION_DELETING = "Eliminando"
LIVE_ACTION_SHELL = "Shell ·"
LIVE_ACTION_LISTING = "Listando"
LIVE_ACTION_SEARCH = "Buscando en código"
LIVE_ACTION_MAP = "Mapeando el repositorio…"
LIVE_ACTION_MULTI_TOOLS = " (+{n} más)"

# ── Paneles read-only (/plan) ───────────────────────────────────────────────
PANEL_TITLE_REVIEW = "Revisión"
PANEL_SUBTITLE_REVIEW = "solo lectura · afirmaciones ancladas a evidencia"
# Cierre /do: vista tipo PR en consola (markdown derivado del packet)
PANEL_TITLE_REVIEW_PACKET_TTY = "Review packet"
PANEL_SUBTITLE_REVIEW_PACKET_TTY = "exportable · mismo contenido en JSON bajo .ghost/review_packets/"
PANEL_TITLE_EVIDENCIA_TIER = "Evidencia (tier)"
PANEL_TITLE_EVIDENCIA_HERRAMIENTAS = "Evidencia (herramientas)"
PANEL_TITLE_DEBUG = "Debug"
# Cuerpo del panel Debug cuando hay digest de grounding (prefijo antes del texto).
DEBUG_GROUNDING_PREFIX = "Grounding:"

# ── Tier / confianza (subcadena estable en badge read-only) ─────────────────
TIER_SUBSTRING_SIN_VERIFICAR = "sin verificar"

# ── Herramientas (GHOST_TOOL_UI=panel) ─────────────────────────────────────
# Fragmento que debe permanecer en el título de la tabla de lote.
TOOL_TABLE_TITLE_MARKER = "lote de herramientas"

# ── Verificación (render_verification_results, cierre) ─────────────────────
LABEL_VERIFY_LINE = "Verify"
VERIFY_CHECK_DISPLAY_NOT_RUN = "NOT RUN"
VERIFY_PYTEST_SUMMARY_HEADER_PREFIX = "VERIFY · salida de tests"
# Título completo del panel extracto pytest (sin sufijo de duración).
VERIFY_PYTEST_SUMMARY_HEADER_EXTRACTO = "VERIFY · salida de tests (extracto)"
RESULTADO_LINE_FAILED = "Resultado: FAILED"
RESULTADO_LINE_OK = "Resultado: OK"
RESULTADO_LINE_INCOMPLETO = "Resultado: incompleto"
VERIFY_MSG_NO_CHECKS_ON_DISK = "Ningún check se ejecutó en disco"
VERIFY_MSG_CHECKS_PASSED = "Checks pasados"
VERIFY_MSG_ALL_SYSTEMS_CLEAR = "ALL SYSTEMS CLEAR"

# ── Reglas editoriales (_ghost_rule_markup argumentos) ─────────────────────
RULE_LABEL_HERRAMIENTAS = "herramientas"
RULE_LABEL_VERIFY = "verify"
RULE_LABEL_RESULTADO = "resultado"

# ── Kickers de bloque (operador, no verbose) ────────────────────────────────
KICKER_RESULTADO = "Resultado"
KICKER_CAMBIOS = "Cambios"
KICKER_VERIFY = "Verify"
KICKER_SIGUIENTE_PASO = "Siguiente paso"
KICKER_WORKSET = "Ámbito de trabajo"
KICKER_PLAN = "Plan operativo"
KICKER_DIAGNOSTICO = "Diagnóstico"
KICKER_SESION_CERRADA = "Sesión cerrada"

# ── Artefactos persistidos (línea compacta) ───────────────────────────────────
ARTIFACT_LINE_MARKER = "artefactos"
# Etiquetas de salidas (orden de prioridad en cierre operador)
ARTIFACT_SALIDA_REVIEW_PACKET = "Review packet"
ARTIFACT_SALIDA_RESUMEN_MD = "Resumen de sesión"
ARTIFACT_SALIDA_PLAN = "Plan persistido"
ARTIFACT_SALIDA_HANDOFF = "Handoff"
ARTIFACT_SALIDA_ENVELOPE = "Delegation / envelope"
ARTIFACT_SALIDA_TRACE = "Trace (JSON)"
ARTIFACT_SALIDA_CARPETA = "Carpeta de artefactos"

# ── Assistant / aprobaciones (mensaje al operador) ─────────────────────────
MSG_APPROVAL_SAME_BUNDLE = (
    "Mismo lote que antes: se mantiene la decisión de aprobación previa."
)
MSG_ACTIVE_WORKSET_PLAIN_PREFIX = "Active workset:"

# ── Markdown de trazas largas (session_trace → operador) ─────────────────────
TRACE_MD_ACTIVE_WORKSET_BOLD = "**Active workset:**"

# ── Cuerpo PR / review packet (harness_bundle, exportable a GH / .md) ───────────
# Títulos estables tipo PR (español, escaneables).
HARNESS_PR_H2_SUMMARY = "## Resumen ejecutivo"
HARNESS_PR_H2_WHAT_CHANGED = "## Cambios principales"
HARNESS_PR_H2_FILES = "## Archivos tocados"
HARNESS_PR_H2_VERIFICATION = "## Verificación"
HARNESS_PR_H2_OPEN_RISKS = "## Riesgos abiertos"
HARNESS_PR_H2_REVIEWER = "## Qué debe revisar el reviewer"
HARNESS_PR_H2_LINKS_HANDOFF = "## Continuidad y enlaces"
HARNESS_PR_H2_NEXT_OPERATOR = "## Siguiente paso (operador)"
# Compat: antes se usaba «Context» como párrafo largo; ahora va bajo Cambios principales.
HARNESS_PR_H2_CONTEXT = HARNESS_PR_H2_WHAT_CHANGED

# ── Cierre `/do` con cambios (tono producto, sin PR real) ─────────────────────
CLOSURE_REVIEW_STATUS_READY = "Listo para review"
CLOSURE_REVIEW_STATUS_PENDING = "Aún no listo para review"
CLOSURE_DIFF_REVIEW_CAPTION = "Cambios revisables"

# ── Verbose-only (GHOST_UI_VERBOSE=1) — no deben filtrarse al operador compacto ─
PANEL_TITLE_CONTRACT_RESUMEN = "Contract (resumen)"
PANEL_TITLE_CONTRACT_SPEC = "Contract spec"

# Patrón antiguo de UI que el modo compacto no debe reintroducir.
LEGACY_TOOLS_LOTE_PHRASE = "Tools · lote"


def output_contains_review_panel(text: str) -> bool:
    """True si el cierre read-only incluyó el panel de revisión."""
    return PANEL_TITLE_REVIEW in (text or "")


def output_contains_verify_brand_or_rule(text: str) -> bool:
    """True si hay bloque de verificación operador (marca Verify o regla · verify ·)."""
    t = text or ""
    return LABEL_VERIFY_LINE in t or f"· {RULE_LABEL_VERIFY} ·" in t.lower()


def output_contains_artifact_footer_line(text: str) -> bool:
    """Línea compacta de artefactos persistidos (marca ◇ + «artefactos»)."""
    return ARTIFACT_LINE_MARKER in (text or "")


def live_rail_has_step_labels(text: str, *labels: str) -> bool:
    """True si el texto del spinner incluye todas las etiquetas de fase (orden libre)."""
    t = text or ""
    return all(lab in t for lab in labels)


def live_rail_shows_current_marker(text: str) -> bool:
    """Paso actual marcado con ● (Unicode) o * (modo ASCII)."""
    t = text or ""
    return "●" in t or "*" in t
