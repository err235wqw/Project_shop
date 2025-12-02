"""
Payment Service - Пример подписчика событий (Хореография) с Transaction Inbox

Этот сервис подписывается на событие order.created из RabbitMQ
и обрабатывает платеж асинхронно с использованием паттерна Transaction Inbox
для гарантии идемпотентности обработки.
"""
import asyncio
import hashlib
import json
import os
from datetime import datetime

import aio_pika
from fastapi import FastAPI
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .database import AsyncSessionLocal, InboxMessageORM, PaymentORM, init_models

app = FastAPI(title="Payment Service", version="1.0.0")


def _get_required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} must be set")
    return value


RABBITMQ_URL = _get_required_env("RABBITMQ_URL")
DATABASE_URL = _get_required_env("DATABASE_URL")
EXCHANGE_NAME = "shop_events"
QUEUE_NAME = "payment_queue"


# Имитация обработки платежа
async def process_payment(order_id: int, amount: float) -> dict:
    """
    Имитация обработки платежа.
    В реальном проекте здесь был бы вызов платежного шлюза.
    """
    await asyncio.sleep(0.5)  # Имитация задержки
    
    # Для примера: платеж всегда успешен
    payment_id = f"pay_{order_id}_{int(asyncio.get_event_loop().time())}"
    
    return {
        "payment_id": payment_id,
        "order_id": order_id,
        "amount": amount,
        "status": "completed",
        "success": True,
    }


async def publish_event(routing_key: str, payload: dict):
    """Публикует событие в RabbitMQ"""
    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    channel = await connection.channel()
    exchange = await channel.declare_exchange(
        EXCHANGE_NAME, aio_pika.ExchangeType.TOPIC, durable=True
    )
    
    body = json.dumps(payload).encode("utf-8")
    await exchange.publish(aio_pika.Message(body=body), routing_key=routing_key)
    
    await connection.close()


def _generate_message_id(message_body: bytes, routing_key: str) -> str:
    """
    Генерирует уникальный ID сообщения для идемпотентности.
    В реальном проекте можно использовать message_id из RabbitMQ.
    """
    content = f"{routing_key}:{message_body.decode('utf-8')}"
    return hashlib.sha256(content.encode()).hexdigest()


async def _is_message_processed(message_id: str, session: AsyncSession) -> bool:
    """Проверяет, было ли сообщение уже обработано (Transaction Inbox)"""
    result = await session.execute(
        select(InboxMessageORM).where(InboxMessageORM.message_id == message_id)
    )
    existing = result.scalar_one_or_none()
    return existing is not None and existing.status == "processed"




async def _process_payment_with_inbox(event: dict, message_id: str, session: AsyncSession):
    """
    Обрабатывает платеж с использованием Transaction Inbox.
    Вся логика выполняется в одной транзакции с сохранением в inbox.
    """
    order_id = event["order_id"]
    amount = event["total_amount"]
    customer_email = event.get("customer_email", "")

    # Проверяем, не обрабатывалось ли это сообщение
    if await _is_message_processed(message_id, session):
        # Сообщение уже обработано, пропускаем (идемпотентность)
        return

    try:
        async with session.begin():
            # Сохраняем в inbox (в той же транзакции)
            inbox_msg = InboxMessageORM(
                message_id=message_id,
                event_type="order_created",
                payload=event,
                status="pending",
            )
            session.add(inbox_msg)
            await session.flush()

            # Обрабатываем платеж
            payment_result = await process_payment(order_id, amount)

            # Сохраняем платеж в БД
            payment = PaymentORM(
                order_id=order_id,
                payment_id=payment_result["payment_id"],
                amount=str(amount),
                status="completed" if payment_result["success"] else "failed",
                customer_email=customer_email,
            )
            session.add(payment)

            # Помечаем сообщение как обработанное
            await session.execute(
                update(InboxMessageORM)
                .where(InboxMessageORM.message_id == message_id)
                .values(status="processed", processed_at=datetime.utcnow())
            )
            # Транзакция коммитится здесь

        # Публикуем результат (после коммита транзакции)
        if payment_result["success"]:
            await publish_event("payment.processed", {
                "order_id": order_id,
                "payment_id": payment_result["payment_id"],
                "amount": amount,
                "customer_email": customer_email,
            })
            print(f"[Payment Service] Платеж обработан: {payment_result['payment_id']}")
        else:
            await publish_event("payment.failed", {
                "order_id": order_id,
                "reason": "Payment processing failed",
            })
            print(f"[Payment Service] Платеж не прошел для заказа #{order_id}")

    except Exception as exc:  # noqa: BLE001
        # Если ошибка - помечаем как failed в inbox
        try:
            async with session.begin():
                await session.execute(
                    update(InboxMessageORM)
                    .where(InboxMessageORM.message_id == message_id)
                    .values(status="failed")
                )
        except Exception:  # noqa: BLE001
            pass  # Игнорируем ошибку обновления статуса
        raise


