"""Short-lived connection credentials, always for the authenticated user."""
from fastapi import APIRouter, Depends, Response

from ..models import User
from ..realtime import session_credentials
from ..security import current_user

router = APIRouter(prefix="/realtime", tags=["realtime"])


@router.get("/session")
async def session(response: Response, user: User = Depends(current_user)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return session_credentials(user.id)
