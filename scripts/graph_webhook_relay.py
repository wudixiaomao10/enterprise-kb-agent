from __future__ import annotations

import os

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response


TARGET_URL = os.getenv(
    "KNOWLEDGE_GRAPH_RELAY_TARGET",
    "http://127.0.0.1:8010/webhooks/microsoft-graph",
)
FEISHU_TARGET_URL = os.getenv(
    "KNOWLEDGE_FEISHU_RELAY_TARGET",
    "http://127.0.0.1:8010/webhooks/feishu",
)
MAX_BODY_BYTES = int(os.getenv("KNOWLEDGE_GRAPH_RELAY_MAX_BYTES", "1048576"))

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.post("/webhooks/microsoft-graph")
async def relay_graph_webhook(request: Request) -> Response:
    return await relay_webhook(request, TARGET_URL)


@app.post("/webhooks/feishu")
async def relay_feishu_webhook(request: Request) -> Response:
    return await relay_webhook(request, FEISHU_TARGET_URL)


async def relay_webhook(request: Request, target_url: str) -> Response:
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Webhook payload too large")
    query = request.url.query
    target = f"{target_url}?{query}" if query else target_url
    headers = {}
    for name in (
        "content-type",
        "x-lark-request-timestamp",
        "x-lark-request-nonce",
        "x-lark-signature",
    ):
        if value := request.headers.get(name):
            headers[name] = value
    async with httpx.AsyncClient(timeout=8.0) as client:
        response = await client.post(target, content=body, headers=headers)
    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=response.headers.get("content-type"),
    )
