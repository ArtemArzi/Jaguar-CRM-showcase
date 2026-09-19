from __future__ import annotations

from collections.abc import Callable
from functools import wraps

from ninja.errors import HttpError

from apps.clubs.models import ClubMembership

Role = ClubMembership.Role

# Role constants (aliases for backward compatibility)
OWNER = Role.OWNER
ADMIN = Role.ADMIN
TRAINER = Role.TRAINER
STUDENT = Role.STUDENT
PARENT = Role.PARENT

# Role groups for common access patterns
STAFF_ROLES = {OWNER, ADMIN, TRAINER}
MANAGEMENT_ROLES = {OWNER, ADMIN}
ALL_ROLES = {OWNER, ADMIN, TRAINER, STUDENT, PARENT}


def role_required(*allowed_roles: str, conceal_denial: bool = False) -> Callable:
    """Decorator for Ninja endpoints to enforce role-based access.

    Usage:
        @router.get("/")
        @role_required("owner", "admin")
        def my_endpoint(request):
            ...

    Returns 403 JSON if user's role not in allowed_roles.  Privacy-sensitive
    exact-resource routes may opt into a generic 404 for an authenticated role
    mismatch with ``conceal_denial=True``.
    Assumes request._membership is set by TenantJWTAuth.
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(request, *args, **kwargs):
            membership = getattr(request, "_membership", None)
            if not membership:
                raise HttpError(401, "Authentication required")
            if membership.role not in allowed_roles:
                if conceal_denial:
                    raise HttpError(404, "Not found")
                raise HttpError(403, "Access denied: insufficient role")
            return func(request, *args, **kwargs)

        return wrapper

    return decorator


def view_role_required(*allowed_roles: str) -> Callable:
    """Decorator for Django views (HTMX admin) to enforce role-based access.

    Usage:
        @view_role_required("owner", "admin")
        def my_view(request):
            ...

    Redirects to login if no membership, returns 403 if role not allowed.
    Assumes request._membership is set by TenantMiddleware.
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(request, *args, **kwargs):
            from django.http import HttpResponseForbidden
            from django.shortcuts import redirect

            membership = getattr(request, "_membership", None)
            if not membership:
                return redirect("admin-login")
            if membership.role not in allowed_roles:
                return HttpResponseForbidden("Access denied: insufficient role")
            return func(request, *args, **kwargs)

        return wrapper

    return decorator


def management_view_required(func: Callable) -> Callable:
    """Shortcut: restrict Django view to owner/admin roles only."""
    return view_role_required(OWNER, ADMIN)(func)
