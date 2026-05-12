from typing import Any
from fastapi import APIRouter, HTTPException, Request

from app.schemas.schema import (
    ActionSchema,
    SchemaListResponse,
    CreateActionSchemaRequest,
    UpdateActionSchemaRequest,
    InferSchemaRequest,
    InferSchemaResponse,
)

router = APIRouter(prefix="/api/schemas", tags=["schemas"])


@router.get("", response_model=SchemaListResponse)
def list_schemas(request: Request) -> SchemaListResponse:
    """List all available action schemas (built-in and user-defined)."""
    manager = request.app.schema_manager
    return SchemaListResponse(schemas=manager.list_schemas())


@router.get("/{schema_id}", response_model=ActionSchema)
def get_schema(schema_id: str, request: Request) -> ActionSchema:
    """Get a specific schema by ID."""
    manager = request.app.schema_manager
    schema = manager.get_schema(schema_id)
    if not schema:
        raise HTTPException(status_code=404, detail=f"Schema '{schema_id}' not found.")
    return schema


@router.post("", response_model=ActionSchema)
def create_schema(payload: CreateActionSchemaRequest, request: Request) -> ActionSchema:
    """Create a new user-defined action schema."""
    manager = request.app.schema_manager
    try:
        schema = manager.create_schema(payload)
        return schema
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to create schema.") from e


@router.patch("/{schema_id}", response_model=ActionSchema)
def update_schema(
    schema_id: str,
    payload: UpdateActionSchemaRequest,
    request: Request,
) -> ActionSchema:
    """Update an existing user-defined schema."""
    manager = request.app.schema_manager
    try:
        schema = manager.update_schema(schema_id, payload)
        return schema
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to update schema.") from e


@router.delete("/{schema_id}")
def delete_schema(schema_id: str, request: Request) -> dict[str, str]:
    """Delete a user-defined schema."""
    manager = request.app.schema_manager
    try:
        manager.delete_schema(schema_id)
        return {"message": f"Schema '{schema_id}' deleted."}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to delete schema.") from e


@router.post("/infer", response_model=InferSchemaResponse)
def infer_schema(payload: InferSchemaRequest, request: Request) -> InferSchemaResponse:
    """Auto-infer an action schema based on sheet name and columns."""
    manager = request.app.schema_manager
    schema, confidence, reasoning = manager.infer_schema(payload.sheet_name, payload.columns)
    return InferSchemaResponse(
        suggested_schema=schema,
        confidence=confidence,
        reasoning=reasoning,
    )
