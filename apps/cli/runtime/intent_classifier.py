"""
GEP-9: Task Intent Classifier.
Rule-based classification of user task intent before execution (DISCOVERY).
Deterministic, priority-based. Strong verbs at start override later keywords.
"""
import re
from dataclasses import dataclass, field
from typing import List, Tuple

from apps.cli.runtime.planning_task import detect_strategy_plan_request
from apps.cli.runtime.repo_overview_task import detect_repo_overview_question

# Intent types
INTENT_IMPLEMENTATION = "implementation"
INTENT_MODIFICATION = "modification"
INTENT_BUGFIX = "bugfix"
INTENT_REVIEW = "review"
INTENT_ANALYSIS = "analysis"
INTENT_REFACTOR = "refactor"
INTENT_VERIFICATION = "verification"

# Scope
SCOPE_UI = "ui"
SCOPE_API = "api"
SCOPE_DATA = "data"
SCOPE_INFRA = "infra"
SCOPE_FULLSTACK = "fullstack"
SCOPE_UNKNOWN = "unknown"

# Change expectation
EXPECT_MUST_WRITE = "must_write"
EXPECT_MAY_WRITE = "may_write"
EXPECT_SHOULD_NOT_WRITE = "should_not_write"


@dataclass
class TaskIntentResult:
    intent: str
    scope: str
    change_expectation: str
    confidence: float
    reasoning_lines: List[str] = field(default_factory=list)


# Strong verbs at START of request have highest priority (checked first)
_START_VERBS: List[Tuple[List[str], str, str]] = [
    (["implement", "implementa", "add", "añade", "agrega", "build", "construye", "create", "crea",
      "conecta", "connect", "escribe", "write", "añadir", "agregar"], INTENT_IMPLEMENTATION, EXPECT_MUST_WRITE),
    (["fix", "arregla", "repair", "repara", "resolve", "resolver", "broken", "corrige", "corregir"],
     INTENT_BUGFIX, EXPECT_MUST_WRITE),
    (["review", "revisa", "inspect", "inspecciona", "check", "chequea", "revisar"],
     INTENT_REVIEW, EXPECT_SHOULD_NOT_WRITE),
    (["verify", "verifica", "confirm", "confirma", "validate", "valida", "comprueba", "comprobar"],
     INTENT_VERIFICATION, EXPECT_MAY_WRITE),
    (["update", "actualiza", "change", "cambia", "adjust", "ajusta", "modify", "modifica"],
     INTENT_MODIFICATION, EXPECT_MUST_WRITE),
    (["analyze", "analiza", "understand", "investigate", "investiga", "research"],
     INTENT_ANALYSIS, EXPECT_SHOULD_NOT_WRITE),
    (["refactor", "refactoriza", "clean up", "limpia", "restructure", "reestructura"],
     INTENT_REFACTOR, EXPECT_MAY_WRITE),
]

# Fallback: keywords anywhere (lower priority)
_INTENT_FALLBACK = [
    (["implement", "implementa", "add", "añade", "agrega", "build", "create", "crea", "conecta", "connect",
      "endpoint", "api", "schema", "añadir", "agregar"], INTENT_IMPLEMENTATION, EXPECT_MUST_WRITE),
    (["update", "actualiza", "change", "cambia", "adjust", "modify", "modifica", "edita", "edit"],
     INTENT_MODIFICATION, EXPECT_MUST_WRITE),
    (["fix", "arregla", "repair", "repara", "resolve bug", "not working", "no funciona",
      "broken", "corrige", "fallo", "falla", "error", "bug"], INTENT_BUGFIX, EXPECT_MUST_WRITE),
    (["review", "revisa", "inspect", "check", "chequea"], INTENT_REVIEW, EXPECT_SHOULD_NOT_WRITE),
    (["analyze", "analiza", "understand", "investigate", "busca", "encuentra", "research"], INTENT_ANALYSIS, EXPECT_SHOULD_NOT_WRITE),
    (["refactor", "refactoriza", "clean up", "limpia", "restructure"], INTENT_REFACTOR, EXPECT_MAY_WRITE),
    (["verify", "verifica", "confirm", "validate", "comprueba"], INTENT_VERIFICATION, EXPECT_MAY_WRITE),
]

