"""Plain-language errors. Raise these from services; the API turns them into
{"detail": "..."} with the right status code."""
from fastapi import HTTPException


class NotFound(HTTPException):
    def __init__(self, what: str = "Record"):
        super().__init__(status_code=404, detail=f"{what} not found")


class BadRequest(HTTPException):
    def __init__(self, detail: str):
        super().__init__(status_code=400, detail=detail)


class Conflict(HTTPException):
    def __init__(self, detail: str):
        super().__init__(status_code=409, detail=detail)


class Forbidden(HTTPException):
    def __init__(self, detail: str = "You don't have access to this"):
        super().__init__(status_code=403, detail=detail)
