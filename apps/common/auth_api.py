from django.http import HttpResponse, JsonResponse
from ninja import Router, Schema

from apps.common import auth_tokens

router = Router(tags=["auth"])


class RefreshCookieIn(Schema):
    refresh_token: str


@router.post("/refresh-cookie/", response={204: None})
def store_refresh_cookie_endpoint(request, payload: RefreshCookieIn):
    response = HttpResponse(status=204)
    if not payload.refresh_token:
        return JsonResponse({"detail": "Refresh token required"}, status=400)
    token_user_id = auth_tokens.refresh_token_user_id(
        refresh_token=payload.refresh_token
    )
    if token_user_id is None or token_user_id != request.user.id:
        return JsonResponse({"detail": "Invalid refresh token"}, status=401)
    auth_tokens.set_refresh_cookie(response, payload.refresh_token)
    return response


@router.post("/refresh/", auth=None)
def refresh_access_token_endpoint(request):
    refresh_token = request.COOKIES.get(auth_tokens.REFRESH_COOKIE_NAME)
    if not refresh_token:
        response = JsonResponse({"detail": "Refresh session expired"}, status=401)
        auth_tokens.clear_refresh_cookie(response)
        return response

    try:
        access_token, next_refresh_token = auth_tokens.refresh_access_token(
            refresh_token=refresh_token
        )
    except auth_tokens.InvalidRefreshTokenError:
        response = JsonResponse({"detail": "Refresh session expired"}, status=401)
        auth_tokens.clear_refresh_cookie(response)
        return response

    response = JsonResponse({"access_token": access_token})
    auth_tokens.set_refresh_cookie(response, next_refresh_token)
    return response


@router.post("/logout/", auth=None, response={204: None})
def logout_refresh_cookie_endpoint(request):
    refresh_token = request.COOKIES.get(auth_tokens.REFRESH_COOKIE_NAME)
    if refresh_token:
        auth_tokens.revoke_refresh_token(refresh_token=refresh_token)
    response = HttpResponse(status=204)
    auth_tokens.clear_refresh_cookie(response)
    return response
