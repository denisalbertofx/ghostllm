"""
Slash `/do`, `/edit`, `/fix`: el contrato operativo no puede quedarse en analysis + should_not_write.

El clasificador / TaskSpec a veces etiqueta mal tareas de implementación; esto fuerza alineación
con la intención explícita del operador sin tocar quotas ni database.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from apps.cli.runtime.task_contract import (
    Intent,
    get_task_contract_spec,
    update_task_contract_after_spec_apply,
)

logger = logging.getLogger(__name__)

SLASH_WRITE_MODES = frozenset({"Execute", "Patch", "Fix"})


def apply_slash_execute_write_overrides(session: Any, intent: Intent) -> bool:
    """
    Si el usuario usó `/do` (o patch/fix) y el spec quedó read-only, reescribe intent/CE en sesión.

    Returns True si se aplicó corrección.
    """
    if not getattr(intent, "is_slash_command", False):
        return False
    if str(getattr(intent, "mode", "") or "").strip() not in SLASH_WRITE_MODES:
        return False
    spec = get_task_contract_spec(session)
    if not isinstance(spec, dict):
        return False
    intent_spec = str(spec.get("intent") or "").strip().lower()
    ce = str(spec.get("change_expectation") or "").strip().lower()
    if intent_spec not in ("analysis", "review") and ce != "should_not_write":
        return False

    task_blob = (
        str(getattr(session, "task", "") or "") + " " + str(getattr(intent, "task", "") or "")
    ).lower()
    bugish = any(
        x in task_blob
        for x in (
            "bug",
            "fix",
            "corrige",
            "corregir",
            "error",
            "fallo",
            "robust",
            "robustez",
            "patch",
            "regresión",
            "regression",
        )
    )
    new_intent = "bugfix" if bugish else "implementation"
    new_spec = dict(spec)
    new_spec["intent"] = new_intent
    if ce == "should_not_write":
        new_spec["change_expectation"] = "may_write"

    session.task_intent = new_intent
    if ce == "should_not_write":
        session.change_expectation = "may_write"
    update_task_contract_after_spec_apply(session, new_spec)

    try:
        session.events.append(
            {
                "event": "slash_execute_contract_override",
                "prior_spec_intent": intent_spec,
                "prior_change_expectation": ce,
                "new_intent": new_intent,
                "new_change_expectation": str(new_spec.get("change_expectation") or ""),
            }
        )
    except Exception:
        pass
    logger.info(
        "slash_execute_contract_override %s",
        json.dumps(
            {"from": [intent_spec, ce], "to": [new_intent, new_spec.get("change_expectation")]},
            ensure_ascii=False,
        ),
    )
    return True
