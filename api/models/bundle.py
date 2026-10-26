"""Context bundle models — the shared inputs every generator reads (#427).

A job's **bundle** is assembled once, one **slice** per row: the job's facts,
its parsed captions, its SST record, the house rules, and the inferred parts a
model builds from those (the outline). Generators declare which slices they
read and see nothing else.

A slice is labelled like an item. ``origin`` says whether a model touched it;
``content`` is what the builder produced and ``edited_content`` is the editor's
correction. Readers get the edit when there is one, and a rebuild never
overwrites it — a correction made once reaches every item that reads the slice.
"""

from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from api.models.item import GenerationState, Origin


class BundlePart(BaseModel):
    """One slice of a job's bundle."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: int
    slice: str
    origin: Origin
    state: GenerationState = GenerationState.ready
    reason: Optional[str] = Field(None, description="Why not ready, e.g. 'no_sst_record', 'model_error'")
    content: Any = Field(None, description="What the builder produced")
    edited_content: Any = Field(None, description="The editor's correction, if any")
    revision: int = Field(1, description="Bumped whenever the effective content changes")

    producer: Optional[str] = None
    model: Optional[str] = Field(None, description="The resolved model id, never an alias")
    backend: Optional[str] = Field(
        None, description="The configured backend that served the call, e.g. 'openrouter' or 'local-llm'"
    )
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    built_at: Optional[datetime] = None

    created_at: datetime
    updated_at: datetime

    @property
    def effective(self) -> Any:
        """What a generator reads: the correction when there is one."""
        return self.edited_content if self.edited_content is not None else self.content

    def is_ready(self) -> bool:
        return self.state == GenerationState.ready


class BundlePartUpdate(BaseModel):
    """PATCH /jobs/{job_id}/bundle/{slice} — the editor's correction only.

    Send ``edited_content: null`` to clear a correction.
    """

    model_config = ConfigDict(extra="forbid")

    edited_content: Any = None


class Bundle(BaseModel):
    """Response for GET /jobs/{job_id}/bundle."""

    job_id: int
    parts: List[BundlePart]
    total: int
