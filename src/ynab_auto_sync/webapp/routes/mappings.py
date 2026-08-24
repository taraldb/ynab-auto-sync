from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ynab_auto_sync.config import AppConfig
from ynab_auto_sync.sync.state_db import MappingValidationError, StateDB
from ynab_auto_sync.webapp.deps import get_config, get_db

router = APIRouter()


class MappingCreateRequest(BaseModel):
    provider: str
    provider_account_id: str
    ynab_budget_id: str
    ynab_account_id: str
    display_name: str = ""
    import_source_name: str = ""
    enabled: bool = True


class MappingUpdateRequest(BaseModel):
    """Every field optional - PATCH semantics. Only fields the client
    actually set (model_dump(exclude_unset=True)) are passed through to
    StateDB.update_mapping, so omitting a field never clobbers it back to a
    default (that's what lets 'toggle enabled' send just {"enabled": false}).
    """

    provider: str | None = None
    provider_account_id: str | None = None
    ynab_budget_id: str | None = None
    ynab_account_id: str | None = None
    display_name: str | None = None
    import_source_name: str | None = None
    enabled: bool | None = None


def _validate_budget_id(config: AppConfig, ynab_budget_id: str) -> None:
    # This used to be a startup-time guarantee in config.py's
    # _validate_account_references, for the old static account list. Now
    # that mappings are mutable at runtime, the same check has to be
    # re-applied on every write instead of once at load time.
    if ynab_budget_id not in config.ynab.budgets.values():
        available = ", ".join(sorted(config.ynab.budgets.values())) or "(none configured)"
        raise HTTPException(
            status_code=422,
            detail=(
                f"ynab_budget_id {ynab_budget_id!r} is not one of the configured YNAB "
                f"budgets - available: {available}"
            ),
        )


def _with_tracked_count(db: StateDB, mapping: dict[str, Any]) -> dict[str, Any]:
    mapping["tracked_count"] = db.count_tracked_for_account(mapping["provider_account_id"])
    return mapping


@router.get("/api/mappings")
async def list_mappings(db: StateDB = Depends(get_db)) -> list[dict[str, Any]]:
    return [_with_tracked_count(db, m) for m in db.list_mappings()]


@router.post("/api/mappings", status_code=201)
async def create_mapping(
    body: MappingCreateRequest,
    config: AppConfig = Depends(get_config),
    db: StateDB = Depends(get_db),
) -> dict[str, Any]:
    _validate_budget_id(config, body.ynab_budget_id)
    try:
        mapping_id = await db.create_mapping(**body.model_dump())
    except MappingValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    mapping = db.get_mapping(mapping_id)
    assert mapping is not None  # just created it under the same lock
    return _with_tracked_count(db, mapping)


@router.patch("/api/mappings/{mapping_id}")
async def update_mapping(
    mapping_id: int,
    body: MappingUpdateRequest,
    config: AppConfig = Depends(get_config),
    db: StateDB = Depends(get_db),
) -> dict[str, Any]:
    if db.get_mapping(mapping_id) is None:
        raise HTTPException(status_code=404, detail=f"no mapping with id={mapping_id}")

    fields = body.model_dump(exclude_unset=True)
    if "ynab_budget_id" in fields:
        _validate_budget_id(config, fields["ynab_budget_id"])

    try:
        await db.update_mapping(mapping_id, **fields)
    except MappingValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    mapping = db.get_mapping(mapping_id)
    assert mapping is not None
    return _with_tracked_count(db, mapping)


@router.delete("/api/mappings/{mapping_id}", status_code=204)
async def delete_mapping(mapping_id: int, db: StateDB = Depends(get_db)) -> None:
    if db.get_mapping(mapping_id) is None:
        raise HTTPException(status_code=404, detail=f"no mapping with id={mapping_id}")
    await db.delete_mapping(mapping_id)


@router.delete("/api/mappings")
async def clear_all_mappings(db: StateDB = Depends(get_db)) -> dict[str, int]:
    """Bulk "clear all" for the GUI's Mappings tab - a distinct path
    (`/api/mappings` with no id) from the exact regexp match a
    `/api/mappings/{mapping_id}` route would need, and never confused with
    it since FastAPI resolves the literal path first."""
    deleted = await db.delete_all_mappings()
    return {"deleted": deleted}
