from fastapi import FastAPI
import os

app = FastAPI(title="backend-api-phase1")


@app.get("/healthz")
def healthz():
    return {
        "status": "ok",
        "service": "backend-api",
        "database_url_present": bool(os.getenv("DATABASE_URL")),
    }


@app.get("/v1/ping")
def ping():
    return {"message": "pong"}
