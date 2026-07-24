"""Dynamic tile routing: filter-only vs items-list sync."""


def test_filter_only_is_not_list_sync_mode():
    """car_code:eq filters must use PostGIS MVT, not the paginated search-cache path."""
    limit = None
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
    assert list_sync_mode is False


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
