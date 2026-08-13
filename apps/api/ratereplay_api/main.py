"""Minimal versioned API shell used by the Milestone 0 latency spike."""

from fastapi import FastAPI

app = FastAPI(title="RateReplay API", version="0.0.0")


@app.get("/healthz", include_in_schema=False)
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/meta")
def metadata() -> dict[str, str]:
    return {
        "calculation_time_mode": "HISTORICAL_REPLAY",
        "evidence_level": "FOUNDATION_ONLY",
        "schema_version": "v1",
    }
