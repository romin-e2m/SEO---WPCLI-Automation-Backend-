"""Single source of truth for built-in workbook action types (field metadata).

Used by ``mapping.ACTION_FIELDS`` and ``SchemaManager`` built-in schemas so they
cannot drift apart.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.schemas.schema import ActionSchema, AnyOfGroup, FieldDefinition
from app.schemas.workbook import ActionType

_BUILTIN_ROWS: list[dict[str, Any]] = [
    {
        "id": "on_page",
        "label": "On-Page (H1)",
        "description": "Manage page H1 tags",
        "fields": [
            {"key": "page_url", "label": "Page URL", "required": True, "type": "url"},
            {"key": "current_h1", "label": "Current H1", "required": False, "type": "text"},
            {"key": "recommended_h1", "label": "Recommended H1", "required": False, "type": "text"},
            {"key": "notes", "label": "Notes", "required": False, "type": "text"},
        ],
        "any_of_groups": [["recommended_h1"]],
    },
    {
        "id": "meta",
        "label": "Meta description",
        "description": "Update meta descriptions for pages",
        "fields": [
            {"key": "page_url", "label": "Page URL", "required": True, "type": "url"},
            {
                "key": "current_meta_description",
                "label": "Current meta description",
                "required": False,
                "type": "text",
            },
            {
                "key": "recommended_meta_description",
                "label": "Recommended meta description",
                "required": True,
                "type": "text",
            },
            {"key": "notes", "label": "Notes", "required": False, "type": "text"},
        ],
        "any_of_groups": [],
    },
    {
        "id": "meta_title",
        "label": "SEO meta title",
        "description": "Update SEO title (search snippet title) for pages and posts",
        "fields": [
            {"key": "page_url", "label": "Page URL", "required": True, "type": "url"},
            {"key": "current_meta_title", "label": "Current meta title", "required": False, "type": "text"},
            {
                "key": "recommended_meta_title",
                "label": "Recommended meta title",
                "required": True,
                "type": "text",
            },
            {"key": "notes", "label": "Notes", "required": False, "type": "text"},
        ],
        "any_of_groups": [],
    },
    {
        "id": "images",
        "label": "Image alt text",
        "description": "Add or update image alt text",
        "fields": [
            {"key": "page_url", "label": "Page URL", "required": True, "type": "url"},
            {"key": "image_url", "label": "Image URL", "required": True, "type": "url"},
            {"key": "current_alt_text", "label": "Current alt text", "required": False, "type": "text"},
            {
                "key": "recommended_alt_text",
                "label": "Recommended alt text",
                "required": True,
                "type": "text",
            },
            {"key": "notes", "label": "Notes", "required": False, "type": "text"},
        ],
        "any_of_groups": [],
    },
    {
        "id": "url_cleanup",
        "label": "URL cleanup in content",
        "description": "Replace old URLs with new ones in content",
        "fields": [
            {"key": "page_url", "label": "Page URL", "required": True, "type": "url"},
            {"key": "old_url", "label": "URL to replace", "required": True, "type": "url"},
            {"key": "new_url", "label": "New URL", "required": True, "type": "url"},
            {"key": "notes", "label": "Notes", "required": False, "type": "text"},
        ],
        "any_of_groups": [],
    },
    {
        "id": "redirects_301",
        "label": "301 Redirects",
        "description": "Create permanent redirects between URLs",
        "fields": [
            {"key": "source_url", "label": "Source (from) URL", "required": True, "type": "url"},
            {"key": "target_url", "label": "Destination (to) URL", "required": True, "type": "url"},
            {"key": "notes", "label": "Notes", "required": False, "type": "text"},
        ],
        "any_of_groups": [],
    },
]


def build_action_fields_dict() -> dict[ActionType, dict[str, Any]]:
    """Shape expected by ``mapping`` validation and descriptors."""
    out: dict[ActionType, dict[str, Any]] = {}
    for row in _BUILTIN_ROWS:
        aid = row["id"]
        out[aid] = {
            "label": row["label"],
            "fields": list(row["fields"]),
            "any_of_groups": [list(g) for g in row["any_of_groups"]],
        }
    return out


def builtin_action_schemas() -> list[ActionSchema]:
    """Built-in ``ActionSchema`` rows for ``SchemaManager``."""
    now = datetime.now(timezone.utc).isoformat()
    schemas: list[ActionSchema] = []
    for row in _BUILTIN_ROWS:
        fields = [
            FieldDefinition(
                key=f["key"],
                label=f["label"],
                required=bool(f["required"]),
                type=f["type"],
            )
            for f in row["fields"]
        ]
        groups = [AnyOfGroup(fields=list(g)) for g in row["any_of_groups"]]
        schemas.append(
            ActionSchema(
                id=row["id"],
                label=row["label"],
                description=row["description"],
                fields=fields,
                any_of_groups=groups,
                created_at=now,
                updated_at=now,
                is_system=True,
            )
        )
    return schemas
