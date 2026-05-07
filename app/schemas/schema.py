from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field

FieldType = Literal["url", "text", "int", "bool"]


class FieldDefinition(BaseModel):
    """Define a single field in a custom action schema."""
    key: str = Field(description="Unique canonical field identifier (e.g., 'page_url')")
    label: str = Field(description="Human-readable label for UI display")
    required: bool = Field(default=False, description="Whether this field must be mapped")
    type: FieldType = Field(default="text", description="Data type for validation")
    description: str = Field(default="", description="Optional help text for users")


class AnyOfGroup(BaseModel):
    """Require at least one field from a group to be present."""
    fields: list[str] = Field(description="List of field keys; at least one must be mapped")


class ActionSchema(BaseModel):
    """Define a custom action type with fields and constraints."""
    id: str = Field(description="Unique identifier (e.g., 'on_page', 'custom_seo_audit')")
    label: str = Field(description="Human-readable action label")
    description: str = Field(default="", description="Optional description of this action type")
    fields: list[FieldDefinition] = Field(description="List of canonical fields")
    any_of_groups: list[AnyOfGroup] = Field(default_factory=list, description="Constraints requiring at least one field per group")
    created_at: str = Field(description="ISO 8601 timestamp when schema was created")
    updated_at: str = Field(description="ISO 8601 timestamp when schema was last updated")
    is_system: bool = Field(default=False, description="True for built-in schemas, False for user-defined")


class SchemaListResponse(BaseModel):
    """Response when listing available schemas."""
    schemas: list[ActionSchema]


class CreateActionSchemaRequest(BaseModel):
    """Request to create a new action schema."""
    id: str = Field(min_length=1, max_length=50, pattern=r"^[a-z0-9_]+$", description="Unique identifier (lowercase, alphanumeric + underscore)")
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(default="")
    fields: list[FieldDefinition]
    any_of_groups: list[AnyOfGroup] = Field(default_factory=list)


class UpdateActionSchemaRequest(BaseModel):
    """Request to update an existing action schema."""
    label: str | None = None
    description: str | None = None
    fields: list[FieldDefinition] | None = None
    any_of_groups: list[AnyOfGroup] | None = None


class InferSchemaRequest(BaseModel):
    """Request backend to automatically infer a schema from sheet columns."""
    sheet_name: str
    columns: list[str]


class InferSchemaResponse(BaseModel):
    """Auto-inferred schema suggestion."""
    suggested_schema: ActionSchema | None = Field(description="Best-effort inferred schema, or null if no good match")
    confidence: float = Field(ge=0.0, le=1.0, description="How confident the inference is (0-1)")
    reasoning: str = Field(description="Explanation of why this schema was suggested or why inference failed")


class SchemaValidationError(BaseModel):
    """Error validating against a schema."""
    schema_id: str
    message: str
