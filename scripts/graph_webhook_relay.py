from __future__ import annotations

import os

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response


TARGET_URL = os.getenv(
    "KNOWLEDGE_GRAPH_RELAY_TARGET",
    "http://127.0.0.1:8010/webhooks/microsoft-graph",
)
MAX_BODY_BYTES = int(os.getenv("KNOWLEDGE_GRAPH_RELAY_MAX_BYTES", "1048576"))

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.post("/webhooks/microsoft-graph")
async def relay_graph_webhook(request: Request) -> Response:
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Webhook payload too large")
    query = request.url.query
    target = f"{TARGET_URL}?{query}" if query else TARGET_URL
    headers = {}
    content_type = request.headers.get("content-type")
    if content_type:
        headers["content-type"] = content_type
    async with httpx.AsyncClient(timeout=8.0) as client:
        response = await client.post(target, content=body, headers=headers)
    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=response.headers.get("content-type"),
    )
