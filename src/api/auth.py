'''*!*! Request identity for local development and Cloudflare Access.'''

from __future__ import annotations

from functools import lru_cache
import os
import re
from typing import Annotated, Mapping

from fastapi import Header, HTTPException, Request


REVIEWER_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.+@-]{0,254}$')


class AuthenticationError(RuntimeError):
    '''*!*! Report a rejected request with its intended HTTP status.'''

    def __init__(self, message: str, status: int):
        '''*!*! Retain a client-safe authentication failure and status.'''

        super().__init__(message)
        self.status = status


def _header(headers: Mapping[str, str], name: str) -> str:
    '''*!*! Read one request header from case-insensitive HTTP mappings.'''

    value = headers.get(name, '')
    if value:
        return value.strip()
    lower_name = name.lower()
    return next(
        (
            str(header_value).strip()
            for header_name, header_value in headers.items()
            if str(header_name).lower() == lower_name
        ),
        '',
    )


@lru_cache(maxsize=4)
def _cloudflare_jwk_client(team_domain: str):
    '''*!*! Cache Cloudflare signing-key clients across authenticated requests.'''

    import jwt

    return jwt.PyJWKClient(f'{team_domain}/cdn-cgi/access/certs')


def decode_cloudflare_access_token(
    token: str,
    team_domain: str,
    audience: str,
) -> dict[str, object]:
    '''*!*! Validate one Cloudflare Access JWT and return its claims.'''

    import jwt

    try:
        signing_key = _cloudflare_jwk_client(team_domain).get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=['RS256'],
            audience=audience,
            issuer=team_domain,
        )
    except Exception as error:
        raise AuthenticationError('Invalid Cloudflare Access token', 401) from error


def resolve_reviewer_identity(
    headers: Mapping[str, str],
    *,
    default_development_reviewer: str = '',
) -> str:
    '''*!*! Resolve a trusted reviewer identity for one HTTP request.'''

    auth_mode = os.environ.get('AUTH_MODE', 'disabled').strip().lower()
    if auth_mode == 'development':
        reviewer_id = (
            _header(headers, 'X-Reviewer-ID')
            or os.environ.get('DEV_REVIEWER_ID', '').strip()
            or default_development_reviewer.strip()
        )
    elif auth_mode == 'cloudflare':
        team_domain = os.environ.get('CF_ACCESS_TEAM_DOMAIN', '').strip().rstrip('/')
        audience = os.environ.get('CF_ACCESS_AUD', '').strip()
        if not team_domain or not audience:
            raise AuthenticationError(
                'Cloudflare Access authentication is not configured',
                503,
            )
        token = _header(headers, 'Cf-Access-Jwt-Assertion')
        if not token:
            raise AuthenticationError('Cloudflare Access token is required', 401)
        claims = decode_cloudflare_access_token(token, team_domain, audience)
        reviewer_id = str(claims.get('email', '')).strip()
    else:
        raise AuthenticationError('Authentication is not configured', 503)

    if not REVIEWER_PATTERN.fullmatch(reviewer_id):
        raise AuthenticationError('A valid reviewer identity is required', 401)
    return reviewer_id


def authenticated_reviewer(
    request: Request,
    reviewer_header: Annotated[str | None, Header(alias='X-Reviewer-ID')] = None,
) -> str:
    '''*!*! Adapt shared request identity handling to a FastAPI dependency.'''

    headers = dict(request.headers) if request is not None else {}
    if reviewer_header:
        headers['X-Reviewer-ID'] = reviewer_header
    try:
        return resolve_reviewer_identity(headers)
    except AuthenticationError as error:
        raise HTTPException(status_code=error.status, detail=str(error)) from error
