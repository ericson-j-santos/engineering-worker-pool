"""Exact reviewed hidden-path exception, never a general dotfile allowance.

Only the installed Noteri42 Gateway diagnostic adapter may stage/recover this
single versioned workflow; arbitrary .git/.github paths remain forbidden.
"""
from __future__ import annotations

ADAPTER = "noteri42.gateway-diagnostic.v1"
PATH = ".github/workflows/noteri-control-plane-probe.yml"


def is_reviewed_hidden_path(adapter_id: object, path: object) -> bool:
    return type(adapter_id) is str and adapter_id == ADAPTER and (
        type(path) is str and path == PATH
    )
