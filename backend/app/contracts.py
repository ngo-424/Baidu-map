"""Versioned geographic wire contract; separate from the legacy canvas demo."""
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from .rules import DistanceRule

Category = Literal["market", "pharmacy", "school"]
Status = Literal["complete", "partial", "failed", "empty"]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Origin(WireModel):
    lng: float = Field(ge=-180, le=180)
    lat: float = Field(gt=-85, lt=85)


class Geometry(WireModel):
    type: Literal["Polygon", "MultiPolygon"]
    coordinates: list
    coordinate_system: Literal["bd09ll"] = Field(default="bd09ll", validation_alias=AliasChoices("coordinate_system", "coordinateSystem"))


class Issue(WireModel):
    code: str
    message: str
    scope: str
    severity: Literal["warning", "error", "pending"] = "warning"


class Facility(WireModel):
    id: str
    name: str
    category: Category
    location: Origin
    in_circle: bool | None


class CategoryResult(WireModel):
    category: Category
    query_status: Literal["complete", "unknown", "failed"]
    count_in_circle: int | None = Field(ge=0)
    service_status: Literal["covered", "blind", "unknown"]


class BlindPoint(WireModel):
    location: Origin
    category: Category
    status: Literal["covered", "blind", "unknown"]
    evidence: str


class Data(WireModel):
    geometry: Geometry | None = None
    uncertain_region: Geometry | None = None
    unknown_region: Geometry | None = None
    computation_extent: Geometry | None = None
    facilities: list[Facility] | None = None
    categories: list[CategoryResult] = Field(default_factory=lambda: [
        CategoryResult(category=c, query_status="unknown", count_in_circle=None, service_status="unknown")
        for c in ("market", "pharmacy", "school")
    ])
    blind_points: list[BlindPoint] | None = None
    blind_region: Geometry | None = None
    report: str | None = None


class Rules(WireModel):
    version: str = "n04-v1"
    time_threshold_s: Literal[900] = 900
    time_inclusive: Literal[True] = True
    time_tolerance_s: float = 0
    time_confirmation: Literal["implementation_only", "mock_only"] = "implementation_only"
    distance: DistanceRule = Field(default_factory=DistanceRule)


class AnalysisResponse(WireModel):
    schema_version: Literal["1.0"] = "1.0"
    analysis_id: str = Field(default_factory=lambda: str(uuid4()))
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: Status
    source: Literal["mock", "synthetic", "system"]
    algorithm_version: str | None = None
    data_updated_at: datetime | None = None
    origin: Origin | None = None
    coordinate_system: Literal["bd09ll"] = "bd09ll"
    coordinate_order: Literal["longitude,latitude"] = "longitude,latitude"
    units: dict[str, str] = Field(default_factory=lambda: {"distance": "m", "duration": "s", "area": "m2"})
    rules: Rules = Field(default_factory=Rules)
    data: Data = Field(default_factory=Data)
    algorithm: dict | None = None
    warnings: list[Issue] = Field(default_factory=list)
    errors: list[Issue] = Field(default_factory=list)


class SyntheticRequest(WireModel):
    origin: Origin
    coordinate_system: Literal["bd09ll"]
    scenario: Literal["plane", "local_failure", "global_failure"] = "plane"
    budget: int = Field(default=400, ge=144, le=800, strict=True)
    distance_rule: DistanceRule = Field(default_factory=DistanceRule)
