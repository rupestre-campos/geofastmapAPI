"""OGC API - Features collections: list, get, create, replace, patch, delete."""

from collections.abc import Sequence

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import collections as collections_crud
from app.db.session import get_db
from app.schemas.collection import (
    CollectionCreate,
    CollectionPatch,
    CollectionRead,
    CollectionReplace,
    CollectionsList,
    ExtentRecomputeResponse,
)
from app.schemas.ogc import Link

router = APIRouter()


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _collection_links(base: str, collection_id: str) -> list[Link]:
    return [
        Link(href=f"{base}/collections/{collection_id}", rel="self", type="application/json"),
        Link(href=f"{base}/collections/{collection_id}/items", rel="items", type="application/geo+json"),
    ]


@router.get(
    "",
    response_model=CollectionsList,
    summary="List collections",
)
async def list_collections(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> CollectionsList:
    base = _base_url(request)
    items_list: Sequence = await collections_crud.list_collections(db)
    collections_out = []
    for item in items_list:
        out = CollectionRead.model_validate(item)
        collections_out.append(
            out.model_copy(update={"links": _collection_links(base, item.id)}),
        )
    return CollectionsList(
        collections=collections_out,
        links=[
            Link(href=f"{base}/collections", rel="self", type="application/json"),
        ],
    )


@router.get(
    "/{collection_id}",
    response_model=CollectionRead,
    summary="Get collection by id",
)
async def get_collection(
    request: Request,
    collection_id: str,
    db: AsyncSession = Depends(get_db),
) -> CollectionRead:
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )
    base = _base_url(request)
    out = CollectionRead.model_validate(collection)
    return out.model_copy(update={"links": _collection_links(base, collection_id)})


@router.put(
    "/{collection_id}",
    response_model=CollectionRead,
    summary="Replace collection metadata",
)
async def replace_collection(
    request: Request,
    collection_id: str,
    payload: CollectionReplace,
    db: AsyncSession = Depends(get_db),
) -> CollectionRead:
    collection = await collections_crud.replace_collection(db, collection_id, payload)
    if not collection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )
    base = _base_url(request)
    out = CollectionRead.model_validate(collection)
    return out.model_copy(update={"links": _collection_links(base, collection_id)})


@router.patch(
    "/{collection_id}",
    response_model=CollectionRead,
    summary="Partially update collection",
)
async def patch_collection(
    request: Request,
    collection_id: str,
    payload: CollectionPatch,
    db: AsyncSession = Depends(get_db),
) -> CollectionRead:
    collection = await collections_crud.patch_collection(db, collection_id, payload)
    if not collection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )
    base = _base_url(request)
    out = CollectionRead.model_validate(collection)
    return out.model_copy(update={"links": _collection_links(base, collection_id)})


@router.post(
    "",
    response_model=CollectionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create collection",
)
async def create_collection(
    payload: CollectionCreate,
    db: AsyncSession = Depends(get_db),
) -> CollectionRead:
    existing = await collections_crud.get_collection(db, payload.id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Collection with this id already exists",
        )
    collection = await collections_crud.create_collection(db, payload)
    return CollectionRead.model_validate(collection)


@router.post(
    "/{collection_id}/extent/recompute",
    response_model=ExtentRecomputeResponse,
    summary="Recompute extent from features",
    description="Compute bounding box from feature geometries, update the collection's stored extent, and return it. Use after bulk import or when extent is stale. Returns extent null if the collection has no features with geometry.",
)
async def recompute_collection_extent(
    collection_id: str,
    db: AsyncSession = Depends(get_db),
) -> ExtentRecomputeResponse:
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )
    extent = await collections_crud.recompute_and_update_collection_extent(db, collection_id)
    return ExtentRecomputeResponse(extent=extent)


@router.delete(
    "/{collection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a collection",
)
async def delete_collection(
    collection_id: str,
    db: AsyncSession = Depends(get_db),
) -> Response:
    deleted = await collections_crud.delete_collection(db, collection_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
