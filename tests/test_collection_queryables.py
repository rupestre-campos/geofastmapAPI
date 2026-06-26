"""Queryable property keys use sampling on large collections."""

from app.crud.features import QUERYABLES_SAMPLE_FEATURES, get_collection_property_keys


def test_get_collection_property_keys_query_samples_features():
    import inspect

    src = inspect.getsource(get_collection_property_keys)
    assert "LIMIT :sample_limit" in src
    assert "DISTINCT ON (id)" in src
    assert "jsonb_object_keys" in src
    assert QUERYABLES_SAMPLE_FEATURES == 100
