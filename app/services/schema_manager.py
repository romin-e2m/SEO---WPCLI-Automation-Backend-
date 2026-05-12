from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from app.schemas.schema import (
    ActionSchema,
    AnyOfGroup,
    FieldDefinition,
    CreateActionSchemaRequest,
    UpdateActionSchemaRequest,
)

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
        builtin = [
            ActionSchema(
                id="on_page",
                label="On-Page (H1)",
                description="Manage page H1 tags",
                fields=[
                    FieldDefinition(key="page_url", label="Page URL", required=True, type="url"),
                    FieldDefinition(key="current_h1", label="Current H1", type="text"),
                    FieldDefinition(key="recommended_h1", label="Recommended H1", type="text"),
                    FieldDefinition(key="notes", label="Notes", type="text"),
                ],
                any_of_groups=[
                    AnyOfGroup(fields=["recommended_h1"])
                ],
                created_at=datetime.now(timezone.utc).isoformat(),
                updated_at=datetime.now(timezone.utc).isoformat(),
                is_system=True,
            ),
            ActionSchema(
                id="meta",
                label="Meta description",
                description="Update meta descriptions for pages",
                fields=[
                    FieldDefinition(key="page_url", label="Page URL", required=True, type="url"),
                    FieldDefinition(key="current_meta_description", label="Current meta description", type="text"),
                    FieldDefinition(key="recommended_meta_description", label="Recommended meta description", required=True, type="text"),
                    FieldDefinition(key="notes", label="Notes", type="text"),
                ],
                created_at=datetime.now(timezone.utc).isoformat(),
                updated_at=datetime.now(timezone.utc).isoformat(),
                is_system=True,
            ),
            ActionSchema(
                id="meta_title",
                label="SEO meta title",
                description="Update SEO title (search snippet title) for pages and posts",
                fields=[
                    FieldDefinition(key="page_url", label="Page URL", required=True, type="url"),
                    FieldDefinition(key="current_meta_title", label="Current meta title", type="text"),
                    FieldDefinition(key="recommended_meta_title", label="Recommended meta title", required=True, type="text"),
                    FieldDefinition(key="notes", label="Notes", type="text"),
                ],
                created_at=datetime.now(timezone.utc).isoformat(),
                updated_at=datetime.now(timezone.utc).isoformat(),
                is_system=True,
            ),
            ActionSchema(
                id="images",
                label="Image alt text",
                description="Add or update image alt text",
                fields=[
                    FieldDefinition(key="page_url", label="Page URL", required=True, type="url"),
                    FieldDefinition(key="image_url", label="Image URL", required=True, type="url"),
                    FieldDefinition(key="current_alt_text", label="Current alt text", type="text"),
                    FieldDefinition(key="recommended_alt_text", label="Recommended alt text", required=True, type="text"),
                    FieldDefinition(key="notes", label="Notes", type="text"),
                ],
                created_at=datetime.now(timezone.utc).isoformat(),
                updated_at=datetime.now(timezone.utc).isoformat(),
                is_system=True,
            ),
            ActionSchema(
                id="url_cleanup",
                label="URL cleanup in content",
                description="Replace old URLs with new ones in content",
                fields=[
                    FieldDefinition(key="page_url", label="Page URL", required=True, type="url"),
                    FieldDefinition(key="old_url", label="URL to replace", required=True, type="url"),
                    FieldDefinition(key="new_url", label="New URL", required=True, type="url"),
                    FieldDefinition(key="notes", label="Notes", type="text"),
                ],
                created_at=datetime.now(timezone.utc).isoformat(),
                updated_at=datetime.now(timezone.utc).isoformat(),
                is_system=True,
            ),
            ActionSchema(
                id="redirects_301",
                label="301 Redirects",
                description="Create permanent redirects between URLs",
                fields=[
                    FieldDefinition(key="source_url", label="Source (from) URL", required=True, type="url"),
                    FieldDefinition(key="target_url", label="Destination (to) URL", required=True, type="url"),
                    FieldDefinition(key="notes", label="Notes", type="text"),
                ],
                created_at=datetime.now(timezone.utc).isoformat(),
                updated_at=datetime.now(timezone.utc).isoformat(),
                is_system=True,
            ),
        ]
        for schema in builtin:
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
            for kw in schema_keywords:
                if kw in sheet_lower:
                    score += 0.3
                    matches.append(f"Sheet name contains '{kw}'")
            
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
                matched_required = sum(1 for key in required_fields if key.lower() in cols_lower)
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
            matched_any = len(all_keys & cols_lower)
            if len(all_keys) > 0:
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
