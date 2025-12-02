import os
from datetime import datetime, timedelta

from fastapi import Depends, FastAPI, HTTPException, status
from jose import jwt
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .database import UserORM, get_session, init_models


def _get_required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} must be set")
    return value


JWT_SECRET = _get_required_env("JWT_SECRET")
JWT_ALGORITHM = _get_required_env("JWT_ALGORITHM")
JWT_EXPIRE_MINUTES = int(_get_required_env("JWT_EXPIRE_MINUTES"))

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


app = FastAPI(title="Auth Service", version="1.1.0")


@app.on_event("startup")
async def on_startup():
    await init_models()


def create_access_token(sub: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=JWT_EXPIRE_MINUTES)
    payload = {"sub": sub, "exp": expire}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


async def _get_user_by_email(session: AsyncSession, email: str) -> UserORM | None:
    res = await session.execute(select(UserORM).where(UserORM.email == email.lower()))
    return res.scalar_one_or_none()


@app.post("/auth/register", response_model=Token, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    session: AsyncSession = Depends(get_session),
):
    existing = await _get_user_by_email(session, payload.email)
    if existing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="User already exists")

    password_hash = pwd_context.hash(payload.password)
    user = UserORM(email=payload.email.lower(), password_hash=password_hash)
    session.add(user)
    await session.commit()
    await session.refresh(user)

    token = create_access_token(user.email)
    return Token(access_token=token)


@app.post("/auth/token", response_model=Token)
async def login(payload: LoginRequest, session: AsyncSession = Depends(get_session)):
    user = await _get_user_by_email(session, payload.email)
    if not user or not pwd_context.verify(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    token = create_access_token(user.email)
    return Token(access_token=token)


class TokenPayload(BaseModel):
    sub: str
    exp: int


def decode_token(token: str) -> TokenPayload:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return TokenPayload(**payload)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )


class CurrentUser(BaseModel):
    email: EmailStr


async def get_current_user(authorization: str | None = Depends(lambda: None)) -> CurrentUser:
    if not authorization:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid auth scheme")
    payload = decode_token(token)
    return CurrentUser(email=payload.sub)


@app.get("/auth/verify", response_model=CurrentUser)
async def verify_token(user: CurrentUser = Depends(get_current_user)):
    return user


@app.get("/health")
async def health():
    return {"status": "ok", "service": "auth_service"}


