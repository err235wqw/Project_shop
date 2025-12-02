import asyncio
import os
from typing import List

import httpx
from fastapi import FastAPI, Depends, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session, init_models, OrderORM
from .schemas import CreateOrderRequest, Order
from .outbox_publisher import publish_outbox_messages
from .db import OutboxMessageORM


def _get_required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} must be set")
    return value


# URL сервисов для оркестрации (опционально, если используются)
PAYMENT_SERVICE_URL = os.getenv("PAYMENT_SERVICE_URL", "http://payment_service:8000")
NOTIFICATION_SERVICE_URL = os.getenv("NOTIFICATION_SERVICE_URL", "http://notification_service:8000")

http_client = httpx.AsyncClient(timeout=10.0)


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
    ХОРЕОГРАФИЯ: Создание заказа с публикацией события
    
    Этот эндпоинт:
    1. Создает заказ в БД
    2. Записывает событие в outbox (в одной транзакции)
    3. Воркер outbox публикует событие order.created в RabbitMQ
    4. Payment и Notification сервисы подписываются на события и обрабатывают их асинхронно
    
    Это пример ХОРЕОГРАФИИ - каждый сервис реагирует на события независимо.
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


@app.post("/orders/orchestrated", response_model=Order)
async def create_order_orchestrated(req: CreateOrderRequest, session: AsyncSession = Depends(get_session)):
    """
    ОРКЕСТРАЦИЯ: Создание заказа с синхронной координацией
    
    Этот эндпоинт демонстрирует ОРКЕСТРАЦИЮ:
    1. Создает заказ в БД
    2. Синхронно вызывает Payment Service (HTTP)
    3. Если платеж успешен - синхронно вызывает Notification Service
    4. Обновляет статус заказа
    
    Order Service здесь выступает ОРКЕСТРАТОРОМ - он знает всю последовательность
    и координирует выполнение всех шагов.
    """
    total_amount = sum(i.price * i.quantity for i in req.items)

    # Шаг 1: Создать заказ
    async with session.begin():
        order = OrderORM(
            customer_email=req.customer_email,
            total_amount=total_amount,
            status="pending",
        )
        session.add(order)
        await session.flush()
        order_id = order.id

    # Шаг 2: Обработать платеж (синхронный вызов)
    try:
        payment_response = await http_client.post(
            f"{PAYMENT_SERVICE_URL}/payments/process",
            json={
                "order_id": order_id,
                "amount": float(total_amount),
                "customer_email": req.customer_email,
            },
        )
        payment_response.raise_for_status()
        payment_data = payment_response.json()
        
        if not payment_data.get("success", False):
            # Откат: отменить заказ
            async with session.begin():
                await session.execute(
                    update(OrderORM)
                    .where(OrderORM.id == order_id)
                    .values(status="cancelled")
                )
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail="Payment failed",
            )
    except httpx.HTTPError:
        # Если payment_service недоступен, просто логируем
        print(f"[Order Service] Payment service недоступен, пропускаем платеж")
        payment_data = {"success": True, "payment_id": "skipped"}

    # Шаг 3: Отправить уведомление (синхронный вызов)
    try:
        notification_response = await http_client.post(
            f"{NOTIFICATION_SERVICE_URL}/notifications/send",
            json={
                "order_id": order_id,
                "customer_email": req.customer_email,
                "amount": float(total_amount),
            },
        )
        notification_response.raise_for_status()
    except httpx.HTTPError:
        # Если notification_service недоступен, просто логируем
        print(f"[Order Service] Notification service недоступен, пропускаем уведомление")

    # Шаг 4: Обновить статус заказа
    async with session.begin():
        await session.execute(
            update(OrderORM)
            .where(OrderORM.id == order_id)
            .values(status="confirmed")
        )

    await session.refresh(order)
    
    return Order(
        id=order.id,
        customer_email=order.customer_email,
        total_amount=order.total_amount,
        status=order.status,
        created_at=order.created_at,
    )


