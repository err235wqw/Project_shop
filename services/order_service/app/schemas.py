from datetime import datetime
from typing import List

from pydantic import BaseModel


class OrderItem(BaseModel):
    product_id: int
    quantity: int
    price: float


class CreateOrderRequest(BaseModel):
    customer_email: str
    items: List[OrderItem]


class Order(BaseModel):
    id: int
    customer_email: str
    total_amount: float
    status: str
    created_at: datetime



