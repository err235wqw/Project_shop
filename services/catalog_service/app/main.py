import json
import os
from typing import List

import redis
from fastapi import FastAPI
from pydantic import BaseModel


def _get_required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} must be set")
    return value


REDIS_URL = _get_required_env("REDIS_URL")


class Product(BaseModel):
    id: int
    name: str
    price: float


app = FastAPI(title="Catalog Service", version="1.0.0")
_redis_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(REDIS_URL)
    return _redis_client


CATALOG_CACHE_KEY = "catalog:products"


@app.get("/products", response_model=List[Product])
async def list_products():
    r = get_redis()
    data = r.get(CATALOG_CACHE_KEY)
    if data:
        items = json.loads(data)
        return [Product(**i) for i in items]

    # Для примера: "тяжёлая" операция — в реальности запрос в БД
    products = [
        Product(id=1, name="Phone", price=500.0),
        Product(id=2, name="Laptop", price=1500.0),
    ]
    r.set(CATALOG_CACHE_KEY, json.dumps([p.model_dump() for p in products]), ex=60)
    return products


@app.get("/health")
async def health():
    return {"status": "ok", "service": "catalog_service"}


