from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.routes import router
from db.session import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Runs init_db() on startup so tables are always created before the first request.
    Using lifespan instead of @app.on_event — on_event is deprecated in FastAPI 0.100+.
    """
    init_db()
    yield
    # nothing to clean up on shutdown for now


app = FastAPI(
    title="Backcurrent — AI Revenue Recovery",
    description=(
        "Agent that detects failed subscription payments, diagnoses the specific decline reason, "
        "decides the right recovery action, and executes it — with a full audit trail and "
        "compliance stopping rules built in."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(router)
