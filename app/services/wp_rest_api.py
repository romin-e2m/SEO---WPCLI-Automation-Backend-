"""
Deprecated compatibility shim.

Import from ``app.services.wp_rest`` instead (``TaskType``, ``RedirectSource``,
``WordPressRESTClient``, ``DryRunResult``, ``OperationResult``).
"""

from app.services.wp_rest import (
    DryRunResult,
    OperationResult,
    RedirectSource,
    TaskType,
    WordPressRESTClient,
)

__all__ = [
    "DryRunResult",
    "OperationResult",
    "RedirectSource",
    "TaskType",
    "WordPressRESTClient",
]
