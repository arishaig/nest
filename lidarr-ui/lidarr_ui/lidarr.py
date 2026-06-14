import asyncio
import httpx
from lidarr_ui.config import settings


class LidarrClient:
    def __init__(self):
        self._client = httpx.AsyncClient(
            base_url=settings.lidarr_url,
            headers={"X-Api-Key": settings.lidarr_key},
            timeout=30.0,
        )

    async def close(self):
        await self._client.aclose()

    async def get_artists(self) -> list[dict]:
        r = await self._client.get("/api/v1/artist")
        r.raise_for_status()
        return r.json()

    async def get_albums(self, artist_id: int) -> list[dict]:
        r = await self._client.get("/api/v1/album", params={"artistId": artist_id})
        r.raise_for_status()
        return r.json()

    async def delete_artist(self, artist_id: int, delete_files: bool = True, add_exclusion: bool = True) -> None:
        r = await self._client.delete(
            f"/api/v1/artist/{artist_id}",
            params={"deleteFiles": str(delete_files).lower(), "addImportExclusion": str(add_exclusion).lower()},
        )
        r.raise_for_status()

    async def delete_album_files(self, album_id: int) -> None:
        r = await self._client.get("/api/v1/trackFile", params={"albumId": album_id})
        r.raise_for_status()
        track_file_ids = [t["id"] for t in r.json()]
        if not track_file_ids:
            return
        r2 = await self._client.request(
            "DELETE", "/api/v1/trackFile/bulk", json={"trackFileIds": track_file_ids}
        )
        r2.raise_for_status()

    async def search_artists(self, term: str) -> list[dict]:
        r = await self._client.get("/api/v1/artist/lookup", params={"term": term})
        r.raise_for_status()
        return r.json()

    async def get_quality_profiles(self) -> list[dict]:
        r = await self._client.get("/api/v1/qualityprofile")
        r.raise_for_status()
        return r.json()

    async def get_metadata_profiles(self) -> list[dict]:
        r = await self._client.get("/api/v1/metadataprofile")
        r.raise_for_status()
        return r.json()

    async def get_root_folders(self) -> list[dict]:
        r = await self._client.get("/api/v1/rootfolder")
        r.raise_for_status()
        return r.json()

    async def add_artist(
        self,
        foreign_artist_id: str,
        artist_name: str,
        quality_profile_id: int,
        metadata_profile_id: int,
        root_folder_path: str,
    ) -> dict:
        payload = {
            "foreignArtistId": foreign_artist_id,
            "artistName": artist_name,
            "qualityProfileId": quality_profile_id,
            "metadataProfileId": metadata_profile_id,
            "rootFolderPath": root_folder_path,
            "monitored": True,
            "addOptions": {"monitor": "none", "searchForMissingAlbums": False},
        }
        r = await self._client.post("/api/v1/artist", json=payload)
        r.raise_for_status()
        return r.json()

    async def wait_for_albums(self, artist_id: int, max_tries: int = 15) -> list[dict]:
        for _ in range(max_tries):
            albums = await self.get_albums(artist_id)
            if albums:
                return albums
            await asyncio.sleep(1)
        return []

    async def set_albums_monitored(self, album_ids: list[int], monitored: bool) -> None:
        r = await self._client.put("/api/v1/album/monitor", json={"albumIds": album_ids, "monitored": monitored})
        r.raise_for_status()

    async def trigger_album_search(self, album_ids: list[int]) -> None:
        r = await self._client.post("/api/v1/command", json={"name": "AlbumSearch", "albumIds": album_ids})
        r.raise_for_status()
