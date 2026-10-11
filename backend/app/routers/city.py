"""Город: выбирается при регистрации, меняется в настройках; «сейчас в другом городе» — на время поездки."""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..content.cities import find_city, search_cities
from ..db import get_session
from ..deps import today_date
from ..models import User, UserCity
from ..security import current_user

router = APIRouter(tags=["city"])


class CityOut(BaseModel):
    name: str
    region: str | None = None


class MyCityOut(BaseModel):
    home_city: str | None
    travel_city: str | None
    travel_until: date | None
    current_city: str | None  # где человек сейчас — для подборок


class MyCityIn(BaseModel):
    home_city: str | None = Field(default=None, max_length=60)
    travel_city: str | None = Field(default=None, max_length=60)  # "" — вернулся домой
    travel_until: date | None = None


def _canon(name: str) -> tuple[str, str | None]:
    """Название из списка (с правильным написанием и поясом) или своё — как ввёл человек."""
    clean = " ".join(name.split())
    if not clean:
        raise HTTPException(422, "Укажи город")
    found = find_city(clean)
    return (found[0], found[2]) if found else (clean, None)


def current_city(row: UserCity | None, today: date) -> str | None:
    if row is None:
        return None
    if row.travel_city and (row.travel_until is None or row.travel_until >= today):
        return row.travel_city
    return row.home_city


def _out(row: UserCity | None, today: date) -> MyCityOut:
    traveling = row is not None and current_city(row, today) == row.travel_city and row.travel_city is not None
    return MyCityOut(
        home_city=row.home_city if row else None,
        travel_city=row.travel_city if traveling else None,
        travel_until=row.travel_until if traveling else None,
        current_city=current_city(row, today),
    )


@router.get("/cities", response_model=list[CityOut])
async def cities(q: str = Query("", max_length=60)):
    return [CityOut(name=n, region=r) for n, r, _ in search_cities(q)]


@router.get("/me/city", response_model=MyCityOut)
async def my_city(user: User = Depends(current_user), session: AsyncSession = Depends(get_session),
                  today: date = Depends(today_date)):
    return _out(await session.get(UserCity, user.id), today)


@router.put("/me/city", response_model=MyCityOut)
async def set_city(body: MyCityIn, user: User = Depends(current_user),
                   session: AsyncSession = Depends(get_session), today: date = Depends(today_date)):
    row = await session.get(UserCity, user.id)
    if body.home_city is not None:
        name, tz = _canon(body.home_city)
        if row is None:
            row = UserCity(user_id=user.id, home_city=name, home_tz=tz)
            session.add(row)
        else:
            row.home_city, row.home_tz = name, tz
    if row is None:
        raise HTTPException(422, "Сначала укажи, где ты живёшь")
    if body.travel_city is not None:
        if body.travel_city.strip() == "":
            row.travel_city = row.travel_tz = row.travel_until = None  # вернулся домой
        else:
            if body.travel_until is not None and body.travel_until < today:
                raise HTTPException(422, "Дата возвращения уже прошла")
            row.travel_city, row.travel_tz = _canon(body.travel_city)
            row.travel_until = body.travel_until
    await session.commit()
    return _out(row, today)
