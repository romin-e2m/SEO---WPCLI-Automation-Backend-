from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from app.schemas.schema import (
    ActionSchema,
    CreateActionSchemaRequest,
    UpdateActionSchemaRequest,
)

from app.services.builtin_action_definitions import builtin_action_schemas

logger = logging.getLogger(__name__)


class SchemaManager:
    """Manage action schemas (built-in and user-defined)."""

    def __init__(self, storage_path: str | None = None):
        """Initialize schema manager.
        
        Args:
            storage_path: Path to JSON file for persisting user-defined schemas.
                         If None, uses in-memory storage only.
        """
        self.storage_path = Path(storage_path) if storage_path else None
        self._schemas: dict[str, ActionSchema] = {}
        self._load_builtin_schemas()
        if self.storage_path and self.storage_path.exists():
            self._load_from_file()

    def _load_builtin_schemas(self) -> None:
        """Load system built-in schemas."""
        for schema in builtin_action_schemas():
            self._schemas[schema.id] = schema

    def _load_from_file(self) -> None:
        """Load user-defined schemas from JSON file."""
        if not self.storage_path or not self.storage_path.exists():
            return
        try:
            with open(self.storage_path, "r") as f:
                data = json.load(f)
            for schema_data in data.get("schemas", []):
                try:
                    schema = ActionSchema(**schema_data)
                    self._schemas[schema.id] = schema
                except Exception as e:
                    logger.warning(f"Failed to load schema {schema_data.get('id', 'unknown')}: {e}")
        except Exception as e:
            logger.error(f"Failed to load schemas from file {self.storage_path}: {e}")

    def _save_to_file(self) -> None:
        """Persist user-defined schemas to JSON file."""
        if not self.storage_path:
            return
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        user_schemas = [s for s in self._schemas.values() if not s.is_system]
        with open(self.storage_path, "w") as f:
            json.dump({"schemas": [s.model_dump() for s in user_schemas]}, f, indent=2)

    def list_schemas(self) -> list[ActionSchema]:
        """Return all available schemas (built-in + user-defined)."""
        return sorted(self._schemas.values(), key=lambda s: (not s.is_system, s.id))

    def get_schema(self, schema_id: str) -> ActionSchema | None:
        """Get a schema by ID."""
        return self._schemas.get(schema_id)

    def create_schema(self, request: CreateActionSchemaRequest) -> ActionSchema:
        """Create a new user-defined schema."""
        if request.id in self._schemas:
            raise ValueError(f"Schema '{request.id}' already exists.")
        
        now = datetime.now(timezone.utc).isoformat()
        schema = ActionSchema(
            id=request.id,
            label=request.label,
            description=request.description,
            fields=request.fields,
            any_of_groups=request.any_of_groups,
            created_at=now,
            updated_at=now,
            is_system=False,
        )
        self._schemas[schema.id] = schema
        self._save_to_file()
        return schema

    def update_schema(self, schema_id: str, request: UpdateActionSchemaRequest) -> ActionSchema:
        """Update an existing user-defined schema."""
        schema = self._schemas.get(schema_id)
        if not schema:
            raise ValueError(f"Schema '{schema_id}' not found.")
        if schema.is_system:
            raise ValueError(f"Cannot modify built-in schema '{schema_id}'.")
        
        now = datetime.now(timezone.utc).isoformat()
        updated = schema.model_copy(update={
            "label": request.label if request.label is not None else schema.label,
            "description": request.description if request.description is not None else schema.description,
            "fields": request.fields if request.fields is not None else schema.fields,
            "any_of_groups": request.any_of_groups if request.any_of_groups is not None else schema.any_of_groups,
            "updated_at": now,
        })
        self._schemas[schema_id] = updated
        self._save_to_file()
        return updated

    def delete_schema(self, schema_id: str) -> None:
        """Delete a user-defined schema."""
        schema = self._schemas.get(schema_id)
        if not schema:
            raise ValueError(f"Schema '{schema_id}' not found.")
        if schema.is_system:
            raise ValueError(f"Cannot delete built-in schema '{schema_id}'.")
        
        del self._schemas[schema_id]
        self._save_to_file()

    def infer_schema(self, sheet_name: str, columns: list[str]) -> tuple[ActionSchema | None, float, str]:
        """Attempt to infer a schema from sheet name and columns.
        
        Returns: (schema, confidence, reasoning)
        """
        # Normalize for matching
        sheet_lower = sheet_name.lower().replace(" ", "_").replace("-", "_")
        cols_lower = {c.lower() for c in columns}
        
        # Score each schema
        scores: dict[str, tuple[float, str]] = {}
        
        for schema in self._schemas.values():
            score = 0.0
            matches = []
            
            # Check sheet name keywords
            schema_keywords = schema.id.lower().split("_")
            # Convert sheet name to words (split by _ and space)
            sheet_words = set(sheet_lower.replace("_", " ").split())
            matched_keywords = 0
            for kw in schema_keywords:
                if kw in sheet_words:
                    matched_keywords += 1
                    score += 0.3
                    matches.append(f"Sheet name contains '{kw}'")
            # Bonus for multi-word matches (e.g., "meta" + "description" for "meta" schema)
            if matched_keywords > 1:
                score += 0.2 * matched_keywords
                matches.append(f"Matched {matched_keywords} keywords from schema name")
            
            # Check for required fields
            # Be defensive: older / malformed schema payloads might have `fields`
            # as a list of strings instead of ActionField objects.
            fields = []
            for f in (schema.fields or []):
                if isinstance(f, str):
                    fields.append({"key": f, "required": False})
                else:
                    fields.append(f)

            required_fields = []
            for f in fields:
                if isinstance(f, dict):
                    if f.get("required") and isinstance(f.get("key"), str):
                        required_fields.append(f["key"])
                else:
                    if getattr(f, "required", False) and isinstance(getattr(f, "key", None), str):
                        required_fields.append(f.key)
            if required_fields:
                matched_required = 0
                for key in required_fields:
                    key_normalized = key.lower().replace("_", " ")
                    # Check exact match or if column contains the field name
                    for col in cols_lower:
                        col_normalized = col.replace("_", " ")
                        if key_normalized in col_normalized or col_normalized in key_normalized:
                            matched_required += 1
                            break
                req_ratio = matched_required / len(required_fields)
                score += req_ratio * 0.5
                if matched_required > 0:
                    matches.append(f"Matched {matched_required}/{len(required_fields)} required fields")
            
            # Check for any fields
            all_keys: set[str] = set()
            for f in fields:
                if isinstance(f, dict):
                    key = f.get("key")
                    if isinstance(key, str):
                        all_keys.add(key.lower())
                else:
                    key = getattr(f, "key", None)
                    if isinstance(key, str):
                        all_keys.add(key.lower())
            if len(all_keys) > 0:
                matched_any = 0
                for key in all_keys:
                    key_normalized = key.replace("_", " ")
                    for col in cols_lower:
                        col_normalized = col.replace("_", " ")
                        if key_normalized in col_normalized or col_normalized in key_normalized:
                            matched_any += 1
                            break
                any_ratio = matched_any / len(all_keys)
                score += any_ratio * 0.2
                if matched_any > 0:
                    matches.append(f"Matched {matched_any}/{len(all_keys)} fields")
            
            scores[schema.id] = (score, "; ".join(matches) or "No matching criteria")
        
        # Find best match
        best_id = max(scores.keys(), key=lambda k: scores[k][0])
        best_score, reasoning = scores[best_id]
        
        if best_score < 0.3:  # Threshold for confidence
            return None, best_score, "No schema matched with sufficient confidence."
        
        return self._schemas[best_id], best_score, reasoning
