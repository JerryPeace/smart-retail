"""Domain exceptions — raised by the service / repository layer, translated uniformly into
HTTP responses by main.py's exception handler.

Why use domain exceptions instead of raising HTTPException inside the service:
  The service shouldn't know about HTTP (it may be called by a background task / CLI / test, which have no HTTP).
  The service raises semantically clear domain exceptions, and the API boundary is responsible for mapping them to status codes (FastAPI convention).
"""
from __future__ import annotations


class NotFoundError(Exception):
    """Resource not found (recommendation / job / evaluation, etc.). → HTTP 404."""
