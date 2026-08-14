"""Versioned, safe HTTP problem responses."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from ratereplay_persistence.object_store import ObjectStoreError
from sqlalchemy.exc import SQLAlchemyError


class ApiProblem(Exception):
    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        field_paths: tuple[str, ...] = (),
        witness: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.field_paths = field_paths
        self.witness = witness or {}
        self.headers = headers or {}


class ProblemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "problem-v1"
    code: str
    message: str
    request_id: str
    field_paths: tuple[str, ...] = ()
    witness: dict[str, object] = Field(default_factory=dict)


def install_problem_handler(app: FastAPI) -> None:
    @app.exception_handler(ApiProblem)
    async def handle_api_problem(request: Request, error: ApiProblem) -> JSONResponse:
        request.state.error_code = error.code
        request_id = getattr(request.state, "request_id", "unavailable")
        problem = ProblemResponse(
            code=error.code,
            message=error.message,
            request_id=request_id,
            field_paths=error.field_paths,
            witness=error.witness,
        )
        return JSONResponse(
            status_code=error.status_code,
            content=problem.model_dump(mode="json"),
            headers={"Cache-Control": "no-store", **error.headers},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        request.state.error_code = "REQUEST_VALIDATION_FAILED"
        field_paths = tuple(
            ".".join(str(part) for part in item["loc"] if part != "body") for item in error.errors()
        )
        problem = ProblemResponse(
            code="REQUEST_VALIDATION_FAILED",
            message="The request does not match the required schema.",
            request_id=getattr(request.state, "request_id", "unavailable"),
            field_paths=field_paths,
        )
        return JSONResponse(
            status_code=422,
            content=problem.model_dump(mode="json"),
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(ObjectStoreError)
    @app.exception_handler(SQLAlchemyError)
    async def handle_dependency_error(request: Request, error: Exception) -> JSONResponse:
        del error
        return _safe_failure(
            request,
            status_code=503,
            code="DEPENDENCY_UNAVAILABLE",
            message="A required service is unavailable.",
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, error: Exception) -> JSONResponse:
        del error
        return _safe_failure(
            request,
            status_code=500,
            code="UNEXPECTED_FAILURE",
            message="RateReplay could not complete the request safely.",
        )


def _safe_failure(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
) -> JSONResponse:
    request.state.error_code = code
    problem = ProblemResponse(
        code=code,
        message=message,
        request_id=getattr(request.state, "request_id", "unavailable"),
    )
    return JSONResponse(
        status_code=status_code,
        content=problem.model_dump(mode="json"),
        headers={"Cache-Control": "no-store"},
    )


def problem_openapi_responses(*status_codes: int) -> dict[int | str, dict[str, Any]]:
    return {
        status_code: {
            "model": ProblemResponse,
            "description": "Versioned safe problem response",
        }
        for status_code in status_codes
    }
