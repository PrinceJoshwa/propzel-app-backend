from fastapi import FastAPI, APIRouter, HTTPException, Request, Response
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List
import uuid
from datetime import datetime
import httpx


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection. The CRM gateway routes do not need MongoDB, so keep the
# app bootable when local database settings have not been provided.
mongo_url = os.environ.get("MONGO_URL")
db_name = os.environ.get("DB_NAME")
client = AsyncIOMotorClient(mongo_url) if mongo_url else None
db = client[db_name] if client is not None and db_name else None

# Create the main app without a prefix
app = FastAPI()

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")


# Define Models
class StatusCheck(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    client_name: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)

class StatusCheckCreate(BaseModel):
    client_name: str

# Add your routes to the router instead of directly to app
@api_router.get("/")
async def root():
    return {"message": "Hello World"}

@api_router.post("/status", response_model=StatusCheck)
async def create_status_check(input: StatusCheckCreate):
    if db is None:
        raise HTTPException(status_code=503, detail="MongoDB is not configured")
    status_dict = input.model_dump()
    status_obj = StatusCheck(**status_dict)
    _ = await db.status_checks.insert_one(status_obj.model_dump())
    return status_obj

@api_router.get("/status", response_model=List[StatusCheck])
async def get_status_checks():
    if db is None:
        raise HTTPException(status_code=503, detail="MongoDB is not configured")
    status_checks = await db.status_checks.find().to_list(1000)
    return [StatusCheck(**status_check) for status_check in status_checks]


# ---------------------------------------------------------------------------
# CRM gateway
# ---------------------------------------------------------------------------
# The Propzel mobile app talks to the existing Tasko CRM backend. That server
# rejects the preview web origin via CORS and its session lives in HttpOnly
# cookies (which browser JS can't read). This stateless pass-through gateway
# forwards every request to the CRM unchanged and moves the session token
# through a readable `X-Session-Cookie` header instead of Set-Cookie, so the
# app works identically on web preview and on-device. No CRM logic is
# reimplemented here.
CRM_BASE = "https://taskko-crm-server.vercel.app/api"
_HOP_HEADERS = {"content-length", "host", "connection", "accept-encoding"}


@api_router.api_route(
    "/crm/{path:path}",
    methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
)
async def crm_proxy(path: str, request: Request):
    body = await request.body()
    fwd_headers = {}
    ct = request.headers.get("content-type")
    if ct:
        fwd_headers["content-type"] = ct
    accept = request.headers.get("accept")
    if accept:
        fwd_headers["accept"] = accept
    session = request.headers.get("x-session-cookie")
    if session:
        fwd_headers["cookie"] = session

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as cx:
            upstream = await cx.request(
                request.method,
                f"{CRM_BASE}/{path}",
                params=dict(request.query_params),
                content=body if body else None,
                headers=fwd_headers,
            )
    except httpx.RequestError as exc:
        logger.error("CRM proxy error: %s", exc)
        return Response(
            content=b'{"detail":"Upstream CRM request failed"}',
            status_code=502,
            media_type="application/json",
        )

    resp = Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )
    cookie_pairs = "; ".join(f"{k}={v}" for k, v in upstream.cookies.items())
    if cookie_pairs:
        resp.headers["X-Session-Cookie"] = cookie_pairs
    return resp


# Include the router in the main app
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Session-Cookie"],
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@app.on_event("shutdown")
async def shutdown_db_client():
    if client is not None:
        client.close()
