"""Download Census tract polygons without requiring GIS desktop software."""
from __future__ import annotations
import io, json, tempfile, zipfile
from pathlib import Path
import requests, shapefile

URL = "https://www2.census.gov/geo/tiger/GENZ2024/shp/cb_2024_24_tract_500k.zip"

def fetch_tract_geometry():
    archive = zipfile.ZipFile(io.BytesIO(requests.get(URL, timeout=180).content))
    with tempfile.TemporaryDirectory() as directory:
        archive.extractall(directory)
        shp_path = next(Path(directory).glob("*.shp"))
        reader = shapefile.Reader(str(shp_path))
        try:
            fields = [field[0] for field in reader.fields[1:]]
            result = {}
            for record in reader.iterShapeRecords():
                data = dict(zip(fields, record.record))
                if str(data["COUNTYFP"]) != "031":
                    continue
                geoid = str(data["GEOID"])
                points = record.shape.points
                if not points:
                    continue
                longitude = sum(point[0] for point in points) / len(points)
                latitude = sum(point[1] for point in points) / len(points)
                result[geoid] = {
                    "geometry_geojson": json.dumps(record.shape.__geo_interface__),
                    "land_area_square_meters": int(data["ALAND"]),
                    "centroid_latitude": latitude,
                    "centroid_longitude": longitude,
                }
            return result
        finally:
            reader.close()
