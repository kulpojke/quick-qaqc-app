'''*!*! Reviewer identity dependencies for development and future OIDC auth.'''

from __future__ import annotations

import os
import re
from typing import Annotated

from fastapi import Header, HTTPException, Request, status


REVIEWER_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$')


def authenticated_reviewer(
    request: Request,
    reviewer_header: Annotated[str | None, Header(alias='X-Reviewer-ID')] = None,
) -> str:
    '''*!*! Resolve a reviewer identity without accepting it in request data.'''

    auth_mode = os.environ.get('AUTH_MODE', 'disabled').strip().lower()
    if auth_mode != 'development':
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='OIDC authentication is not configured yet',
        )
    reviewer_id = (reviewer_header or '').strip()
    if not REVIEWER_PATTERN.fullmatch(reviewer_id):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='A valid X-Reviewer-ID header is required',
        )
    return reviewer_id
