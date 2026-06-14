from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
from pydantic import BaseModel

from lidarr_ui.config import settings
from lidarr_ui.lidarr import LidarrClient

_client: LidarrClient | None = None
_defaults: dict = {}

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _find_image(images: list[dict], cover_type: str) -> str:
    for img in images:
        if img.get("coverType") == cover_type:
            return img.get("url", "")
    return ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client, _defaults
    _client = LidarrClient()
    try:
        quality = await _client.get_quality_profiles()
        metadata = await _client.get_metadata_profiles()
        roots = await _client.get_root_folders()
        _defaults = {
            "quality_profiles": quality,
            "metadata_profiles": metadata,
            "root_folder": roots[0]["path"] if roots else "/data/media/music",
            "default_quality_id": quality[0]["id"] if quality else 1,
            "default_metadata_id": metadata[0]["id"] if metadata else 1,
        }
    except Exception:
        _defaults = {
            "quality_profiles": [],
            "metadata_profiles": [],
            "root_folder": "/data/media/music",
            "default_quality_id": 1,
            "default_metadata_id": 1,
        }
    yield
    if _client:
        await _client.close()


app = FastAPI(lifespan=lifespan)


def client() -> LidarrClient:
    if _client is None:
        raise HTTPException(503, "Client not ready")
    return _client


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return TEMPLATES.TemplateResponse(request, "index.html", {"defaults": _defaults})


@app.get("/api/image")
async def proxy_image(path: str = Query(...)):
    try:
        r = await client()._client.get(path)
        return Response(
            content=r.content,
            media_type=r.headers.get("content-type", "image/jpeg"),
            headers={"Cache-Control": "max-age=86400"},
        )
    except Exception:
        return Response(status_code=404)


@app.get("/api/artists")
async def list_artists():
    artists = await client().get_artists()
    result = []
    for a in artists:
        stats = a.get("statistics", {})
        result.append({
            "id": a["id"],
            "foreignArtistId": a.get("foreignArtistId", ""),
            "name": a.get("artistName", ""),
            "albumCount": stats.get("albumCount", 0),
            "trackFileCount": stats.get("trackFileCount", 0),
            "sizeOnDisk": stats.get("sizeOnDisk", 0),
            "monitored": a.get("monitored", False),
            "path": a.get("path", ""),
            "poster": _find_image(a.get("images", []), "poster"),
        })
    return sorted(result, key=lambda x: x["sizeOnDisk"], reverse=True)


@app.get("/api/artists/{artist_id}/albums")
async def list_albums(artist_id: int):
    albums = await client().get_albums(artist_id)
    result = []
    for a in albums:
        stats = a.get("statistics", {})
        result.append({
            "id": a["id"],
            "foreignAlbumId": a.get("foreignAlbumId", ""),
            "title": a.get("title", ""),
            "year": a.get("releaseDate", "")[:4] if a.get("releaseDate") else "",
            "albumType": a.get("albumType", ""),
            "monitored": a.get("monitored", False),
            "trackFileCount": stats.get("trackFileCount", 0),
            "sizeOnDisk": stats.get("sizeOnDisk", 0),
            "cover": _find_image(a.get("images", []), "cover"),
        })
    return sorted(result, key=lambda x: x.get("year", ""), reverse=True)


@app.delete("/api/artists/{artist_id}")
async def delete_artist(
    artist_id: int,
    deleteFiles: bool = Query(default=True),
    addImportExclusion: bool = Query(default=True),
):
    await client().delete_artist(artist_id, delete_files=deleteFiles, add_exclusion=addImportExclusion)
    return {"ok": True}


@app.delete("/api/albums/{album_id}/files")
async def delete_album_files(album_id: int):
    await client().delete_album_files(album_id)
    return {"ok": True}


class BulkAlbumFilesPayload(BaseModel):
    albumIds: list[int]


@app.delete("/api/albums/bulk/files")
async def delete_bulk_album_files(body: BulkAlbumFilesPayload):
    for album_id in body.albumIds:
        await client().delete_album_files(album_id)
    return {"deleted": len(body.albumIds)}


class MonitorPayload(BaseModel):
    monitored: bool


@app.put("/api/albums/{album_id}/monitor")
async def set_album_monitored(album_id: int, body: MonitorPayload):
    await client().set_albums_monitored([album_id], body.monitored)
    return {"ok": True}


@app.post("/api/albums/{album_id}/download")
async def download_album(album_id: int):
    c = client()
    await c.set_albums_monitored([album_id], True)
    await c.trigger_album_search([album_id])
    return {"ok": True}


@app.get("/api/search")
async def search_artists(q: str = Query(..., min_length=1)):
    results = await client().search_artists(q)
    return [
        {
            "foreignArtistId": a.get("foreignArtistId", ""),
            "name": a.get("artistName", ""),
            "disambiguation": a.get("disambiguation", ""),
            "overview": (a.get("overview") or "")[:200],
            "poster": _find_image(a.get("images", []), "poster"),
            "inLibrary": a.get("id") is not None,
            "libraryId": a.get("id"),
        }
        for a in results
    ]


class AddArtistPayload(BaseModel):
    foreignArtistId: str
    artistName: str
    qualityProfileId: int | None = None
    metadataProfileId: int | None = None


@app.post("/api/artists")
async def add_artist(body: AddArtistPayload):
    c = client()
    quality_id = body.qualityProfileId or _defaults.get("default_quality_id", 1)
    metadata_id = body.metadataProfileId or _defaults.get("default_metadata_id", 1)
    root = _defaults.get("root_folder", "/data/media/music")

    artist = await c.add_artist(
        foreign_artist_id=body.foreignArtistId,
        artist_name=body.artistName,
        quality_profile_id=quality_id,
        metadata_profile_id=metadata_id,
        root_folder_path=root,
    )
    artist_id = artist["id"]

    albums = await c.wait_for_albums(artist_id)
    album_list = [
        {
            "id": a["id"],
            "foreignAlbumId": a.get("foreignAlbumId", ""),
            "title": a.get("title", ""),
            "year": a.get("releaseDate", "")[:4] if a.get("releaseDate") else "",
            "albumType": a.get("albumType", ""),
            "trackCount": a.get("statistics", {}).get("trackCount", 0),
            "cover": _find_image(a.get("images", []), "cover"),
        }
        for a in albums
    ]
    return {"id": artist_id, "albums": sorted(album_list, key=lambda x: x.get("year", ""), reverse=True)}


class FinalizePayload(BaseModel):
    albumIds: list[int]


@app.post("/api/artists/{artist_id}/finalize")
async def finalize_artist(artist_id: int, body: FinalizePayload):
    if body.albumIds:
        await client().set_albums_monitored(body.albumIds, True)
        await client().trigger_album_search(body.albumIds)
    return {"queued": len(body.albumIds)}


def serve():
    uvicorn.run(app, host="0.0.0.0", port=settings.listen_port)
