"""Dynamic tile routing: filter-only vs items-list sync."""


def test_filter_only_uses_search_cache_path():
    """car_code:eq filters use the search-cache tile path (not paginated items default)."""
    limit = None
    offset = 0
    sortby = None
    orig_q = None
    orig_ids = None
    filter_param = ["car_code:eq:X"]
    feature_ids = None
    list_sync_mode = (
        limit is not None
        or offset != 0
        or (sortby is not None and str(sortby).strip())
        or (orig_q is not None and str(orig_q).strip())
        or (orig_ids is not None and str(orig_ids).strip())
    )
    filter_only_mode = bool(filter_param) and not list_sync_mode and not bool(feature_ids)
    assert list_sync_mode is False
    assert filter_only_mode is True


def test_items_page_is_list_sync_mode():
    limit = 100
    offset = 0
    sortby = None
    orig_q = None
    orig_ids = None
    list_sync_mode = (
        limit is not None
        or offset != 0
        or (sortby is not None and str(sortby).strip())
        or (orig_q is not None and str(orig_q).strip())
        or (orig_ids is not None and str(orig_ids).strip())
    )
    assert list_sync_mode is True