# Scope keywords
_SCOPE_KEYWORDS = [
    (["ui", "frontend", "componente", "component", "página", "page", "layout", "estilos", "styles"], SCOPE_UI),
    (["api", "endpoint", "route", "ruta", "controller", "handler", "rest", "get /api", "post /api"], SCOPE_API),
    (["data", "schema", "drizzle", "sqlite", "database", "db", "migration", "modelo", "model", "tabla", "table"], SCOPE_DATA),
    (["infra", "config", "configuración", "docker", "env", "deploy", "ci", "pipeline"], SCOPE_INFRA),
]

# Intent softening: phrases that allow no-op / conditional execution -> soften must_write to may_write
_SOFTENING_PHRASES = [
    "only if needed",
    "only if necessary",
    "only necessary changes",
    "make only necessary changes",
    "haz solo los cambios necesarios",
    "solo los cambios necesarios",
    "if already implemented",
    "if already present",
    "if it already exists",
    "si ya existe",
    "si ya está implementado",
    "report what you verified",
    "report exactly what you verified",
    "concluye con total precisión qué verificaste",
    "qué verificaste de verdad",
    "qué no ejecutaste",
    "do not change unless required",
    "don't change unless required",
    "only run checks if needed",
    "ejecuta únicamente un check real si de verdad hace falta",
    "run a real check only if truly needed",
    "no changes needed",
    "explain with evidence",
    "explain with evidence",
]

# Scope EXCLUSIONS: if present, never use fullstack; respect explicit bounds
_SCOPE_EXCLUSIONS = [
    (["no toques la ui", "do not touch ui", "no toques ui", "solo datos", "solo api", "only data", "only api",
      "solo capa de datos", "solo capa de api", "trabaja solo en la capa de datos", "trabaja solo en la capa de api",
      "capa de datos y api", "data and api", "datos y api", "no tocar la ui", "don't touch ui"],
     ["ui"]),  # when UI excluded, never fullstack
]


def _get_start_tokens(text: str, n: int = 12) -> str:
    """Leading clause words for strong-verb matching (avoid later 'add test' clauses)."""
    t = (text or "").strip().lower()
    t = re.sub(r"^/[a-z0-9_-]+\b\s*", "", t, count=1)
    clause = re.split(r"[.!?;\n]", t, maxsplit=1)[0]
    words = re.split(r"\s+", clause, maxsplit=n)[:n]
    return " " + " ".join(words) + " "


def _check_scope_exclusions(text: str) -> bool:
    """True if scope exclusions apply (e.g. UI explicitly excluded)."""
    t = (text or "").strip().lower()
    for phrases, _ in _SCOPE_EXCLUSIONS:
        for phrase in phrases:
            if phrase in t:
                return True
    return False


def normalize_prompt_for_intent(user_prompt: str) -> str:
    """Lowercase body with leading slash-commands stripped once (align with classify_task_intent)."""
    t = (user_prompt or "").strip().lower()
    return re.sub(r"^/[a-z0-9_-]+\b\s*", "", t, count=1)


def explicit_implementation_or_fix_request(user_prompt: str) -> bool:
    """
    True when the user is asking to change code / fix, not only to describe or review issues.
    Used to avoid misclassifying “find the biggest bug” style prompts as bugfix.
    """
    t = normalize_prompt_for_intent(user_prompt)
    if not t:
        return False
    wrapped = f" {t} "
    markers = (
        " arregla ",
        " arreglar",
        " arreglalo",
        " corrige ",
        " corregir",
        " soluciona ",
        " implementa ",
        " implementar",
        " patch ",
        " y arregla",
        " y corrige",
        " and fix",
        " then fix",
        " fix it",
        " haz que funcione",
        " make it work",
        " crea un fix",
        " write a fix",
    )
    if any(m in wrapped for m in markers):
        return True
    if re.search(r"\b(fix|fixes|fixed|fixing|patching|patches)\b", t):
        if "bug" in t or "fallo" in t or "error" in t:
            # "fix the bug" is a fix request, not inspection-only
            return True
    if re.search(r"\b(arregla|arreglar|arreglarlo|arreglalo|corrige|corregir|corregirlo|soluciona|solucionar)\b", t):
        return True
    return False


def user_explicitly_requests_verify_shell(user_prompt: str) -> bool:
    """User asked to run lint/typecheck/build/tests explicitly (allows run_shell in read-only sessions)."""
    t = normalize_prompt_for_intent(user_prompt)
    if not t:
        return False
    needles = (
        "npm run lint",
        "npm run build",
        "npm test",
        "npx tsc",
        "yarn lint",
        "yarn build",
        "pnpm lint",
        "pytest",
        "jest",
        "run lint",
        "run tests",
        "run test",
        "run build",
        "run typecheck",
        "typecheck",
        "ejecuta lint",
        "ejecuta pytest",
        "ejecuta los tests",
        "corre pytest",
        "corre los tests",
        "mypy",
        "ruff ",
    )
    return any(n in t for n in needles)


