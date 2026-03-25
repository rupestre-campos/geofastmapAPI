"""Tests for project documentation search index."""

from app.services.project_docs_search import search_project_docs


def test_search_finds_tiles_doc():
    results, total, pages = search_project_docs("vector tiles dynamic", page=1, per_page=8)
    assert total >= 1
    assert pages >= 1
    routes = {r["route"] for r in results}
    assert "project-docs/tiles" in routes


def test_search_pagination_second_page():
    results1, total, pages = search_project_docs("tiles", page=1, per_page=1)
    assert total >= 2, "expected multiple docs to mention tiles"
    results2, total2, _ = search_project_docs("tiles", page=2, per_page=1)
    assert total2 == total
    assert results1[0]["route"] != results2[0]["route"]


def test_search_empty_query_returns_nothing():
    results, total, pages = search_project_docs("   ", page=1)
    assert results == []
    assert total == 0
    assert pages == 0
