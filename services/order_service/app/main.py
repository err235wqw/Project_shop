import asyncio
from typing import List

from fastapi import FastAPI, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session, init_models, OrderORM
from .schemas import CreateOrderRequest, Order
from .outbox_publisher import publish_outbox_messages
from .db import OutboxMessageORM


app = FastAPI(title="Order Service", version="1.0.0")


@app.on_event("startup")
async def on_startup():
    await init_models()
    # Запускаем воркер outbox в фоне
    asyncio.create_task(publish_outbox_messages())


@app.get("/health")
async def health():
    return {"status": "ok", "service": "order_service"}


@app.get("/orders", response_model=List[Order])
async def list_orders(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(OrderORM))
    rows = result.scalars().all()
    return [
        Order(
            id=row.id,
            customer_email=row.customer_email,
            total_amount=row.total_amount,
            status=row.status,
            created_at=row.created_at,
        )
        for row in rows
    ]


@app.post("/orders", response_model=Order)
async def create_order(req: CreateOrderRequest, session: AsyncSession = Depends(get_session)):
    """
    Пример транзакции:
    - создаём заказ
    - записываем событие в outbox
    Оба действия в одной БД-транзакции.
    """
    total_amount = sum(i.price * i.quantity for i in req.items)

    async with session.begin():
        order = OrderORM(
            customer_email=req.customer_email,
            total_amount=total_amount,
            status="pending",
        )
        session.add(order)
        await session.flush()

        event_payload = {
            "order_id": order.id,
            "customer_email": order.customer_email,
            "total_amount": float(order.total_amount),
        }
        outbox_msg = OutboxMessageORM(event_type="order_created", payload=event_payload)
        session.add(outbox_msg)

    await session.refresh(order)

    return Order(
        id=order.id,
        customer_email=order.customer_email,
        total_amount=order.total_amount,
        status=order.status,
        created_at=order.created_at,
    )


