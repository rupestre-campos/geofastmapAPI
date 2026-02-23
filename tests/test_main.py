from fastapi.testclient import TestClient

from app.main import create_app


def test_main_create_app_and_routes():
    app = create_app()
    client = TestClient(app)

    # OpenAPI should be available
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    data = resp.json()

    paths = data.get("paths", {})
    assert "/" in paths
    assert "/conformance" in paths
    assert "/collections" in paths
    assert "/collections/{collection_id}" in paths
    assert "/collections/{collection_id}/items" in paths


def test_ogc_landing_page():
    """OGC API - Features: GET / returns landing page with links."""
    app = create_app()
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    data = resp.json()
    assert "title" in data
    assert "links" in data
    rels = [link["rel"] for link in data["links"]]
    assert "self" in rels
    assert "conformance" in rels
    assert "data" in rels
    assert "service-desc" in rels
    assert "service-doc" in rels


def test_ogc_conformance():
    """OGC API - Features: GET /conformance returns conformsTo array including Part 4."""
    app = create_app()
    client = TestClient(app)
    resp = client.get("/conformance")
    assert resp.status_code == 200
    data = resp.json()
    assert "conformsTo" in data
    conforms = data["conformsTo"]
    assert "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/core" in conforms
    assert "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/geojson" in conforms
    assert "http://www.opengis.net/spec/ogcapi-features-4/1.0-draft/conf/create-replace-delete" in conforms
    assert "http://www.opengis.net/spec/ogcapi-features-4/1.0-draft/conf/update" in conforms


def test_openapi_at_api():
    """GET /api returns OpenAPI JSON (same as /openapi.json)."""
    app = create_app()
    client = TestClient(app)
    resp = client.get("/api")
    assert resp.status_code == 200
    data = resp.json()
    assert "openapi" in data
    assert "paths" in data


def test_health():
    """GET /health returns 200 and status ok (used by Docker healthcheck)."""
    app = create_app()
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

