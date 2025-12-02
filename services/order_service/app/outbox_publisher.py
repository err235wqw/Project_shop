import asyncio
import json
import os

import aio_pika
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .db import AsyncSessionLocal, OutboxMessageORM


def _get_required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} must be set")
    return value


RABBITMQ_URL = _get_required_env("RABBITMQ_URL")
EXCHANGE_NAME = "shop_events"
ROUTING_KEY_ORDER_CREATED = "order.created"


async def publish_outbox_messages():
    """
    Простой воркер: каждые N секунд читает pending-сообщения из outbox,
    публикует в RabbitMQ и помечает как sent.
    """
    while True:
        try:
            connection = await aio_pika.connect_robust(RABBITMQ_URL)
            channel = await connection.channel()
            exchange = await channel.declare_exchange(
                EXCHANGE_NAME, aio_pika.ExchangeType.TOPIC, durable=True
            )

            async with AsyncSessionLocal() as session:  # type: AsyncSession
                result = await session.execute(
                    select(OutboxMessageORM).where(OutboxMessageORM.status == "pending")
                )
                messages = result.scalars().all()

                for msg in messages:
                    body = json.dumps(msg.payload).encode("utf-8")
                    routing_key = (
                        ROUTING_KEY_ORDER_CREATED
                        if msg.event_type == "order_created"
                        else "events.generic"
                    )

                    await exchange.publish(
                        aio_pika.Message(body=body),
                        routing_key=routing_key,
                    )

                    await session.execute(
                        update(OutboxMessageORM)
                        .where(OutboxMessageORM.id == msg.id)
                        .values(status="sent")
                    )

                if messages:
                    await session.commit()

            await connection.close()
        except Exception as exc:  # noqa: BLE001
            # Для примера просто печатаем ошибку
            print("Outbox worker error:", exc)

        await asyncio.sleep(5)


