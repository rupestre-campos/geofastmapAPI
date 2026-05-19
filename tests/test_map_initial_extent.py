"""Mirror map initial-extent helpers (same rules as static/js/geofastmap-utils.js)."""


def is_valid_map_bbox(bbox):
    if not bbox or len(bbox) < 4:
        return False
    minx, miny, maxx, maxy = (float(bbox[i]) for i in range(4))
    if not all(map(lambda x: x == x, [minx, miny, maxx, maxy])):  # NaN check
        return False
    if minx >= maxx or miny >= maxy:
        return False
    if maxx - minx > 120 or maxy - miny > 80:
        return False
    return True


def is_placeholder_map_camera(center, zoom):
    if not center or len(center) < 2 or zoom is None:
        return True
    lon, lat, z = float(center[0]), float(center[1]), float(zoom)
    if abs(lon) < 0.01 and abs(lat - 20) < 0.01 and z <= 2.5:
        return True
    return False


def is_explicit_saved_camera(center, zoom, bearing, pitch, bbox):
    if not center or len(center) < 2 or zoom is None:
        return False
    if is_placeholder_map_camera(center, zoom):
        return False
    z = float(zoom)
    b = float(bearing) if bearing is not None else 0.0
    p = float(pitch) if pitch is not None else 0.0
    if abs(b) > 0.01 or abs(p) > 0.01:
        return True
    if z >= 5:
        return True
    if is_valid_map_bbox(bbox):
        width = float(bbox[2]) - float(bbox[0])
        height = float(bbox[3]) - float(bbox[1])
        if width <= 30 and height <= 20 and z <= 3:
            return False
    return z > 3


def should_fit_bounds_on_load(bbox, center, zoom, bearing, pitch):
    return is_valid_map_bbox(bbox) and not is_explicit_saved_camera(
        center, zoom, bearing, pitch, bbox
    )


class TestMapInitialExtent:
    def test_valid_regional_bbox(self):
        bbox = [-10.0, 40.0, 10.0, 50.0]
        assert is_valid_map_bbox(bbox)

    def test_world_bbox_rejected(self):
        assert not is_valid_map_bbox([-180, -90, 180, 90])

    def test_placeholder_camera_not_explicit(self):
        assert is_placeholder_map_camera([0, 20], 2)

    def test_fit_when_bbox_and_placeholder_camera(self):
        bbox = [-10.0, 40.0, 10.0, 50.0]
        assert should_fit_bounds_on_load(bbox, [0, 20], 2, 0, 0)

    def test_no_fit_when_explicit_zoom(self):
        bbox = [-10.0, 40.0, 10.0, 50.0]
        assert not should_fit_bounds_on_load(bbox, [-5.0, 45.0], 8, 0, 0)

    def test_fit_when_tight_bbox_and_low_zoom_camera(self):
        """Low zoom + regional bbox still prefers fitBounds over placeholder camera."""
        bbox = [-1.0, 48.0, 1.0, 49.0]
        assert should_fit_bounds_on_load(bbox, [-0.5, 48.5], 3, 0, 0)