async def consume_order_created():
    """
    Подписчик на событие order.created с использованием Transaction Inbox (ХОРЕОГРАФИЯ)
    
    Этот воркер:
    1. Подписывается на очередь payment_queue
    2. Получает события order.created
    3. Сохраняет в inbox (для идемпотентности)
    4. Обрабатывает платеж в одной транзакции с inbox
    5. Публикует payment.processed или payment.failed
    
    Transaction Inbox гарантирует:
    - Идемпотентность: одно сообщение обработается только один раз
    - Надежность: даже при повторной доставке сообщения не будет дублирования платежей
    """
    while True:
        try:
            connection = await aio_pika.connect_robust(RABBITMQ_URL)
            channel = await connection.channel()
            exchange = await channel.declare_exchange(
                EXCHANGE_NAME, aio_pika.ExchangeType.TOPIC, durable=True
            )
            queue = await channel.declare_queue(QUEUE_NAME, durable=True)
            await queue.bind(exchange, routing_key="order.created")
            
            print("[Payment Service] Подписан на order.created (с Transaction Inbox)")
            
            async with queue.iterator() as queue_iter:
                async for message in queue_iter:
                    async with message.process():
                        try:
                            event = json.loads(message.body)
                            routing_key = message.routing_key or "order.created"
                            
                            # Генерируем уникальный ID сообщения для идемпотентности
                            message_id = _generate_message_id(message.body, routing_key)
                            
                            print(f"[Payment Service] Получен заказ #{event['order_id']}, message_id: {message_id[:8]}...")
                            
                            # Обрабатываем с использованием inbox (в транзакции)
                            async with AsyncSessionLocal() as session:  # type: AsyncSession
                                await _process_payment_with_inbox(event, message_id, session)
                                
                        except Exception as exc:  # noqa: BLE001
                            print(f"[Payment Service] Ошибка обработки: {exc}")
                            
        except Exception as exc:  # noqa: BLE001
            print(f"[Payment Service] Ошибка подключения: {exc}")
            await asyncio.sleep(5)


@app.on_event("startup")
async def on_startup():
    """Инициализируем БД и запускаем подписчика в фоне"""
    await init_models()
    asyncio.create_task(consume_order_created())


@app.get("/health")
async def health():
    return {"status": "ok", "service": "payment_service"}


@app.get("/payments")
async def list_payments():
    """Список платежей из БД"""
    async with AsyncSessionLocal() as session:  # type: AsyncSession
        result = await session.execute(select(PaymentORM))
        payments = result.scalars().all()
        return {
            "payments": [
                {
                    "id": p.id,
                    "order_id": p.order_id,
                    "payment_id": p.payment_id,
                    "amount": float(p.amount),
                    "status": p.status,
                    "created_at": p.created_at.isoformat(),
                }
                for p in payments
            ]
        }


@app.post("/payments/process")
async def process_payment_endpoint(request: dict):
    """
    Эндпоинт для ОРКЕСТРАЦИИ (вызывается синхронно из order_service)
    
    В режиме хореографии этот эндпоинт не используется,
    так как payment_service подписывается на события из RabbitMQ.
    """
    order_id = request.get("order_id")
    amount = request.get("amount")
    customer_email = request.get("customer_email", "")
    
    result = await process_payment(order_id, amount)
    
    return {
        "success": result["success"],
        "payment_id": result["payment_id"],
        "order_id": order_id,
        "amount": amount,
    }