def detect_code_inspection_readonly_prompt(user_prompt: str) -> bool:
    """
    Read-only code inspection: biggest-bug questions, review/analyze-without-fix phrasing.

    These must not be classified as bugfix/must_write just because the word "bug" appears.
    """
    t = normalize_prompt_for_intent(user_prompt)
    if not t:
        return False
    phrases = (
        "bug más grande",
        "bug mas grande",
        "mayor bug",
        "peor bug",
        "biggest bug",
        "largest bug",
        "worst bug",
        "dime el bug",
        "dime cual es el bug",
        "dime cuál es el bug",
        "cuál es el bug",
        "cual es el bug",
        "qué bug",
        "que bug",
        "what bug",
        "which bug",
        "encuentra el bug",
        "encuentra el mayor",
        "find the bug",
        "find the biggest",
        "find the worst",
        "qué problema ves",
        "que problema ves",
        "qué problema hay",
        "que problema hay",
        "what problem do you see",
        "what issue do you see",
        "haz review",
        "haz una review",
        "haz un code review",
        "code review",
        "revisión de código",
        "revision de codigo",
        "revisa el código",
        "revisa el codigo",
        "review the code",
        "security review",
        "revisión de seguridad",
    )
    if any(p in t for p in phrases):
        return True
    if ("dime" in t or "cuéntame" in t or "cuentame" in t) and "bug" in t:
        return True
    if ("cuál" in t or "cual" in t or "qué" in t or "what" in t) and (
        "bug" in t or "problema" in t or "issue" in t
    ):
        return True
    return False


def prompt_locks_taskspec_as_inspection_readonly(user_prompt: str) -> bool:
    """Post-merge guard: keep TaskSpec in read-only inspection when the prompt demands it."""
    return detect_code_inspection_readonly_prompt(user_prompt) and not explicit_implementation_or_fix_request(
        user_prompt
    )


def _keyword_present(text: str, keyword: str) -> bool:
    """
    Conservative keyword presence check.

    Short single tokens use token boundaries so `ui` does not match inside
    words like `arquitectura`.
    """
    kw = (keyword or "").strip().lower()
    if not kw:
        return False
    if re.search(r"[\s/.-]", kw):
        return kw in text
    return bool(re.search(rf"(?<![\w]){re.escape(kw)}(?![\w])", text))


