from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from backend.api_routes import router

app = FastAPI(title="Companion Chat API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)

@app.get("/")
async def root():
    return {"status": "ok", "service": "companion-chat"}
