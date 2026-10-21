"""
Airtable API Service - READ-ONLY

Provides read-only access to the PBS Wisconsin SST (Single Source of Truth) table.

CRITICAL: This service is intentionally READ-ONLY. No write operations are permitted.
"""

from typing import Optional

import httpx

from api.services.secrets import get_secret


class AirtableClient:
    """
    READ-ONLY Airtable client for SST lookups.

    This client only provides read operations against the Airtable API.
    No create, update, or delete operations are implemented.
    """

    BASE_ID = "appZ2HGwhiifQToB6"
    TABLE_ID = "tblTKFOwTvK7xw1H5"
    TABLE_NAME = "✔️Single Source of Truth"
    PROJECTS_TABLE_ID = "tblU9LfZeVNicdB5e"
    INTERFACE_PAGE_ID = "pagCh7J2dYzqPC3bH"  # SST interface view
    MEDIA_ID_FIELD = "Media ID"
    MEDIA_ID_FIELD_ID = "fld8k42kJeWMHA963"
    API_BASE_URL = "https://api.airtable.com/v0"
    # Fields requested by batch lookups. Every name must exist on the SST
    # table or Airtable rejects the whole request with 422 (see #382, where
    # a nonexistent "Title" made the batch lookup return empty forever).
    # Pinned against the live schema by tests/test_sst_context_path.py.
    SST_BATCH_FIELDS = ("Media ID", "Release Title", "Project")

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize Airtable client.

        Args:
            api_key: Airtable API key. If not provided, checks Keychain then env var.

        Raises:
            ValueError: If no API key is provided or found.
        """
        self.api_key = api_key or get_secret("AIRTABLE_API_KEY")
        if not self.api_key:
            raise ValueError("Airtable API key required. Add to Keychain or set AIRTABLE_API_KEY env var.")

        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def search_sst_by_media_id(self, media_id: str) -> Optional[dict]:
        """
        Search SST table by Media ID field.

        Args:
            media_id: The Media ID to search for (e.g., "3092977804")

        Returns:
            Record dict if found, None if not found or on error.
            Record format: {"id": "rec...", "fields": {...}, "createdTime": "..."}

        Raises:
            httpx.HTTPError: On network or API errors (except 404/empty results)
        """
        url = f"{self.API_BASE_URL}/{self.BASE_ID}/{self.TABLE_ID}"

        # Use filterByFormula to search by Media ID field
        # Airtable formula: {Media ID} = 'value'
        formula = f"{{{self.MEDIA_ID_FIELD}}} = '{media_id}'"

        params = {
            "filterByFormula": formula,
            "maxRecords": 1,  # We only expect one match
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.get(url, headers=self.headers, params=params)
                response.raise_for_status()

                data = response.json()
                records = data.get("records", [])

                if records:
                    return records[0]
                return None

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return None
                raise
            except httpx.HTTPError:
                raise

    async def batch_search_sst_by_media_ids(self, media_ids: list[str]) -> dict[str, dict]:
        """
        Batch search SST table by multiple Media IDs.

        Makes a single Airtable API call with an OR formula to fetch multiple
        records efficiently. Much faster than N individual lookups.

        Args:
            media_ids: List of Media IDs to search for (max ~100 per batch)

        Returns:
            Dict mapping media_id -> record dict for found records.
            Missing media_ids won't have entries in the result.

        Raises:
            httpx.HTTPError: On network or API errors. A failed request must
                not be reported as an empty result — callers treat {} as
                "no records matched" (see #382).
        """
        if not media_ids:
            return {}

        # Build OR formula: OR({Media ID}='id1', {Media ID}='id2', ...)
        # Airtable has a formula length limit, so we batch in groups of ~50
        results: dict[str, dict] = {}
        batch_size = 50  # Conservative batch size to avoid formula length limits

        url = f"{self.API_BASE_URL}/{self.BASE_ID}/{self.TABLE_ID}"

        async with httpx.AsyncClient(timeout=60.0) as client:
            for i in range(0, len(media_ids), batch_size):
                batch = media_ids[i : i + batch_size]

                # Build OR formula for this batch
                conditions = [f"{{{self.MEDIA_ID_FIELD}}}='{mid}'" for mid in batch]
                formula = f"OR({','.join(conditions)})"

                params = {
                    "filterByFormula": formula,
                    "maxRecords": len(batch),
                    "fields[]": list(self.SST_BATCH_FIELDS),  # Only fetch needed fields
                }

                response = await client.get(url, headers=self.headers, params=params)
                response.raise_for_status()

                data = response.json()
                for record in data.get("records", []):
                    mid = record.get("fields", {}).get(self.MEDIA_ID_FIELD)
                    if mid:
                        results[mid] = record

        return results

    async def get_sst_record(self, record_id: str) -> Optional[dict]:
        """
        Fetch a specific SST record by Airtable record ID.

        Args:
            record_id: Airtable record ID (e.g., "recXXXXXXXXXXXXXX")

        Returns:
            Record dict if found, None if not found.
            Record format: {"id": "rec...", "fields": {...}, "createdTime": "..."}

        Raises:
            httpx.HTTPError: On network or API errors (except 404)
        """
        url = f"{self.API_BASE_URL}/{self.BASE_ID}/{self.TABLE_ID}/{record_id}"

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.get(url, headers=self.headers)
                response.raise_for_status()
                return response.json()

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return None
                raise
            except httpx.HTTPError:
                raise

    async def get_project_record(self, record_id: str) -> Optional[dict]:
        """
        Fetch a specific Project record by Airtable record ID.

        Args:
            record_id: Airtable record ID (e.g., "recXXXXXXXXXXXXXX")

        Returns:
            Record dict if found, None if not found.
        """
        url = f"{self.API_BASE_URL}/{self.BASE_ID}/{self.PROJECTS_TABLE_ID}/{record_id}"

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.get(url, headers=self.headers)
                response.raise_for_status()
                return response.json()

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return None
                raise
            except httpx.HTTPError:
                raise

    def get_sst_url(self, record_id: str) -> str:
        """
        Generate Airtable web interface URL for a record.

        Opens the record within the SST interface view for a better UX.

        Args:
            record_id: Airtable record ID (e.g., "recXXXXXXXXXXXXXX")

        Returns:
            Full Airtable URL to the record in the interface view
        """
        return f"https://airtable.com/{self.BASE_ID}/{self.INTERFACE_PAGE_ID}/{record_id}"


# Factory function for dependency injection
def get_airtable_client() -> AirtableClient:
    """
    Create AirtableClient instance.

    Returns:
        Configured AirtableClient instance

    Raises:
        ValueError: If AIRTABLE_API_KEY env var is not set
    """
    return AirtableClient()