def classify_task_intent(user_prompt: str) -> TaskIntentResult:
    """
    Rule-based classification. Strong verbs at start override later keywords.
    Scope exclusions (e.g. "do not touch UI") override generic multi-scope detection.
    """
    t = (user_prompt or "").strip().lower()
    t = re.sub(r"^/[a-z0-9_-]+\b\s*", "", t, count=1)
    start_tokens = _get_start_tokens(t)
    reasoning: List[str] = []

    if detect_strategy_plan_request(user_prompt):
        return TaskIntentResult(
            intent=INTENT_ANALYSIS,
            scope=SCOPE_UNKNOWN,
            change_expectation=EXPECT_SHOULD_NOT_WRITE,
            confidence=0.88,
            reasoning_lines=[
                "Intent 'analysis': strategic planning request detected",
                "Scope 'unknown': broad planning should stay repo-wide and read-only",
            ],
        )

    # 1. Strong verbs at START have highest priority
    intent = INTENT_MODIFICATION
    change_expectation = EXPECT_MAY_WRITE
    start_matched = False

    for keywords, candidate_intent, candidate_exp in _START_VERBS:
        for kw in keywords:
            if f" {kw} " in start_tokens or t.startswith(kw + " ") or t.startswith(kw + ","):
                intent = candidate_intent
                change_expectation = candidate_exp
                reasoning.append(f"Intent '{candidate_intent}': start verb '{kw}' matched")
                start_matched = True
                break
        if start_matched:
            break

    # 1b. Inspection / biggest-bug / review-without-fix before keyword fallback (avoids bare "bug" -> bugfix)
    if not start_matched and detect_code_inspection_readonly_prompt(
        user_prompt
    ) and not explicit_implementation_or_fix_request(user_prompt):
        return TaskIntentResult(
            intent=INTENT_ANALYSIS,
            scope=SCOPE_UNKNOWN,
            change_expectation=EXPECT_SHOULD_NOT_WRITE,
            confidence=0.9,
            reasoning_lines=[
                "Intent 'analysis': code inspection / biggest-bug or review-style question (read-only)",
            ],
        )

    # 2. Fallback: keywords anywhere (only if no start match)
    if not start_matched:
        for keywords, candidate_intent, candidate_exp in _INTENT_FALLBACK:
            for kw in keywords:
                if _keyword_present(t, kw):
                    intent = candidate_intent
                    change_expectation = candidate_exp
                    reasoning.append(f"Intent '{candidate_intent}': keyword '{kw}' matched")
                    break
            if reasoning and "Intent '" in reasoning[-1]:
                break

    if not start_matched and intent == INTENT_BUGFIX:
        if detect_code_inspection_readonly_prompt(user_prompt) and not explicit_implementation_or_fix_request(
            user_prompt
        ):
            intent = INTENT_ANALYSIS
            change_expectation = EXPECT_SHOULD_NOT_WRITE
            reasoning.append(
                "Intent 'analysis': inspection phrasing overrides keyword 'bug' (bugfix fallback)"
            )

    # Rule: if implementation verbs present and "error"/"invalid" only in constraints (400, invalid value),
    # keep implementation. "responde 400 con error" = constraint, not bugfix.
    if intent == INTENT_BUGFIX and any(w in t for w in ["add", "agrega", "añade", "implement", "implementa", "create", "crea"]):
        constraint_patterns = ["400", "invalid", "inválido", "error claro", "must be", "debe ser"]
        if any(p in t for p in constraint_patterns) and any(w in start_tokens for w in ["add", "agrega", "añade", "implement", "implementa"]):
            intent = INTENT_IMPLEMENTATION
            change_expectation = EXPECT_MUST_WRITE
            reasoning.append("Intent 'implementation': start verb overrides constraint-related 'error'")

    if not start_matched and intent == INTENT_MODIFICATION and detect_repo_overview_question(t):
        intent = INTENT_ANALYSIS
        change_expectation = EXPECT_SHOULD_NOT_WRITE
        reasoning.append("Intent 'analysis': repo overview question detected")

    if not reasoning:
        reasoning.append(f"Intent '{intent}': no specific keyword matched, using default")

    # 3. Scope: check exclusions first
    scope_excluded = _check_scope_exclusions(t)
    if scope_excluded:
        reasoning.append("Scope exclusion: UI/explicit bounds detected")

    scope = SCOPE_UNKNOWN
    scope_matches = []
    for keywords, candidate_scope in _SCOPE_KEYWORDS:
        for kw in keywords:
            if _keyword_present(t, kw):
                scope_matches.append(candidate_scope)
                break

    if scope_excluded:
        scope_matches = [s for s in scope_matches if s != SCOPE_UI]
        if SCOPE_API in scope_matches or "api" in t or "endpoint" in t or "route" in t:
            scope = SCOPE_API
            reasoning.append("Scope 'api': explicit data/API layer constraint")
        elif SCOPE_DATA in scope_matches:
            scope = SCOPE_DATA
            reasoning.append("Scope 'data': explicit data layer constraint")
        elif scope_matches:
            scope = scope_matches[0]
        else:
            scope = SCOPE_API  # default when "only data/API"
            reasoning.append("Scope 'api': implied by exclusion of UI")
    elif len(scope_matches) >= 2:
        scope = SCOPE_FULLSTACK
        reasoning.append("Scope 'fullstack': multiple scopes detected")
    elif scope_matches:
        scope = scope_matches[0]
        reasoning.append(f"Scope '{scope}': keyword matched")

    if scope == SCOPE_UNKNOWN:
        reasoning.append("Scope 'unknown': no scope keywords found")

    # Second-pass: intent softening — soften must_write to may_write when conditional/no-op acceptance
    if change_expectation == EXPECT_MUST_WRITE:
        for phrase in _SOFTENING_PHRASES:
            if phrase in t:
                change_expectation = EXPECT_MAY_WRITE
                reasoning.append(f"Intent softening: '{phrase}' -> change_expectation=may_write")
                break

    confidence = 0.9 if start_matched else 0.75 if scope != SCOPE_UNKNOWN else 0.6
    return TaskIntentResult(
        intent=intent,
        scope=scope,
        change_expectation=change_expectation,
        confidence=confidence,
        reasoning_lines=reasoning,
    )
