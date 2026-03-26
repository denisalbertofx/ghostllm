"""
GEP-10: Implementation Quality Checks.
Semantic heuristics to avoid falsely marking buggy code as already_implemented.
Deterministic, pattern-based. No full parsing.
"""
import json
import re
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional

QUALITY_VALID = "valid"
QUALITY_SUSPICIOUS = "suspicious"
QUALITY_INCORRECT = "incorrect"


@dataclass
class QualityCheckResult:
    quality_status: str  # valid | suspicious | incorrect
    confidence: float
    issues_found: List[str] = field(default_factory=list)
    evidence_lines: List[str] = field(default_factory=list)
    recommended_action: str = ""


def _extract_read_file_contents(tool_history: List[Dict[str, Any]]) -> List[str]:
    """Extract file contents from read_file tool results."""
    contents: List[str] = []
    for m in tool_history:
        if m.get("role") != "tool" or m.get("name") != "read_file":
            continue
        try:
            raw = m.get("content", "")
            res = json.loads(raw) if isinstance(raw, str) and raw.strip().startswith("{") else {}
            c = res.get("content", "")
            if c and isinstance(c, str):
                contents.append(c)
        except Exception:
            pass
    return contents


def _check_zod_raw_input_antipattern(code: str) -> Optional[str]:
    """
    Check: Zod safeParse exists, but parsed typed value is not used downstream.
    Raw input variable used in typed ORM filter (eq, where) instead of parseResult.data.
    Returns issue text or None.
    """
    code_nl = code.replace("\r", "\n")
    # Normalize to catch common patterns
    if ".safeParse(" not in code and "safeParse(" not in code:
        return None

    # Find variables passed to safeParse: schema.safeParse(status) or safeParse(status)
    safeparse_match = re.search(
        r"(?:\.safeParse|safeParse)\s*\(\s*(\w+)\s*\)",
        code_nl,
        re.IGNORECASE
    )
    if not safeparse_match:
        return None

    raw_var = safeparse_match.group(1)

    # Check if parseResult.data or .data is used in downstream typed ops
    # Look for: parseResult.data, result.data, parsed.data, validatedStatus, etc.
    uses_parsed = bool(re.search(
        r"(?:parseResult|parsed|result|validated)\s*\.\s*data\b",
        code_nl,
        re.IGNORECASE
    ))
    if uses_parsed:
        return None

    # Check for typed filter operations that use the raw variable
    # eq(column, rawVar), findMany({ where: ... eq(..., rawVar)
    # or status ? eq(issues.status, status)
    eq_pattern = re.compile(
        r"eq\s*\(\s*\w+\.(\w+)\s*,\s*" + re.escape(raw_var) + r"\s*\)",
        re.IGNORECASE
    )
    if eq_pattern.search(code_nl):
        return (
            f"Zod validation found, but parsed typed value is not used downstream. "
            f"Raw '{raw_var}' is used in eq() instead of parseResult.data"
        )

    # Broader: raw var in where clause
    where_block = re.search(r"where\s*:\s*\{[^}]*\}", code_nl, re.DOTALL)
    if where_block and raw_var in where_block.group(0):
        return (
            f"Raw query param '{raw_var}' is used in typed ORM filter after validation. "
            f"Use parseResult.data for type safety"
        )

    return None


def _check_validation_without_reuse(code: str) -> Optional[str]:
    """Validation exists but validated data is not assigned or reused."""
    if "safeParse" not in code and "parse(" not in code:
        return None
    if ".success" in code or "!parseResult.success" in code:
        if "parseResult.data" not in code and "result.data" not in code and "parsed." not in code:
            return "Validation exists but validated data is not assigned or reused"
    return None


def evaluate_implementation_quality(
    session: Any,
    tool_history: List[Dict[str, Any]],
    messages: Optional[List[Dict[str, Any]]] = None,
) -> QualityCheckResult:
    """
    Run semantic quality checks on code that was read during the session.
    Used to downgrade already_implemented when implementation has structural defects.
    """
    contents = _extract_read_file_contents(tool_history)
    if not contents:
        return QualityCheckResult(
            quality_status=QUALITY_VALID,
            confidence=0.5,
            issues_found=[],
            evidence_lines=["No file contents to check"],
            recommended_action="",
        )

    combined = "\n---FILE---\n".join(contents)
    issues: List[str] = []
    evidence: List[str] = []

    # Rule 1: Zod safeParse + raw input in typed op
    z1 = _check_zod_raw_input_antipattern(combined)
    if z1:
        issues.append(z1)
        evidence.append("Zod validation found, but parsed typed value is not used downstream")
        evidence.append("Raw query param is used in typed ORM filter after validation")

    # Rule 2: Validation without reuse
    z2 = _check_validation_without_reuse(combined)
    if z2:
        issues.append(z2)
        evidence.append("Feature exists structurally, but implementation quality check failed")

    if not issues:
        return QualityCheckResult(
            quality_status=QUALITY_VALID,
            confidence=0.85,
            issues_found=[],
            evidence_lines=[],
            recommended_action="",
        )

    # Determine severity
    has_critical = any(
        "eq(" in i or "parseResult.data" in i or "typed" in i.lower()
        for i in issues
    )
    status = QUALITY_INCORRECT if has_critical else QUALITY_SUSPICIOUS
    confidence = 0.9 if has_critical else 0.75

    return QualityCheckResult(
        quality_status=status,
        confidence=confidence,
        issues_found=issues,
        evidence_lines=list(dict.fromkeys(evidence)),
        recommended_action="Fix the implementation: use parseResult.data (or validated value) for typed ORM operations instead of raw input.",
    )
