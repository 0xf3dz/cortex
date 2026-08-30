import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import Settings
from app.db import Database
from app.embedding import EmbeddingEncoder, LocalEmbeddingEncoder
from app.webhook import router as webhook_router
from app.worker import EventWorker
from app.whatsapp_client import GraphWhatsAppClient, WhatsAppClient


def create_app(
    settings: Settings | None = None,
    whatsapp_client: WhatsAppClient | None = None,
    embedding_encoder: EmbeddingEncoder | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings()  # type: ignore[call-arg]
    database = Database(resolved_settings.database_path)
    database.initialize()
    owns_whatsapp_client = whatsapp_client is None
    resolved_whatsapp_client = whatsapp_client or GraphWhatsAppClient(
        access_token=resolved_settings.meta_access_token,
        phone_number_id=resolved_settings.whatsapp_phone_number_id,
        graph_api_version=resolved_settings.whatsapp_graph_api_version,
    )
    worker: EventWorker | None = None
    if resolved_settings.worker_enabled:
        resolved_embedding_encoder = embedding_encoder or LocalEmbeddingEncoder(
            resolved_settings.embedding_model
        )
        worker = EventWorker(
            database=database,
            whatsapp_client=resolved_whatsapp_client,
            embedding_encoder=resolved_embedding_encoder,
            user_timezone=resolved_settings.user_timezone,
            poll_interval_seconds=resolved_settings.worker_poll_interval_seconds,
            stale_after_seconds=resolved_settings.worker_stale_after_seconds,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        worker_task: asyncio.Task[None] | None = None
        if worker is not None:
            worker_task = asyncio.create_task(worker.run())
        try:
            yield
        finally:
            if worker is not None and worker_task is not None:
                worker.stop()
                await worker_task
            if owns_whatsapp_client and isinstance(
                resolved_whatsapp_client,
                GraphWhatsAppClient,
            ):
                await resolved_whatsapp_client.aclose()

    application = FastAPI(
        title="WhatsApp Search",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.state.database = database
    application.state.whatsapp_client = resolved_whatsapp_client
    application.state.worker = worker
    application.include_router(webhook_router)

    @application.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return application
