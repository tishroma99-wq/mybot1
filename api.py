from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import uvicorn
import database.queries as db
from database.engine import init_db
from moderation.content_checker import auto_moderate_resource
from config import BOT_TOKEN
from aiogram import Bot
from aiogram.types import LabeledPrice
import bcrypt

bot = Bot(token=BOT_TOKEN)

def get_publish_price(count: int) -> int:
    if count == 0:
        return 0
    if count == 1:
        return 25
    return 50

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    print("FastAPI + SQLite started!")
    yield

app = FastAPI(title="TrustGram API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ========== КАТАЛОГ ==========
@app.get("/api/categories")
async def get_categories():
    categories = await db.get_categories()
    return [dict(c) for c in categories]

@app.get("/api/top-resources")
async def get_top_resources(limit: int = 20):
    resources = await db.get_top_resources(limit)
    return [dict(r) for r in resources]

@app.get("/api/top")
async def get_top(limit: int = 20):
    resources = await db.get_top_resources(limit)
    return [dict(r) for r in resources]

@app.get("/api/catalog")
async def get_catalog(category: str = "", search: str = "", limit: int = 50):
    resources = await db.get_resources(status="approved", category=category, search=search, limit=limit)
    return [dict(r) for r in resources]

@app.get("/api/resource/{resource_id}")
async def get_resource(resource_id: int):
    r = await db.get_resource_by_id(resource_id)
    if not r: raise HTTPException(status_code=404, detail="Not found")
    return dict(r)

@app.get("/api/stats")
async def get_stats():
    return await db.get_stats()

# ========== АУТЕНТИФИКАЦИЯ ==========
@app.post("/api/register")
async def register_user(data: dict):
    telegram_id = data.get("telegram_id")
    phone = data.get("phone", "").strip()
    username = data.get("username", "").strip()

    if not telegram_id:
        raise HTTPException(status_code=400, detail="Данные Telegram не найдены. Откройте через бота.")
    if not phone:
        raise HTTPException(status_code=400, detail="Нужно поделиться номером телефона")
    if not username:
        raise HTTPException(status_code=400, detail="Укажите канал/группу/бота для верификации")

    existing = await db.get_user(telegram_id)
    if existing and existing("phone"):
        raise HTTPException(status_code=409, detail="Этот аккаунт уже верифицирован")

    user_id = await db.verify_user(telegram_id, phone, username)
    return {"success": True, "user_id": user_id}

# ========== ОТЗЫВЫ ==========
@app.post("/api/resource/{resource_id}/review")
async def submit_review(resource_id: int, data: dict):
    rating = data.get("rating")
    text = data.get("text", "").strip()
    
    if not rating or not isinstance(rating, int) or rating < 1 or rating > 5:
        raise HTTPException(status_code=400, detail="Rating must be integer 1-5")
    
    if len(text) < 10:
        raise HTTPException(status_code=400, detail="Review text must be at least 10 characters")
    
    resource = await db.get_resource_by_id(resource_id)
    if not resource:
        raise HTTPException(status_code=404, detail="Resource not found")
    
    review_id = await db.add_review(resource_id, 0, rating, text)
    return {"success": True, "review_id": review_id}

@app.post("/api/resources/submit")
async def submit_resource(data: dict):
    telegram_id = data.get("telegram_id")
    username = (data.get("username") or "").strip()
    category = data.get("category")
    description = (data.get("description") or "").strip()

    if not telegram_id or not username or not category:
        raise HTTPException(status_code=400, detail="Заполните все поля")
    if len(description) < 10:
        raise HTTPException(status_code=400, detail="Описание — минимум 10 символов")

    try:
        chat = await bot.get_chat(f"@{username}")
    except Exception:
        raise HTTPException(status_code=404, detail="Ресурс не найден в Telegram")

    rtype = "channel" if chat.type == "channel" else "group" if chat.type in ("supergroup", "group") else "bot"
    status, reason = await auto_moderate_resource(description, rtype)
    if status == "rejected":
        raise HTTPException(status_code=400, detail=f"Отклонено модерацией: {reason}")

    ctx = {"type": rtype, "username": username, "title": chat.title or username, "chat_id": chat.id,
           "category": category, "description": description, "social": "none", "auto_status": status}

    count = await db.count_resources(telegram_id)
    price = get_publish_price(count)

    if price == 0:
        await db.create_resource(owner_id=telegram_id, resource_type=rtype, title=ctx["title"], username=username,
                                  chat_id=chat.id, category=category, description=description, social_link="none")
        return {"success": True, "free": True}

    await db.set_context(telegram_id, "awaiting_payment", ctx)
    await bot.send_invoice(chat_id=telegram_id, title="Публикация ресурса",
                            description=f"Размещение «{ctx['title']}» в каталоге TrustGram",
                            payload=f"publish_{telegram_id}", currency="XTR",
                            prices=[LabeledPrice(label="Публикация ресурса", amount=price)])
    return {"success": True, "free": False, "price": price}

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
