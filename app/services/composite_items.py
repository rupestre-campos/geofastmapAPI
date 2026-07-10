"""Fan-out items queries across composite collection members."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.crud import collections as collections_crud
from app.crud import features as features_crud
from app.models.collection import Collection
from app.models.feature import Feature
from app.services.composite_collections import member_collection_ids, parse_composite_members
from app.services.shadow_import import active_shadow_exclude_job_ids
from app.utils.geo import geometry_to_geojson
from app.utils.property_filters import PropertyFilter

COMPOSITE_ITEM_SEP = ":"


def format_composite_item_id(member_collection_id: str, feature_id: str) -> str:
    return f"{member_collection_id}{COMPOSITE_ITEM_SEP}{feature_id}"


def parse_composite_item_id(item_id: str) -> tuple[str, str] | None:
    if COMPOSITE_ITEM_SEP not in item_id:
        return None
    member_id, _, feature_id = item_id.partition(COMPOSITE_ITEM_SEP)
    if not member_id or not feature_id:
        return None
    return member_id, feature_id


async def composite_member_ids(db: AsyncSession, collection: Collection) -> list[str]:
    members = parse_composite_members(getattr(collection, "composite_members", None))
    return member_collection_ids(members)


async def composite_feature_count(db: AsyncSession, member_ids: list[str]) -> int:
    total = 0
    for mid in member_ids:
        coll = await collections_crud.get_collection(db, mid)
        if coll:
            total += int(coll.feature_count or 0)
    return total


def _list_query_kwargs(
    *,
    limit: int,
    offset: int,
    bbox: tuple[float, float, float, float] | None,
    datetime_start: datetime | None,
    datetime_end: datetime | None,
    sortby: str | None,
    sortdesc: bool,
    property_filters: dict[str, str] | None,
    structured_filters: Sequence[PropertyFilter] | None,
    fulltext_q: str | None,
    feature_ids: Sequence[str] | None,
    include_geometry: bool,
    skip_count: bool,
    exclude_bulk_job_ids: Sequence[str] | None,
) -> dict[str, Any]:
    return {
        "limit": limit,
        "offset": offset,
        "bbox": bbox,
        "datetime_start": datetime_start,
        "datetime_end": datetime_end,
        "sortby": sortby,
        "sortdesc": sortdesc,
        "property_filters": property_filters,
        "structured_filters": structured_filters,
        "fulltext_q": fulltext_q,
        "feature_ids": feature_ids,
        "include_geometry": include_geometry,
        "skip_count": skip_count,
        "exclude_bulk_job_ids": exclude_bulk_job_ids,
    }


async def _member_count(
    db: AsyncSession,
    member_id: str,
    *,
    query_kwargs: dict[str, Any],
) -> int:
    coll = await collections_crud.get_collection(db, member_id)
    _, total = await features_crud.list_features_paginated(
        db,
        member_id,
        limit=1,
        offset=0,
        collection_feature_count=coll.feature_count if coll else None,
        skip_count=False,
        exclude_bulk_job_ids=active_shadow_exclude_job_ids(member_id) or None,
        **{k: v for k, v in query_kwargs.items() if k not in ("limit", "offset", "skip_count", "include_geometry")},
    )
    return int(total)


async def list_composite_features_paginated(
    db: AsyncSession,
    member_ids: list[str],
    *,
    limit: int = 100,
    offset: int = 0,
    bbox: tuple[float, float, float, float] | None = None,
    datetime_start: datetime | None = None,
    datetime_end: datetime | None = None,
    sortby: str | None = None,
    sortdesc: bool = False,
    property_filters: dict[str, str] | None = None,
    structured_filters: Sequence[PropertyFilter] | None = None,
    fulltext_q: str | None = None,
    feature_ids: Sequence[str] | None = None,
    include_geometry: bool = True,
    skip_count: bool = False,
) -> tuple[list[tuple[str, Feature]], int]:
    """
    List features across composite members. Returns ([(member_id, feature), ...], numberMatched).
    Composite item ids use format member_id:feature_id.
    """
    if not member_ids:
        return [], 0

    parsed_ids: list[tuple[str, str]] | None = None
    if feature_ids:
        parsed_ids = []
        for fid in feature_ids:
            parsed = parse_composite_item_id(fid)
            if parsed:
                parsed_ids.append(parsed)
            else:
                for mid in member_ids:
                    parsed_ids.append((mid, fid))

    base_kwargs = _list_query_kwargs(
        limit=limit,
        offset=offset,
        bbox=bbox,
        datetime_start=datetime_start,
        datetime_end=datetime_end,
        sortby=sortby,
        sortdesc=sortdesc,
        property_filters=property_filters,
        structured_filters=structured_filters,
        fulltext_q=fulltext_q,
        feature_ids=None,
        include_geometry=include_geometry,
        skip_count=skip_count,
        exclude_bulk_job_ids=None,
    )

    if parsed_ids:
        out: list[tuple[str, Feature]] = []
        for mid, fid in parsed_ids:
            if mid not in member_ids:
                continue
            feat = await features_crud.get_feature(db, mid, fid)
            if feat:
                out.append((mid, feat))
        return out[:limit], len(out)

    total = 0
    if not skip_count:
        counts = await asyncio.gather(
            *[_member_count(db, mid, query_kwargs=base_kwargs) for mid in member_ids]
        )
        total = sum(counts)
    else:
        total = await composite_feature_count(db, member_ids)

    sort_key = (sortby or "").strip() or "id"
    if sort_key in ("id", "") and not property_filters and not structured_filters and not fulltext_q and bbox is None:
        remaining_offset = offset
        remaining_limit = limit
        out = []
        for mid in member_ids:
            if remaining_limit <= 0:
                break
            coll = await collections_crud.get_collection(db, mid)
            member_total = (
                await _member_count(db, mid, query_kwargs=base_kwargs)
                if not skip_count
                else int(coll.feature_count or 0) if coll else 0
            )
            if remaining_offset >= member_total:
                remaining_offset -= member_total
                continue
            page, _ = await features_crud.list_features_paginated(
                db,
                mid,
                collection_feature_count=coll.feature_count if coll else None,
                exclude_bulk_job_ids=active_shadow_exclude_job_ids(mid) or None,
                feature_ids=None,
                limit=remaining_limit,
                offset=remaining_offset,
                bbox=bbox,
                datetime_start=datetime_start,
                datetime_end=datetime_end,
                sortby=sortby,
                sortdesc=sortdesc,
                property_filters=property_filters,
                structured_filters=structured_filters,
                fulltext_q=fulltext_q,
                include_geometry=include_geometry,
                skip_count=True,
            )
            out.extend((mid, f) for f in page)
            remaining_limit -= len(page)
            remaining_offset = 0
        return out, total

    settings = get_settings()
    merge_cap = max(limit + offset, int(getattr(settings, "composite_items_merge_cap", 5000)))
    fetch_limit = min(merge_cap, settings.items_max_limit)

    async def _fetch_member(mid: str) -> tuple[str, list[Feature]]:
        coll = await collections_crud.get_collection(db, mid)
        page, _ = await features_crud.list_features_paginated(
            db,
            mid,
            limit=fetch_limit,
            offset=0,
            bbox=bbox,
            datetime_start=datetime_start,
            datetime_end=datetime_end,
            sortby=sortby,
            sortdesc=sortdesc,
            property_filters=property_filters,
            structured_filters=structured_filters,
            fulltext_q=fulltext_q,
            collection_feature_count=coll.feature_count if coll else None,
            include_geometry=include_geometry,
            skip_count=True,
            exclude_bulk_job_ids=active_shadow_exclude_job_ids(mid) or None,
        )
        return mid, list(page)

    member_pages = await asyncio.gather(*[_fetch_member(mid) for mid in member_ids])
    merged: list[tuple[str, Feature]] = []
    for mid, page in member_pages:
        merged.extend((mid, f) for f in page)

    def _sort_key(row: tuple[str, Feature]) -> Any:
        _mid, feat = row
        if sort_key == "created_at":
            ts = feat.created_at
            return ts if ts is not None else datetime.min.replace(tzinfo=datetime_start.tzinfo if datetime_start and datetime_start.tzinfo else None)
        if sort_key != "id":
            props = feat.properties or {}
            return props.get(sort_key)
        return format_composite_item_id(_mid, feat.id)

    merged.sort(key=_sort_key, reverse=sortdesc)
    page = merged[offset : offset + limit]
    if skip_count and not total:
        total = len(merged)
    return page, total


async def get_composite_feature(
    db: AsyncSession,
    member_ids: list[str],
    item_id: str,
) -> tuple[str, Feature] | None:
    parsed = parse_composite_item_id(item_id)
    if parsed:
        mid, fid = parsed
        if mid not in member_ids:
            return None
        feat = await features_crud.get_feature(db, mid, fid)
        return (mid, feat) if feat else None
    for mid in member_ids:
        feat = await features_crud.get_feature(db, mid, item_id)
        if feat:
            return mid, feat
    return None


async def get_composite_property_keys(
    db: AsyncSession,
    member_ids: list[str],
    configured_fields: list[str] | None = None,
) -> list[str]:
    keys: set[str] = set(configured_fields or [])
    for mid in member_ids:
        keys.update(await features_crud.get_collection_property_keys(db, mid))
    return sorted(keys)


def composite_feature_to_geojson(
    member_id: str,
    feature: Feature,
    *,
    composite_collection_id: str,
    properties_include: set[str] | None = None,
) -> dict[str, Any]:
    comp_id = format_composite_item_id(member_id, feature.id)
    geom = getattr(feature, "geometry_geojson", None) or geometry_to_geojson(feature.geometry)
    props = dict(feature.properties) if feature.properties else {}
    props.setdefault("_member_collection_id", member_id)
    props.setdefault("_member_feature_id", feature.id)
    if properties_include is not None:
        props = {k: v for k, v in props.items() if k in properties_include or k.startswith("_member")}
    feat: dict[str, Any] = {
        "type": "Feature",
        "id": comp_id,
        "geometry": geom,
        "properties": props,
    }
    bbox = getattr(feature, "bbox", None)
    if bbox is not None:
        feat["bbox"] = bbox
    return feat


async def stream_composite_features_geojsonl(
    db: AsyncSession,
    member_ids: list[str],
    *,
    bbox: tuple[float, float, float, float] | None = None,
    datetime_start: datetime | None = None,
    datetime_end: datetime | None = None,
    property_filters: dict[str, str] | None = None,
    structured_filters: Sequence[PropertyFilter] | None = None,
    fulltext_q: str | None = None,
    batch_size: int = 2000,
) -> AsyncIterator[bytes]:
    for mid in member_ids:
        gen = features_crud.stream_features_geojsonl(
            db,
            mid,
            bbox=bbox,
            datetime_start=datetime_start,
            datetime_end=datetime_end,
            property_filters=property_filters,
            structured_filters=structured_filters,
            fulltext_q=fulltext_q,
            batch_size=batch_size,
        )
        async for row in gen:
            try:
                rec = json.loads(row.decode("utf-8").strip())
            except Exception:
                yield row
                continue
            if not isinstance(rec, dict):
                yield row
                continue
            fid = rec.get("id") or (rec.get("properties") or {}).get("id")
            if fid is None:
                yield row
                continue
            rec["id"] = format_composite_item_id(mid, str(fid))
            props = dict(rec.get("properties") or {})
            props["_member_collection_id"] = mid
            props["_member_feature_id"] = str(fid)
            rec["properties"] = props
            yield (json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
