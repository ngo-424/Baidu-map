import asyncio
import math
from pathlib import Path

from fastapi import APIRouter
from life_circle.engine import compute_isochrone
from life_circle.models import IsochroneRequest
from life_circle.providers import AnalyticProvider

from .contracts import AnalysisResponse, Data, Issue, Rules, Status, SyntheticRequest

router = APIRouter(prefix="/api/v1/analysis", tags=["N04/N05"], responses={
    422: {"model": AnalysisResponse, "description": "Invalid request"},
    500: {"model": AnalysisResponse, "description": "Internal failure"},
})
MOCK_DIR = Path(__file__).resolve().parents[1] / "mocks"


@router.get("/mock/{scenario}", response_model=AnalysisResponse)
def mock_analysis(scenario: Status):
    return AnalysisResponse.model_validate_json((MOCK_DIR / f"{scenario}.json").read_text(encoding="utf-8"))


def run_synthetic(request: SyntheticRequest):
    origin = (request.origin.lng, request.origin.lat)
    config = IsochroneRequest(origin, request.coordinate_system, budget=request.budget, expand=False)

    def observed(x, y):
        if request.scenario == "global_failure" or (
            request.scenario == "local_failure" and 300 < x < 750 and -350 < y < 350
        ):
            return None
        return math.hypot(x, y) / 1.2

    result = asyncio.run(compute_isochrone(config, AnalyticProvider(origin, observed)))
    payload = result.to_dict()
    # Whole-analysis status cannot be complete before facilities/report are implemented.
    return AnalysisResponse(
        status="failed" if result.quality == "insufficient" else "partial",
        source="synthetic", algorithm_version="aca992d", origin=request.origin, rules=Rules(distance=request.distance_rule),
        data=Data(geometry=payload["geometry"], uncertain_region=payload["uncertainRegion"],
                  unknown_region=payload["unknownRegion"], computation_extent=payload["computationExtent"]),
        algorithm=payload,
        warnings=[Issue(code="SYNTHETIC_ONLY", message="合成时间场，不代表真实社区。", scope="all"),
                  Issue(code="RULES_PENDING", message="阶段0/N01业务口径尚待团队确认；参数详见N04。", scope="rules", severity="pending"),
                  Issue(code="MODULES_NOT_RUN", message="设施、盲区和报告未执行。", scope="facilities,blind_points,report")],
        errors=[Issue(code="INSUFFICIENT_EVIDENCE", message="没有足够步行证据生成等时圈。", scope="isochrone", severity="error")]
        if result.quality == "insufficient" else [],
    )


@router.post("/synthetic", response_model=AnalysisResponse)
def synthetic_analysis(request: SyntheticRequest):
    # FastAPI runs sync handlers in its worker pool: geometry work does not block the event loop.
    return run_synthetic(request)
