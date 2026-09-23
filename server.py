from fastapi import FastAPI, APIRouter, HTTPException, Depends, Header, Query, File, UploadFile
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import re
import io
import math
import random
import logging
import difflib
import asyncio
import jwt
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
import uuid
from datetime import datetime, timezone, timedelta
import time
import json
import urllib.request
import urllib.error

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(
    mongo_url,
    maxPoolSize=20,
    minPoolSize=1,
    serverSelectionTimeoutMS=10000,
    connectTimeoutMS=10000,
    socketTimeoutMS=45000,
    retryReads=True,
    retryWrites=True,
)
db = client[os.environ['DB_NAME']]
JWT_SECRET = os.environ['JWT_SECRET']
TELEGRAM_BOT_TOKEN = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_BOT_USERNAME = (os.environ.get("TELEGRAM_BOT_USERNAME") or "").strip().lstrip("@")
TELEGRAM_WEBHOOK_SECRET = (os.environ.get("TELEGRAM_WEBHOOK_SECRET") or "").strip()

app = FastAPI()
api_router = APIRouter(prefix="/api")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Return JSON instead of plain-text 500 so frontend can show the real error."""
    from fastapi.responses import JSONResponse
    logger.exception("Unhandled error on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": f"Server xatosi: {type(exc).__name__}: {exc}"})


# ---------- HTTP Method Override middleware ----------
# Some hosting providers / proxies (nginx, Cloudflare, some shared hosts)
# only forward GET/POST and block PUT/PATCH/DELETE -> client gets HTTP 405
# ("Method Not Allowed"). The frontend sends an X-HTTP-Method-Override
# header alongside a real POST; this middleware rewrites the request so it
# reaches the right FastAPI route handler.
class MethodOverrideMiddleware:
    # NOTE: this is intentionally a *pure ASGI* middleware, not a
    # starlette.middleware.base.BaseHTTPMiddleware. BaseHTTPMiddleware's
    # call_next() closes over the ORIGINAL scope object captured in
    # __call__ and ignores any mutated scope/Request passed into
    # dispatch(), so rewriting request.scope["method"] there is a no-op
    # and routing still used the original method -> every PUT/DELETE
    # request coming in as POST+override kept hitting the POST handler
    # (or 405 when there was no POST route), which is exactly the
    # "Method Not Allowed" bug reported for seller product edit/delete.
    # A raw ASGI middleware receives the real scope dict and passes it
    # straight to the wrapped app, so mutating it here actually changes
    # which route gets matched.
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            raw_headers = dict(scope.get("headers") or [])
            override = raw_headers.get(b"x-http-method-override")
            if override:
                override_method = override.decode("latin-1").upper()
                if override_method and override_method != scope["method"].upper():
                    scope = dict(scope)
                    scope["method"] = override_method
        await self.app(scope, receive, send)


def now():
    return datetime.now(timezone.utc)



def local_day_key(ts=None) -> str:
    """Toshkent kuni (UTC+5) YYYY-MM-DD — bugungi statistika uchun."""
    if ts is None:
        dt = now() + timedelta(hours=5)
    elif isinstance(ts, datetime):
        dt = ts
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc) + timedelta(hours=5)
    else:
        s = str(ts or "")
        try:
            raw = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(timezone.utc) + timedelta(hours=5)
        except Exception:
            return s[:10] if len(s) >= 10 else ""
    return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"


def order_seller_amount(o: dict) -> float:
    """Sotuvchi aylanmasi: seller_subtotal → earn_total → items → subtotal."""
    for key in ("seller_subtotal", "earn_total", "seller_total"):
        v = o.get(key)
        if v is not None:
            try:
                f = float(v)
                if f > 0 or v == 0 or v == 0.0:
                    return f
            except Exception:
                pass
    total = 0.0
    for i in (o.get("items") or []):
        try:
            unit = float(i.get("base_price") or i.get("earn") or i.get("seller_price") or i.get("price") or 0)
            qty = float(i.get("qty") or 0)
            total += unit * qty
        except Exception:
            continue
    if total > 0:
        return total
    try:
        return float(o.get("subtotal") or 0)
    except Exception:
        return 0.0


def iso(dt=None):
    return (dt or now()).isoformat()


def uid():
    return str(uuid.uuid4())


def json_safe(obj):
    """Recursively convert Mongo/BSON values to JSON-serializable Python types.
    Prevents 500 Internal Server Error when FastAPI cannot encode ObjectId/datetime/bytes.
    """
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items() if k != "_id"}
    if isinstance(obj, (list, tuple, set)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    # ObjectId, Decimal128, etc.
    try:
        from bson import ObjectId
        if isinstance(obj, ObjectId):
            return str(obj)
    except Exception:
        pass
    try:
        return str(obj)
    except Exception:
        return None


# ---------- Models ----------
class SendOtpReq(BaseModel):
    phone: str


class VerifyOtpReq(BaseModel):
    phone: str
    code: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    language: Optional[str] = "uz"
    address_text: Optional[str] = None  # ixtiyoriy yozuv (chekda chiqadi)
    profile_note: Optional[str] = None


class ProfileReq(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    language: Optional[str] = None


class AddressReq(BaseModel):
    label: str
    text: str
    lat: Optional[float] = None
    lng: Optional[float] = None


class LocationReq(BaseModel):
    lat: float
    lng: float


class PromoReq(BaseModel):
    code: str
    subtotal: float


class OrderItemReq(BaseModel):
    product_id: str
    qty: int
    variation: Optional[str] = None


class OrderReq(BaseModel):
    items: List[OrderItemReq]
    address_text: str
    address_lat: Optional[float] = None
    address_lng: Optional[float] = None
    delivery_method: str = "courier"  # courier | pickup
    payment_method: str = "cash"
    comment: Optional[str] = ""
    promo_code: Optional[str] = None


class ReviewReq(BaseModel):
    rating: int
    text: str


class SellerApplyReq(BaseModel):
    shop_name: str
    document: Optional[str] = ""
    shop_lat: Optional[float] = None
    shop_lng: Optional[float] = None


class MarkupReq(BaseModel):
    percent: float


class BulkMarkupReq(BaseModel):
    percent: float
    only_without_override: bool = False


class CourierApplyReq(BaseModel):
    zone: str = "Toshkent"
    lat: Optional[float] = None
    lng: Optional[float] = None


class ProductReq(BaseModel):
    name_uz: str
    name_ru: str = ""
    name_en: str = ""
    desc_uz: str = ""
    desc_ru: str = ""
    desc_en: str = ""
    category_id: str
    price: float
    old_price: Optional[float] = None
    cost_price: Optional[float] = None  # tannarx (sof foyda hisobi uchun)
    box_price: Optional[float] = None
    units_per_box: int = 0
    images: List[str] = []
    stock: int = 0
    variations: List[Dict[str, Any]] = []
    unit_type: str = "piece"  # piece | kg


class ItemDecisionReq(BaseModel):
    index: int
    action: str  # accept | reject


class KgExtraReq(BaseModel):
    index: int
    extra_qty: float = 0  # ortiqcha kg (yoki gram sifatida 0.05 = 50g)
    extra_price: float = 0  # sotuvchi yozgan ortiqcha summa (sof)


class ActionReq(BaseModel):
    action: str
    reason: Optional[str] = ""
    # partial accept: har bir mahsulot bo'yicha qaror
    items: Optional[List[ItemDecisionReq]] = None
    # kg mahsulotlar uchun ortiqcha og'irlik (Yig'ildi paytida)
    kg_extras: Optional[List[KgExtraReq]] = None


class StatusReq(BaseModel):
    status: str


class CourierFinalizeItemReq(BaseModel):
    index: int
    action: str = "delivered"  # delivered | returned


class CourierFinalizeReq(BaseModel):
    items: List[CourierFinalizeItemReq]


class ToggleReq(BaseModel):
    online: bool


class CategoryReq(BaseModel):
    name_uz: str
    name_ru: str = ""
    name_en: str = ""
    icon: str = "package"
    parent_id: Optional[str] = None
    order: int = 0
    preview_image: Optional[str] = None


class BannerReq(BaseModel):
    image: str
    title: str
    link_type: str = "none"
    link_id: Optional[str] = None
    expires_at: Optional[str] = None


class PromoCreateReq(BaseModel):
    code: str
    type: str = "percent"
    value: float
    min_cart: float = 0
    limit: int = 100
    expires_at: Optional[str] = None


class FlashReq(BaseModel):
    product_id: str
    price: float
    hours: int = 24


class SettingsReq(BaseModel):
    delivery_fee: Optional[float] = None
    min_order: Optional[float] = None
    work_hours: Optional[str] = None
    contact: Optional[str] = None
    default_markup_percent: Optional[float] = None
    default_delivery_eta_days: Optional[int] = None
    # Ortiqcha kg uchun moderator/admin foizi (sotuvchi yozgan summaga ustama)
    kg_extra_markup_percent: Optional[float] = None


class CourierCreateReq(BaseModel):
    phone: str
    first_name: str
    zone: str = "Toshkent"
    is_admin_courier: bool = False


class BlockReq(BaseModel):
    blocked: bool


class EtaReq(BaseModel):
    eta_days: int = 0


class RejectedReplacementReq(BaseModel):
    item_index: int
    product_id: str


class ResolveRejectedOrderReq(BaseModel):
    replacements: List[RejectedReplacementReq] = []
    eta_days: Optional[int] = None
    note: Optional[str] = ""


# ---------- Auth helpers ----------
def make_token(user_id: str, role: str):
    return jwt.encode({"sub": user_id, "role": role, "exp": now() + timedelta(days=30)}, JWT_SECRET, algorithm="HS256")


async def get_user_optional(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        return None
    try:
        payload = jwt.decode(authorization[7:], JWT_SECRET, algorithms=["HS256"])
        user = await db.users.find_one({"id": payload["sub"]}, {"_id": 0})
        if user and user.get("blocked"):
            return None
        return user
    except Exception:
        return None


async def get_user(authorization: Optional[str] = Header(None)):
    user = await get_user_optional(authorization)
    if not user:
        raise HTTPException(401, "Avtorizatsiya talab qilinadi")
    return user


async def get_admin(user=Depends(get_user)):
    if user["role"] not in ("admin", "moderator"):
        raise HTTPException(403, "Ruxsat yo'q")
    return user


def public_user(u):
    u = {k: v for k, v in u.items() if k != "_id"}
    return u


async def notify(user_id: str, title: str, body: str):
    await db.notifications.insert_one({"id": uid(), "user_id": user_id, "title": title, "body": body, "read": False, "created_at": iso()})


TASHKENT_CENTER = (41.311081, 69.240562)


async def get_settings():
    return await db.settings.find_one({"id": "main"}) or {}


async def geocode_best_effort(address_text: str):
    """Free geocoding via OpenStreetMap Nominatim. Best-effort only — never raises."""
    try:
        import httpx
        q = address_text if "toshkent" in address_text.lower() or "tashkent" in address_text.lower() else f"{address_text}, Toshkent"
        async with httpx.AsyncClient(timeout=4.0) as c:
            r = await c.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": q, "format": "json", "limit": 1},
                headers={"User-Agent": "UzMarket/1.0 (delivery-app)"},
            )
            data = r.json()
            if data:
                return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        pass
    return None


def shop_location(seller_user: Optional[dict]):
    if seller_user:
        si = seller_user.get("seller_info") or {}
        if si.get("shop_lat") is not None and si.get("shop_lng") is not None:
            return si["shop_lat"], si["shop_lng"]
        addrs = seller_user.get("addresses") or []
        for a in addrs:
            if a.get("lat") is not None and a.get("lng") is not None:
                return a["lat"], a["lng"]
    return TASHKENT_CENTER



# ---------- Telegram OTP bot ----------
def normalize_phone(raw: str) -> str:
    digits = re.sub(r"[^\d+]", "", raw or "")
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if not digits.startswith("+") and len(digits) >= 9:
        # O'zbekiston: 998...
        if digits.startswith("998"):
            digits = "+" + digits
        elif len(digits) == 9:
            digits = "+998" + digits
        else:
            digits = "+" + digits
    return digits


def _telegram_api(method: str, payload: dict) -> dict:
    """Sinxron Telegram Bot API chaqiruvi."""
    if not TELEGRAM_BOT_TOKEN:
        return {"ok": False, "description": "TELEGRAM_BOT_TOKEN yo'q"}
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8")
            return json.loads(body)
        except Exception:
            return {"ok": False, "description": str(e)}
    except Exception as e:
        return {"ok": False, "description": str(e)}


async def telegram_send_message(chat_id, text: str, reply_markup: Optional[dict] = None) -> bool:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    loop = asyncio.get_event_loop()
    res = await loop.run_in_executor(None, lambda: _telegram_api("sendMessage", payload))
    ok = bool(res.get("ok"))
    if not ok:
        logger.warning("Telegram send failed: %s", res)
    return ok


async def find_telegram_chat_id(phone: str) -> Optional[int]:
    phone = normalize_phone(phone)
    # variants
    variants = {phone}
    if phone.startswith("+"):
        variants.add(phone[1:])
    link = await db.telegram_links.find_one({"phone": {"$in": list(variants)}}, sort=[("linked_at", -1)])
    if link and link.get("chat_id") is not None:
        return link["chat_id"]
    # users collection fallback
    u = await db.users.find_one({"phone": {"$in": list(variants)}, "telegram_chat_id": {"$ne": None}})
    if u and u.get("telegram_chat_id") is not None:
        return u["telegram_chat_id"]
    return None


async def link_telegram_phone(chat_id: int, phone: str, tg_user: Optional[dict] = None):
    phone = normalize_phone(phone)
    doc = {
        "phone": phone,
        "chat_id": int(chat_id),
        "telegram_user_id": (tg_user or {}).get("id"),
        "telegram_username": (tg_user or {}).get("username"),
        "linked_at": iso(),
    }
    await db.telegram_links.update_one(
        {"phone": phone},
        {"$set": doc},
        upsert=True,
    )
    await db.users.update_one(
        {"phone": phone},
        {"$set": {"telegram_chat_id": int(chat_id), "telegram_username": (tg_user or {}).get("username")}},
    )


# ---------- Auth ----------

@api_router.post("/telegram/webhook")
async def telegram_webhook(update: dict, x_telegram_bot_api_secret_token: Optional[str] = Header(None)):
    """Telegram bot webhook: /start va kontakt ulash orqali phone ↔ chat_id bog'lash."""
    if TELEGRAM_WEBHOOK_SECRET and x_telegram_bot_api_secret_token != TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(403, "Forbidden")
    try:
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        from_user = message.get("from") or {}
        text = (message.get("text") or "").strip()
        contact = message.get("contact")

        if not chat_id:
            return {"ok": True}

        # Kontakt ulashildi
        if contact and contact.get("phone_number"):
            # Faqat o'z kontaktini qabul qilamiz
            if contact.get("user_id") and from_user.get("id") and contact.get("user_id") != from_user.get("id"):
                await telegram_send_message(chat_id, "Faqat o'zingizning telefon raqamingizni ulashing.")
                return {"ok": True}
            phone = normalize_phone(str(contact.get("phone_number")))
            await link_telegram_phone(chat_id, phone, from_user)
            await telegram_send_message(
                chat_id,
                f"✅ Raqam ulandi: <b>{phone}</b>\n\nEndi ilovada shu raqam bilan kirishingiz mumkin — tasdiqlash kodi shu yerga keladi.",
            )
            return {"ok": True}

        if text.startswith("/start") or text in ("/help", "start"):
            # /start TOKEN — ilovadan kelgan deep link
            parts = text.split(maxsplit=1)
            payload = (parts[1].strip() if len(parts) > 1 else "") or ""
            if payload and payload not in ("start", "help"):
                tok = await db.telegram_start_tokens.find_one(
                    {"token": payload, "used": False},
                    sort=[("created_at", -1)],
                )
                if tok and tok.get("expires_at", "") >= iso():
                    phone = normalize_phone(tok["phone"])
                    code = tok.get("code") or ""
                    await link_telegram_phone(chat_id, phone, from_user)
                    await db.telegram_start_tokens.update_one(
                        {"token": payload}, {"$set": {"used": True, "chat_id": chat_id}}
                    )
                    # Kodni darhol yuborish
                    if code:
                        await telegram_send_message(
                            chat_id,
                            f"<b>ZarraMarket</b> tasdiqlash kodi:\n\n"
                            f"<code>{code}</code>\n\n"
                            f"Kod 5 daqiqa amal qiladi. Ilovaga qaytib kodni kiriting.",
                        )
                    else:
                        await telegram_send_message(
                            chat_id,
                            f"✅ Raqam ulandi: <b>{phone}</b>\nIlovada qayta «Kod olish» ni bosing.",
                        )
                    return {"ok": True}
                else:
                    await telegram_send_message(
                        chat_id,
                        "Havola eskirgan yoki noto'g'ri. Ilovada qayta «Kod olish» ni bosing.",
                    )
                    return {"ok": True}

            kb = {
                "keyboard": [[{"text": "📱 Telefon raqamni ulash", "request_contact": True}]],
                "resize_keyboard": True,
                "one_time_keyboard": True,
            }
            await telegram_send_message(
                chat_id,
                "Assalomu alaykum! <b>ZarraMarket</b> botiga xush kelibsiz.\n\n"
                "Ilovadan kirish uchun avval ilovada telefon raqamingizni kiriting va «Kod olish» ni bosing — "
                "Telegram avtomatik ochiladi.\n\n"
                "Yoki pastdagi tugma orqali raqamni ulashing.",
                reply_markup=kb,
            )
            return {"ok": True}

        if text:
            await telegram_send_message(
                chat_id,
                "Telefon raqamni ulash uchun /start bosing va «Telefon raqamni ulash» tugmasini bosing.",
            )
        return {"ok": True}
    except Exception as e:
        logger.exception("telegram webhook: %s", e)
        return {"ok": True}


@api_router.get("/telegram/bot-info")
async def telegram_bot_info():
    """Frontend uchun bot username/link."""
    return {
        "configured": bool(TELEGRAM_BOT_TOKEN),
        "username": TELEGRAM_BOT_USERNAME or None,
        "link": f"https://t.me/{TELEGRAM_BOT_USERNAME}" if TELEGRAM_BOT_USERNAME else None,
    }

@api_router.post("/auth/send-otp")
async def send_otp(req: SendOtpReq):
    """
    1) Agar raqam botga ulangan bo'lsa — kod to'g'ridan Telegramga.
    2) Aks holda — deep link qaytariladi: ilova t.me/Bot?start=TOKEN ochadi,
       foydalanuvchi Start bosadi → bot kodni yuboradi.
    """
    phone = normalize_phone(req.phone)
    if len(re.sub(r"\D", "", phone)) < 9:
        raise HTTPException(400, "Telefon raqam noto'g'ri")
    minute_ago = iso(now() - timedelta(minutes=1))
    hour_ago = iso(now() - timedelta(hours=1))
    if await db.otps.find_one({"phone": phone, "created_at": {"$gt": minute_ago}}):
        raise HTTPException(429, "1 daqiqada faqat 1 ta kod yuborish mumkin")
    if await db.otps.count_documents({"phone": phone, "created_at": {"$gt": hour_ago}}) >= 5:
        raise HTTPException(429, "1 soatda maksimum 5 ta kod. Keyinroq urinib ko'ring")

    code = f"{random.randint(100000, 999999)}"
    await db.otps.insert_one({
        "id": uid(), "phone": phone, "code": code,
        "expires_at": iso(now() + timedelta(minutes=5)),
        "created_at": iso(), "used": False,
    })
    exists = await db.users.find_one({"phone": phone}) is not None
    bot_username = TELEGRAM_BOT_USERNAME or "your_bot"
    bot_link_base = f"https://t.me/{bot_username}"

    if not TELEGRAM_BOT_TOKEN:
        await db.sms_log.insert_one({
            "id": uid(), "phone": phone, "text": f"OTP {code}", "status": "demo", "sent_at": iso(),
        })
        logger.info(f"DEMO OTP for {phone}: {code}")
        return {
            "demo_code": code,
            "expires_in": 300,
            "exists": exists,
            "demo": True,
            "channel": "demo",
            "need_start": False,
            "message": "Demo rejim — kod ekranda",
            "bot_link": bot_link_base if TELEGRAM_BOT_USERNAME else None,
        }

    chat_id = await find_telegram_chat_id(phone)
    if chat_id:
        text = (
            f"<b>ZarraMarket</b> tasdiqlash kodi:\n\n"
            f"<code>{code}</code>\n\n"
            f"Kod 5 daqiqa amal qiladi. Hech kimga bermang."
        )
        ok = await telegram_send_message(chat_id, text)
        await db.sms_log.insert_one({
            "id": uid(), "phone": phone, "text": f"OTP chat={chat_id}",
            "status": "telegram_ok" if ok else "telegram_fail", "sent_at": iso(),
        })
        if not ok:
            # bog'lanish buzilgan — qayta start
            chat_id = None
        else:
            return {
                "expires_in": 300,
                "exists": exists,
                "demo": False,
                "channel": "telegram",
                "need_start": False,
                "message": "Kod Telegramga yuborildi",
                "bot_link": bot_link_base,
                "bot_username": bot_username,
            }

    # Ulanmagan: deep-link token
    token = uid().replace("-", "")[:16]
    await db.telegram_start_tokens.insert_one({
        "token": token,
        "phone": phone,
        "code": code,
        "expires_at": iso(now() + timedelta(minutes=10)),
        "created_at": iso(),
        "used": False,
    })
    deep = f"{bot_link_base}?start={token}"
    await db.sms_log.insert_one({
        "id": uid(), "phone": phone, "text": f"OTP pending start token={token}",
        "status": "await_start", "sent_at": iso(),
    })
    return {
        "expires_in": 300,
        "exists": exists,
        "demo": False,
        "channel": "telegram_start",
        "need_start": True,
        "message": "Telegram ochiladi — Start bosing, kod botga keladi",
        "bot_link": deep,
        "bot_username": bot_username,
    }


@api_router.post("/auth/verify-otp")
async def verify_otp(req: VerifyOtpReq):
    phone = normalize_phone(req.phone)
    otp = await db.otps.find_one({"phone": phone, "code": req.code, "used": False}, sort=[("created_at", -1)])
    if not otp:
        raise HTTPException(400, "Kod noto'g'ri")
    if otp["expires_at"] < iso():
        raise HTTPException(400, "Kod muddati tugagan")
    await db.otps.update_one({"id": otp["id"]}, {"$set": {"used": True}})
    user = await db.users.find_one({"phone": phone})
    is_new = user is None
    if is_new:
        addresses = []
        note = (
            (getattr(req, "profile_note", None) or req.address_text or "")
            if True
            else ""
        )
        note = str(note or "").strip()
        if note:
            addresses.append({
                "id": uid(),
                "label": "Ixtiyoriy",
                "text": note,
                "lat": None,
                "lng": None,
            })
        user = {
            "id": uid(), "phone": phone,
            "first_name": req.first_name or "Foydalanuvchi", "last_name": req.last_name or "",
            "role": "client", "language": req.language or "uz", "blocked": False,
            "referral_code": f"UZ{random.randint(10000, 99999)}",
            "favorites": [], "addresses": addresses,
            "profile_note": note,
            "created_at": iso(),
        }
        await db.users.insert_one(dict(user))
    if user.get("blocked"):
        raise HTTPException(403, "Akkaunt bloklangan")
    return {"token": make_token(user["id"], user["role"]), "user": public_user(user), "is_new": is_new}


@api_router.get("/auth/me")
async def me(user=Depends(get_user)):
    return public_user(user)


@api_router.put("/auth/profile")
async def update_profile(req: ProfileReq, user=Depends(get_user)):
    upd = {k: v for k, v in req.dict().items() if v is not None}
    if upd:
        await db.users.update_one({"id": user["id"]}, {"$set": upd})
    u = await db.users.find_one({"id": user["id"]}, {"_id": 0})
    return u


@api_router.delete("/auth/account")
async def delete_account(user=Depends(get_user)):
    await db.users.delete_one({"id": user["id"]})
    return {"ok": True}


# ---------- Addresses ----------
@api_router.post("/addresses")
async def add_address(req: AddressReq, user=Depends(get_user)):
    addr = {"id": uid(), **req.dict()}
    await db.users.update_one({"id": user["id"]}, {"$push": {"addresses": addr}})
    return addr


@api_router.delete("/addresses/{addr_id}")
async def del_address(addr_id: str, user=Depends(get_user)):
    await db.users.update_one({"id": user["id"]}, {"$pull": {"addresses": {"id": addr_id}}})
    return {"ok": True}


# ---------- Location ----------
@api_router.put("/users/me/location")
async def save_my_location(req: LocationReq, user=Depends(get_user)):
    loc = {"lat": req.lat, "lng": req.lng, "updated_at": iso()}
    await db.users.update_one({"id": user["id"]}, {"$set": {"saved_location": loc}})
    u = await db.users.find_one({"id": user["id"]}, {"_id": 0})
    return public_user(u)


@api_router.put("/seller/location")
async def save_shop_location(req: LocationReq, user=Depends(get_user)):
    if not user.get("seller_info"):
        raise HTTPException(403, "Siz sotuvchi emassiz")
    await db.users.update_one({"id": user["id"]}, {"$set": {"seller_info.shop_lat": req.lat, "seller_info.shop_lng": req.lng}})
    u = await db.users.find_one({"id": user["id"]}, {"_id": 0})
    return public_user(u)


# ---------- Catalog ----------
# In-memory cache for /categories (60s TTL). Home + catalog both call this.
# CRITICAL: never scan products here — that query timed out on Atlas (20s
# socketTimeout) and produced 500 on every /categories request.
_CATEGORIES_CACHE: Dict[str, Any] = {"data": None, "expires_at": 0.0}


@api_router.get("/categories")
async def categories():
    now_ts = time.time()
    if _CATEGORIES_CACHE["data"] is not None and now_ts < _CATEGORIES_CACHE["expires_at"]:
        return _CATEGORIES_CACHE["data"]

    try:
        # Lightweight query only. max_time_ms fails fast instead of hanging 20–45s.
        cats = await db.categories.find({}, {"_id": 0}).sort("order", 1).max_time_ms(8000).to_list(200)
        safe_cats = json_safe(cats)
        _CATEGORIES_CACHE["data"] = safe_cats
        _CATEGORIES_CACHE["expires_at"] = now_ts + 60.0
        return safe_cats
    except Exception as e:
        logger.exception("categories endpoint failed: %s", e)
        if _CATEGORIES_CACHE["data"] is not None:
            return _CATEGORIES_CACHE["data"]
        return []


@api_router.get("/banners")
async def banners():
    try:
        items = await db.banners.find({"active": True}, {"_id": 0}).sort("order", 1).to_list(20)
        now_iso = iso()
        filtered = [b for b in items if not b.get("expires_at") or b["expires_at"] > now_iso]
        return json_safe(filtered)
    except Exception as e:
        logger.exception("banners failed: %s", e)
        return []


SETTINGS_CACHE: Dict[str, Any] = {"default_markup_percent": 0}

# Home / katalog list uchun qisqa TTL cache
_PRODUCTS_LIST_CACHE: Dict[str, Any] = {"data": {}, "expires": {}}
_PRODUCTS_LIST_TTL = 25.0


def product_out(p):
    """Safe product serializer — never raises on missing/bad fields."""
    if not p:
        return {}
    try:
        p = {k: v for k, v in dict(p).items() if k != "_id"}
        # normalize name/desc to dict so frontend never crashes
        name = p.get("name")
        if not isinstance(name, dict):
            p["name"] = {"uz": str(name or ""), "ru": "", "en": ""}
        else:
            p["name"] = {
                "uz": str(name.get("uz") or name.get("ru") or name.get("en") or ""),
                "ru": str(name.get("ru") or ""),
                "en": str(name.get("en") or ""),
            }
        desc = p.get("desc")
        if not isinstance(desc, dict):
            p["desc"] = {"uz": str(desc or ""), "ru": "", "en": ""}

        fs = p.get("flash_sale") if isinstance(p.get("flash_sale"), dict) else None
        base_price = float(p.get("price") or 0)
        markup = p.get("markup_percent")
        if markup is None:
            markup = SETTINGS_CACHE.get("default_markup_percent") or 0
        try:
            markup = float(markup or 0)
        except (TypeError, ValueError):
            markup = 0.0
        try:
            units_per_box = max(int(p.get("units_per_box") or 0), 0)
        except (TypeError, ValueError):
            units_per_box = 0
        seller_old_price = p.get("old_price")
        seller_box_price = p.get("box_price")
        if units_per_box > 0 and not seller_box_price:
            seller_box_price = round(base_price * units_per_box)

        marked_price = round(base_price * (1 + markup / 100)) if markup else base_price
        effective_old_price = None
        if seller_old_price is not None:
            try:
                effective_old_price = round(float(seller_old_price) * (1 + markup / 100)) if markup else float(seller_old_price)
            except (TypeError, ValueError):
                effective_old_price = None

        flash_active = bool(fs and str(fs.get("ends_at") or "") > iso())
        try:
            seller_effective_price = float(fs["price"]) if flash_active else base_price
            effective_price = float(fs["price"]) if flash_active else marked_price
        except (TypeError, ValueError, KeyError):
            seller_effective_price = base_price
            effective_price = marked_price
            flash_active = False

        effective_box_price = None
        seller_effective_box_price = None
        if units_per_box > 0:
            if flash_active:
                seller_effective_box_price = round(seller_effective_price * units_per_box)
                effective_box_price = round(effective_price * units_per_box)
            else:
                try:
                    base_box = float(seller_box_price if seller_box_price else base_price * units_per_box)
                except (TypeError, ValueError):
                    base_box = base_price * units_per_box
                seller_effective_box_price = base_box
                effective_box_price = round(base_box * (1 + markup / 100)) if markup else base_box

        unit_type = str(p.get("unit_type") or "piece").lower().strip()
        if unit_type not in ("piece", "kg"):
            unit_type = "piece"
        if unit_type == "kg":
            sale_mode = "kg"
        elif units_per_box > 0:
            sale_mode = "box"
        else:
            sale_mode = "piece"
        display_price = effective_box_price if sale_mode == "box" and effective_box_price is not None else effective_price
        display_old_price = None
        if sale_mode == "box" and effective_old_price is not None:
            display_old_price = round(effective_old_price * units_per_box)
        elif effective_old_price is not None:
            display_old_price = effective_old_price

        try:
            stock_total_units = int(p.get("stock", 0) or 0)
        except (TypeError, ValueError):
            stock_total_units = 0
        # Ombor har doim dona/kg (stock maydoni) da saqlanadi.
        # Quti faqat ko'rsatish uchun: to'liq quti soni + qolgan dona.
        display_stock = stock_total_units
        boxes_available = 0
        if sale_mode == "kg":
            display_stock_label = "kg"
        elif units_per_box > 0:
            boxes_available = stock_total_units // units_per_box
            display_stock_label = "dona"
            # UI uchun quti sonini ham beramiz (lekin tugaganlik donaga bog'liq)
            display_stock = stock_total_units  # dona bo'yicha
        else:
            display_stock_label = "dona"

        # ensure images is always a list of strings
        images = p.get("images")
        if not isinstance(images, list):
            p["images"] = [str(images)] if images else []
        else:
            p["images"] = [str(x) for x in images if x]

        p["effective_price"] = effective_price
        p["flash_active"] = flash_active
        p["seller_price"] = base_price
        p["seller_old_price"] = seller_old_price
        p["effective_old_price"] = effective_old_price
        p["seller_box_price"] = seller_box_price
        p["effective_box_price"] = effective_box_price
        p["seller_effective_price"] = seller_effective_price
        p["seller_effective_box_price"] = seller_effective_box_price
        p["seller_display_price"] = seller_effective_box_price if sale_mode == "box" and seller_effective_box_price is not None else seller_effective_price
        p["display_price"] = display_price
        p["display_old_price"] = display_old_price
        p["piece_price"] = effective_price
        p["sale_mode"] = sale_mode
        p["unit_type"] = unit_type
        p["sale_units"] = units_per_box if sale_mode == "box" and units_per_box > 0 else 1
        p["units_per_box"] = units_per_box
        p["stock_unit"] = "kg" if unit_type == "kg" else "dona"
        p["stock_total_units"] = stock_total_units
        p["display_stock"] = display_stock
        p["display_stock_label"] = display_stock_label
        p["boxes_available"] = boxes_available if units_per_box > 0 else 0
        p["markup_percent"] = markup
        # Tugagan = dona/kg qolmaganda (quti bo'yicha emas!)
        p["out_of_stock"] = stock_total_units <= 0
        p.setdefault("status", "pending")
        p.setdefault("hidden", False)
        p.setdefault("pinned", False)
        return p
    except Exception as e:
        logger.exception("product_out failed for id=%s: %s", (p or {}).get("id"), e)
        # minimal safe payload so admin list never dies
        return {
            "id": (p or {}).get("id"),
            "name": {"uz": "Noma'lum", "ru": "", "en": ""},
            "desc": {"uz": "", "ru": "", "en": ""},
            "images": [],
            "price": 0,
            "effective_price": 0,
            "display_price": 0,
            "status": (p or {}).get("status") or "pending",
            "out_of_stock": True,
            "error": True,
        }



PRODUCT_FILTER = {"status": "approved", "hidden": {"$ne": True}}


def _list_thumb(img_str: str, max_side: int = 240, quality: int = 55) -> str:
    """List/card uchun kichik JPEG thumbnail. Xato bo'lsa original yoki bo'sh."""
    try:
        if not img_str or not isinstance(img_str, str):
            return ""
        s = img_str.strip()
        if not s:
            return ""
        if s.startswith("http://") or s.startswith("https://"):
            return s
        if s.startswith("data:") and len(s) < 12_000:
            return s
        if not s.startswith("data:"):
            return s if len(s) < 12_000 else ""
        import base64 as _b64
        import io as _io
        from PIL import Image as _PILImage
        parts = s.split(",", 1)
        b64part = parts[1] if len(parts) == 2 else s
        try:
            raw = _b64.b64decode(b64part)
        except Exception:
            return ""
        im = _PILImage.open(_io.BytesIO(raw))
        if im.mode != "RGB":
            im = im.convert("RGB")
        # Pillow 9 vs 10 compatibility
        try:
            resample = _PILImage.Resampling.LANCZOS
        except AttributeError:
            resample = getattr(_PILImage, "LANCZOS", _PILImage.BICUBIC)
        im.thumbnail((max_side, max_side), resample)
        buf = _io.BytesIO()
        im.save(buf, format="JPEG", quality=quality, optimize=True)
        out_b64 = _b64.b64encode(buf.getvalue()).decode("ascii")
        return "data:image/jpeg;base64," + out_b64
    except Exception:
        try:
            return s if len(s) < 40_000 else ""
        except Exception:
            return ""


def product_list_out(p):
    """Lightweight product for list/admin grids. Hech qachon raise qilmasin."""
    try:
        out = product_out(p)
        if not isinstance(out, dict):
            out = {}
        imgs = out.get("images") or []
        raw_first = ""
        for im in imgs[:3]:
            try:
                s = str(im) if im is not None else ""
            except Exception:
                s = ""
            if s and len(s.strip()) >= 8:
                raw_first = s.strip()
                break
        if not raw_first and isinstance(p, dict):
            for k in ("image", "preview_image", "main_image", "thumbnail", "photo"):
                v = p.get(k)
                if isinstance(v, str) and len(v.strip()) > 8:
                    raw_first = v.strip()
                    break
        thumb = _list_thumb(raw_first) if raw_first else ""
        out["images"] = [thumb] if thumb else []
        out["image"] = thumb
        out["preview_image"] = thumb
        out.pop("desc", None)
        out.pop("variations", None)
        out.pop("search_text", None)
        out.pop("attributes", None)
        out.pop("specs", None)
        return json_safe(out)
    except Exception as e:
        logger.exception("product_list_out failed: %s", e)
        try:
            pid = (p or {}).get("id") if isinstance(p, dict) else None
        except Exception:
            pid = None
        return {
            "id": pid,
            "name": {"uz": "Mahsulot", "ru": "", "en": ""},
            "images": [],
            "image": "",
            "preview_image": "",
            "price": 0,
            "effective_price": 0,
            "display_price": 0,
            "piece_price": 0,
            "out_of_stock": True,
        }





@api_router.get("/products")
async def list_products(
    search: Optional[str] = None, category_id: Optional[str] = None,
    seller_id: Optional[str] = None, min_price: Optional[float] = None,
    max_price: Optional[float] = None, discount: Optional[bool] = None,
    min_rating: Optional[float] = None, in_stock: Optional[bool] = None,
    sort: Optional[str] = "mix", skip: int = 0, limit: int = Query(20, le=50),
):
    cache_key = None
    try:
        if not search and not seller_id and min_price is None and max_price is None and not discount and min_rating is None and not in_stock:
            cache_key = f"{category_id or ''}|{sort}|{skip}|{limit}"
            now_ts = time.time()
            exp = _PRODUCTS_LIST_CACHE["expires"].get(cache_key, 0)
            if now_ts < exp and cache_key in _PRODUCTS_LIST_CACHE["data"]:
                return _PRODUCTS_LIST_CACHE["data"][cache_key]
    except Exception:
        cache_key = None

    q: Dict[str, Any] = dict(PRODUCT_FILTER)
    if category_id:
        q["$or"] = [{"category_id": category_id}, {"subcategory_id": category_id}]
    if seller_id:
        q["seller_id"] = seller_id
    if min_price is not None:
        q["price"] = {"$gte": min_price}
    if max_price is not None:
        q.setdefault("price", {})["$lte"] = max_price
    if discount:
        q["old_price"] = {"$ne": None, "$gt": 0}
    if min_rating:
        q["rating"] = {"$gte": min_rating}
    if in_stock:
        q["stock"] = {"$gt": 0}
    if search:
        rx = {"$regex": re.escape(search), "$options": "i"}
        q["$and"] = [{"$or": [{"name.uz": rx}, {"name.ru": rx}, {"name.en": rx}, {"desc.uz": rx}]}]
        # non-blocking log — never slow down search response
        asyncio.create_task(db.search_log.insert_one({"id": uid(), "q": search, "at": iso()}))
    sort_map = {"cheap": [("price", 1)], "expensive": [("price", -1)], "new": [("created_at", -1)],
                "popular": [("sold", -1)], "rating": [("rating", -1)], "mix": [("pinned", -1), ("sold", -1)]}
    cursor = db.products.find(q).sort(sort_map.get(sort, sort_map["mix"])).skip(skip).limit(limit)
    raw_items = await cursor.to_list(limit)
    items = [product_list_out(p) for p in raw_items if p]
    # category-name fallback: "telefon" matches category "Telefonlar"
    if search and not items and not skip:
        rx = {"$regex": re.escape(search), "$options": "i"}
        cats = await db.categories.find({"$or": [{"name.uz": rx}, {"name.ru": rx}, {"name.en": rx}]}, {"_id": 0, "id": 1}).to_list(20)
        if cats:
            cat_ids = [c["id"] for c in cats]
            by_cat = await db.products.find({**PRODUCT_FILTER, "$or": [{"category_id": {"$in": cat_ids}}, {"subcategory_id": {"$in": cat_ids}}]}).limit(limit).to_list(limit)
            items = [product_list_out(p) for p in by_cat]
    # lightweight fuzzy (cap 200 docs, early exit)
    if search and not items and not skip and len(search) >= 3:
        needle = search.lower()
        all_names = await db.products.find(PRODUCT_FILTER, {"_id": 0, "id": 1, "name": 1}).limit(200).to_list(200)
        matched_ids = []
        for p in all_names:
            name = p.get("name") or {}
            if not isinstance(name, dict):
                continue
            for lang_name in name.values():
                if not isinstance(lang_name, str):
                    continue
                for word in lang_name.lower().split():
                    if difflib.SequenceMatcher(None, needle, word).ratio() > 0.65:
                        matched_ids.append(p["id"])
                        break
                if p["id"] in matched_ids:
                    break
            if len(matched_ids) >= limit:
                break
        if matched_ids:
            fuzzy = await db.products.find({"id": {"$in": matched_ids[:limit]}}).to_list(limit)
            items = [product_out(p) for p in fuzzy]
    # fuzzy against category names
    if search and not items and not skip and len(search) >= 3:
        needle = search.lower()
        all_cats = await db.categories.find({}, {"_id": 0, "id": 1, "name": 1}).to_list(200)
        fuzzy_cat_ids = []
        for c in all_cats:
            name = c.get("name") or {}
            if not isinstance(name, dict):
                continue
            for lang_name in name.values():
                if not isinstance(lang_name, str):
                    continue
                for word in lang_name.lower().split():
                    if difflib.SequenceMatcher(None, needle, word).ratio() > 0.6:
                        fuzzy_cat_ids.append(c["id"])
                        break
        if fuzzy_cat_ids:
            by_cat = await db.products.find({**PRODUCT_FILTER, "$or": [{"category_id": {"$in": fuzzy_cat_ids}}, {"subcategory_id": {"$in": fuzzy_cat_ids}}]}).limit(limit).to_list(limit)
            items = [product_list_out(p) for p in by_cat]
    total = await db.products.count_documents(q)
    payload = {"items": items, "total": total}
    try:
        if cache_key is not None:
            _PRODUCTS_LIST_CACHE["data"][cache_key] = payload
            _PRODUCTS_LIST_CACHE["expires"][cache_key] = time.time() + _PRODUCTS_LIST_TTL
            if len(_PRODUCTS_LIST_CACHE["data"]) > 40:
                oldest = sorted(_PRODUCTS_LIST_CACHE["expires"].items(), key=lambda x: x[1])[:20]
                for k, _ in oldest:
                    _PRODUCTS_LIST_CACHE["data"].pop(k, None)
                    _PRODUCTS_LIST_CACHE["expires"].pop(k, None)
    except Exception:
        pass
    return payload


@api_router.get("/products/flash-sale")
async def flash_sale():
    try:
        items = await db.products.find({**PRODUCT_FILTER, "flash_sale.ends_at": {"$gt": iso()}}).to_list(20)
        return [product_list_out(p) for p in items if p]
    except Exception as e:
        logger.exception("flash_sale failed: %s", e)
        return []


@api_router.get("/products/recommendations")
async def recommendations(user=Depends(get_user_optional)):
    try:
        cat_ids = []
        if user:
            views = await db.views.find({"user_id": user["id"]}).sort("at", -1).to_list(20)
            cat_ids = list({v["category_id"] for v in views if v.get("category_id")})
        q = dict(PRODUCT_FILTER)
        if cat_ids:
            q["category_id"] = {"$in": cat_ids}
        items = await db.products.find(q).sort("sold", -1).limit(10).to_list(10)
        if len(items) < 6:
            more = await db.products.find(PRODUCT_FILTER).sort("views", -1).limit(10).to_list(10)
            seen = {p["id"] for p in items}
            items += [p for p in more if p["id"] not in seen]
        return [product_list_out(p) for p in items[:10] if p]
    except Exception as e:
        logger.exception("recommendations failed: %s", e)
        return []


@api_router.get("/products/{pid}")
async def get_product(pid: str, user=Depends(get_user_optional)):
    p = await db.products.find_one({"id": pid})
    if not p:
        raise HTTPException(404, "Mahsulot topilmadi")
    await db.products.update_one({"id": pid}, {"$inc": {"views": 1}})
    if user:
        await db.views.insert_one({"id": uid(), "user_id": user["id"], "product_id": pid, "category_id": p.get("category_id"), "at": iso()})
    seller = await db.users.find_one({"id": p["seller_id"]}, {"_id": 0})
    out = product_out(p)
    if seller:
        count = await db.products.count_documents({"seller_id": seller["id"], **PRODUCT_FILTER})
        out["seller"] = {"id": seller["id"], "shop_name": seller.get("seller_info", {}).get("shop_name", "Do'kon"),
                        "rating": seller.get("seller_info", {}).get("rating", 5.0), "products_count": count}
    return out


@api_router.get("/products/{pid}/similar")
async def similar(pid: str):
    p = await db.products.find_one({"id": pid})
    if not p:
        return []
    items = await db.products.find({**PRODUCT_FILTER, "category_id": p.get("category_id"), "id": {"$ne": pid}}).limit(8).to_list(8)
    return [product_out(x) for x in items]


@api_router.get("/products/{pid}/reviews")
async def product_reviews(pid: str):
    return await db.reviews.find({"product_id": pid, "hidden": {"$ne": True}}, {"_id": 0}).sort("created_at", -1).to_list(100)


@api_router.post("/products/{pid}/reviews")
async def add_review(pid: str, req: ReviewReq, user=Depends(get_user)):
    bought = await db.orders.find_one({"client_id": user["id"], "status": "delivered", "items.product_id": pid})
    if not bought:
        raise HTTPException(403, "Faqat mahsulotni xarid qilganlar sharh yoza oladi")
    if await db.reviews.find_one({"product_id": pid, "client_id": user["id"]}):
        raise HTTPException(400, "Siz allaqachon sharh yozgansiz")
    rev = {"id": uid(), "product_id": pid, "client_id": user["id"],
           "client_name": f"{user['first_name']} {user.get('last_name', '')}".strip(),
           "rating": max(1, min(5, req.rating)), "text": req.text, "verified": True, "created_at": iso()}
    await db.reviews.insert_one(dict(rev))
    revs = await db.reviews.find({"product_id": pid, "hidden": {"$ne": True}}).to_list(1000)
    avg = round(sum(r["rating"] for r in revs) / len(revs), 1)
    await db.products.update_one({"id": pid}, {"$set": {"rating": avg, "reviews_count": len(revs)}})
    return {k: v for k, v in rev.items() if k != "_id"}


@api_router.get("/search/suggest")
async def suggest(q: str = ""):
    if not q:
        pop = await db.search_log.aggregate([{"$group": {"_id": "$q", "c": {"$sum": 1}}}, {"$sort": {"c": -1}}, {"$limit": 8}]).to_list(8)
        return {"suggestions": [p["_id"] for p in pop]}
    rx = {"$regex": re.escape(q), "$options": "i"}
    prods = await db.products.find({**PRODUCT_FILTER, "$or": [{"name.uz": rx}, {"name.ru": rx}, {"name.en": rx}]}, {"_id": 0, "name": 1}).limit(6).to_list(6)
    return {"suggestions": list({(p.get("name") or {}).get("uz") or "" for p in prods if (p.get("name") or {}).get("uz")})}

"""
Rasm orqali qidiruv — backend qo'shimchasi
================================================
Quyidagilarni server.py (asosiy FastAPI fayl) ga qo'shing.

1) Importlarga qo'shing (yuqoriga):
   from fastapi import File, UploadFile
   import io
   import math
   import struct

2) /search/suggest dan KEYIN quyidagi kodni joylashtiring.

Pillow ixtiyoriy: `pip install pillow` (tavsiya etiladi).
Pillow bo'lmasa ham endpoint ishlaydi — soddaroq RGB o'rtacha bilan.
"""

# ===== PASTE FROM HERE (after /search/suggest) =====

# ---------- Image search (visual similarity) ----------
def _avg_rgb_from_jpeg_rough(data: bytes):
    """Very rough fallback without Pillow: sample bytes as pseudo RGB."""
    if not data or len(data) < 64:
        return (128.0, 128.0, 128.0)
    # skip header-ish region, sample every Nth byte
    sample = data[min(100, len(data) // 10) :]
    step = max(1, len(sample) // 3000)
    rs, gs, bs, n = 0, 0, 0, 0
    for i in range(0, len(sample) - 2, step * 3):
        rs += sample[i]
        gs += sample[i + 1]
        bs += sample[i + 2]
        n += 1
        if n >= 1000:
            break
    if n == 0:
        return (128.0, 128.0, 128.0)
    return (rs / n, gs / n, bs / n)




# ---- Visual search: tez + ishonchli ----
_IMAGE_FEAT_CACHE: Dict[str, Any] = {}
_IMAGE_FEAT_CACHE_MAX = 2000


def _image_signature(data: bytes) -> str:
    import hashlib
    if not data:
        return ""
    return f"{len(data)}:{hashlib.md5(data).hexdigest()}"


def _image_features_from_bytes(data: bytes):
    """Rang + 8x8 grid + histogram — o'xshashlik uchun yaxshiroq."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = img.size
        if w < 1 or h < 1:
            raise ValueError("empty")
        try:
            resample = Image.Resampling.BILINEAR
        except AttributeError:
            resample = Image.BILINEAR
        # global average on small thumb
        small = img.copy()
        small.thumbnail((32, 32))
        pixels = list(small.getdata())
        n = len(pixels) or 1
        ar = sum(p[0] for p in pixels) / n
        ag = sum(p[1] for p in pixels) / n
        ab = sum(p[2] for p in pixels) / n
        # 8x8 spatial grid (stronger than 4x4)
        grid = img.resize((8, 8), resample)
        grid_feats = []
        for r, g, b in grid.getdata():
            grid_feats.extend([float(r) / 255.0, float(g) / 255.0, float(b) / 255.0])
        # coarse RGB histogram (4 bins each)
        hist = [0.0] * 12
        for r, g, b in pixels:
            hist[min(3, r * 4 // 256)] += 1
            hist[4 + min(3, g * 4 // 256)] += 1
            hist[8 + min(3, b * 4 // 256)] += 1
        inv = 1.0 / float(n)
        hist = [x * inv for x in hist]
        aspect = float(w) / float(h) if h else 1.0
        return [ar / 255.0, ag / 255.0, ab / 255.0] + grid_feats + hist + [aspect]
    except Exception:
        try:
            ar, ag, ab = _avg_rgb_from_jpeg_rough(data)
        except Exception:
            ar = ag = ab = 128.0
        return [ar / 255.0, ag / 255.0, ab / 255.0] + [0.5] * (64 * 3) + [0.25] * 12 + [1.0]


def _feature_distance(a, b) -> float:
    """Kichikroq = o'xshashroq. Turli uzunlikdagi eski cache ham ishlaydi."""
    if not a or not b:
        return 1e9
    n = min(len(a), len(b))
    if n < 3:
        return 1e9
    s = 0.0
    for i in range(n):
        try:
            d = float(a[i]) - float(b[i])
        except Exception:
            continue
        # grid/hist o'rtacha, global rang biroz og'irroq
        w = 2.0 if i < 3 else 1.0
        s += w * d * d
    return math.sqrt(s / n)


def _decode_data_uri(s: str) -> Optional[bytes]:
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if s.startswith("data:") and "," in s:
        try:
            import base64
            return base64.b64decode(s.split(",", 1)[1])
        except Exception:
            return None
    return None


def _first_image_ref(p: dict) -> Optional[str]:
    if not isinstance(p, dict):
        return None
    imgs = p.get("images")
    if isinstance(imgs, list):
        for x in imgs:
            if isinstance(x, str) and len(x.strip()) > 10:
                return x.strip()
            if isinstance(x, dict):
                for k in ("url", "image", "src", "uri", "path"):
                    if x.get(k) and str(x[k]).strip():
                        return str(x[k]).strip()
    for k in ("image", "preview_image", "main_image", "thumbnail", "photo"):
        v = p.get(k)
        if isinstance(v, str) and len(v.strip()) > 10:
            return v.strip()
    return None


def _load_image_bytes(image_ref: str) -> Optional[bytes]:
    """data-URI yoki (sinxron emas) HTTP — HTTP ni caller async qiladi."""
    if not image_ref:
        return None
    raw = _decode_data_uri(image_ref)
    if raw:
        return raw
    return None


def _visual_search_out(p: dict):
    try:
        return product_list_out(p)
    except Exception:
        try:
            return json_safe(product_out(p))
        except Exception:
            return {"id": (p or {}).get("id"), "name": {"uz": "Mahsulot"}, "images": [], "out_of_stock": True}


def _shrink_bytes(data: bytes, max_side: int = 256) -> bytes:
    if not data or len(data) < 50_000:
        return data
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("RGB")
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=70)
        return buf.getvalue()
    except Exception:
        return data[:800_000]


def _feat_from_product(p: dict):
    """(feat, sig) yoki (None, '')."""
    pid = str(p.get("id") or "")
    stored = p.get("image_feat")
    stored_sig = str(p.get("image_sig") or "")
    if isinstance(stored, list) and len(stored) >= 10:
        _IMAGE_FEAT_CACHE[pid] = {"feat": stored, "sig": stored_sig}
        return stored, stored_sig
    if pid and pid in _IMAGE_FEAT_CACHE:
        c = _IMAGE_FEAT_CACHE[pid]
        return c.get("feat"), c.get("sig") or ""

    ref = _first_image_ref(p)
    if not ref:
        return None, ""
    raw = _load_image_bytes(ref)
    if not raw:
        return None, ""  # HTTP — keyinroq async
    raw = _shrink_bytes(raw)
    sig = _image_signature(raw)
    feat = _image_features_from_bytes(raw)
    if pid:
        _IMAGE_FEAT_CACHE[pid] = {"feat": feat, "sig": sig}
        if len(_IMAGE_FEAT_CACHE) > _IMAGE_FEAT_CACHE_MAX:
            for k in list(_IMAGE_FEAT_CACHE.keys())[: _IMAGE_FEAT_CACHE_MAX // 2]:
                _IMAGE_FEAT_CACHE.pop(k, None)
    return feat, sig


@api_router.post("/search/by-image")
async def search_by_image(image: UploadFile = File(...)):
    """
    Eng o'xshash mahsulot BIRINCHI.
    1) MD5 aniq moslik
    2) image_feat masofa bo'yicha sort (kichik → birinchi)
    """
    t0 = time.time()
    TIME_BUDGET = 3.5

    try:
        data = await image.read()
    except Exception:
        raise HTTPException(400, "Rasm o'qilmadi")
    if not data or len(data) < 24:
        raise HTTPException(400, "Rasm bo'sh yoki juda kichik")

    data = _shrink_bytes(data, max_side=512)
    query_sig = _image_signature(data)
    query_feat = _image_features_from_bytes(data)

    # 1) Avval feature allaqachon bor mahsulotlar (tez, butun katalog)
    precomputed = []
    try:
        precomputed = await (
            db.products.find(
                {**PRODUCT_FILTER, "image_feat": {"$exists": True, "$type": "array"}},
            )
            .limit(500)
            .max_time_ms(4000)
            .to_list(500)
        )
    except Exception as e:
        logger.warning("search precomputed: %s", e)

    exact_hits = []
    scored = []  # (dist, product)

    for p in precomputed:
        if time.time() - t0 > TIME_BUDGET:
            break
        try:
            feat = p.get("image_feat")
            sig = str(p.get("image_sig") or "")
            if not isinstance(feat, list) or len(feat) < 3:
                continue
            if sig and query_sig and sig == query_sig:
                exact_hits.append(p)
                continue
            dist = _feature_distance(query_feat, feat)
            scored.append((dist, p))
        except Exception:
            continue

    # 2) Feature yo'qlar — eng mashhurlaridan hisobla (qolgan vaqt)
    need_compute = []
    try:
        need_compute = await (
            db.products.find(
                {
                    **PRODUCT_FILTER,
                    "$or": [
                        {"image_feat": {"$exists": False}},
                        {"image_feat": None},
                    ],
                }
            )
            .sort([("sold", -1), ("pinned", -1)])
            .limit(80)
            .max_time_ms(4000)
            .to_list(80)
        )
    except Exception as e:
        logger.warning("search need_compute: %s", e)

    to_persist = []
    for p in need_compute:
        if time.time() - t0 > TIME_BUDGET:
            break
        try:
            feat, sig = _feat_from_product(p)
            if not feat:
                ref = _first_image_ref(p)
                if ref and (ref.startswith("http://") or ref.startswith("https://")):
                    # HTTP keyinroq
                    continue
                continue
            if sig and query_sig and sig == query_sig:
                exact_hits.append(p)
                continue
            dist = _feature_distance(query_feat, feat)
            scored.append((dist, p))
            if p.get("id"):
                to_persist.append((p["id"], feat, sig))
        except Exception:
            continue

    # HTTP rasmlar (qisqa)
    remain = TIME_BUDGET - (time.time() - t0)
    http_need = []
    for p in need_compute:
        if p.get("id") and any(x[1].get("id") == p.get("id") for x in scored):
            continue
        ref = _first_image_ref(p)
        if ref and (ref.startswith("http://") or ref.startswith("https://")):
            http_need.append((p, ref))
    if http_need and remain > 0.5:
        import httpx
        sem = asyncio.Semaphore(8)

        async def fetch_one(p, ref):
            try:
                async with sem:
                    async with httpx.AsyncClient(timeout=min(1.0, remain), follow_redirects=True) as c:
                        r = await c.get(ref)
                        if r.status_code != 200 or not r.content:
                            return None
                        raw = _shrink_bytes(r.content)
                        feat = _image_features_from_bytes(raw)
                        sig = _image_signature(raw)
                        pid = str(p.get("id") or "")
                        if pid:
                            _IMAGE_FEAT_CACHE[pid] = {"feat": feat, "sig": sig}
                        return (p, feat, sig)
            except Exception:
                return None

        http_results = await asyncio.gather(
            *[fetch_one(p, ref) for p, ref in http_need[:25]],
            return_exceptions=True,
        )
        for r in http_results:
            if not isinstance(r, tuple):
                continue
            p, feat, sig = r
            if sig and query_sig and sig == query_sig:
                exact_hits.append(p)
            else:
                scored.append((_feature_distance(query_feat, feat), p))
                if p.get("id"):
                    to_persist.append((p["id"], feat, sig))

    if to_persist:
        asyncio.create_task(_persist_image_feats(to_persist[:60]))

    # Aniq moslik — eng yuqori prioritet
    if exact_hits:
        # unique by id
        seen = set()
        uniq = []
        for p in exact_hits:
            pid = p.get("id")
            if pid in seen:
                continue
            seen.add(pid)
            uniq.append(p)
        return {
            "items": [_visual_search_out(p) for p in uniq[:24]],
            "exact": True,
            "took_ms": int((time.time() - t0) * 1000),
        }

    if scored:
        # eng yaqin BIRINCHI (dist o'sish tartibida)
        scored.sort(key=lambda x: (x[0], -int((x[1] or {}).get("sold") or 0)))
        seen = set()
        top = []
        for dist, p in scored:
            pid = p.get("id")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            top.append(p)
            if len(top) >= 24:
                break
        return {
            "items": [_visual_search_out(p) for p in top],
            "exact": False,
            "took_ms": int((time.time() - t0) * 1000),
            "best_dist": round(float(scored[0][0]), 4) if scored else None,
        }

    # Fallback — faqat hech narsa score bo'lmasa
    fallback = precomputed or need_compute or []
    popular = sorted(
        fallback,
        key=lambda x: (-int(x.get("sold") or 0), -float(x.get("rating") or 0)),
    )[:20]
    return {
        "items": [_visual_search_out(p) for p in popular],
        "exact": False,
        "fallback": True,
        "took_ms": int((time.time() - t0) * 1000),
    }


async def _persist_image_feats(pairs):
    try:
        for pid, feat, sig in pairs:
            await db.products.update_one(
                {"id": pid},
                {"$set": {"image_feat": feat, "image_sig": sig}},
            )
    except Exception as e:
        logger.warning("persist image_feat failed: %s", e)


# ---------- Favorites ----------
@api_router.post("/favorites/{pid}")
async def toggle_favorite(pid: str, user=Depends(get_user)):
    favs = user.get("favorites", [])
    if pid in favs:
        await db.users.update_one({"id": user["id"]}, {"$pull": {"favorites": pid}})
        return {"favorited": False}
    await db.users.update_one({"id": user["id"]}, {"$push": {"favorites": pid}})
    return {"favorited": True}


@api_router.get("/favorites")
async def list_favorites(user=Depends(get_user)):
    items = await db.products.find({"id": {"$in": user.get("favorites", [])}}).to_list(100)
    return [product_out(p) for p in items]


# ---------- Promo ----------
@api_router.post("/promo/validate")
async def validate_promo(req: PromoReq, user=Depends(get_user)):
    promo = await db.promocodes.find_one({"code": req.code.upper(), "active": True}, {"_id": 0})
    if not promo:
        raise HTTPException(404, "Promokod topilmadi")
    if promo.get("expires_at") and promo["expires_at"] < iso():
        raise HTTPException(400, "Promokod muddati tugagan")
    if promo.get("used", 0) >= promo.get("limit", 0):
        raise HTTPException(400, "Promokod limiti tugagan")
    if req.subtotal < promo.get("min_cart", 0):
        raise HTTPException(400, f"Minimal savat summasi: {int(promo['min_cart']):,} so'm")
    discount = req.subtotal * promo["value"] / 100 if promo["type"] == "percent" else promo["value"]
    return {"code": promo["code"], "discount": min(discount, req.subtotal), "type": promo["type"], "value": promo["value"]}


# ---------- Orders ----------
STATUS_FLOW = ["new", "confirmed", "packing", "courier", "delivered"]


@api_router.post("/orders")
async def create_order(req: OrderReq, user=Depends(get_user)):
    if not req.items:
        raise HTTPException(400, "Savat bo'sh")
    settings = await db.settings.find_one({"id": "main"}) or {}
    delivery_fee = settings.get("delivery_fee", 15000) if req.delivery_method == "courier" else 0
    default_eta_days = int(settings.get("default_delivery_eta_days") or 0)
    by_seller: Dict[str, list] = {}
    subtotal_all = 0.0
    for it in req.items:
        p = await db.products.find_one({"id": it.product_id})
        if not p or p.get("status") != "approved":
            raise HTTPException(400, "Mahsulot mavjud emas")
        out = product_out(p)
        upb = int(out.get("units_per_box") or 0)
        unit_type = out.get("unit_type") or "piece"
        variation = (it.variation or "").lower()
        # Savatdan kelgan tanlov: "quti..." → box, aks holda dona/kg
        if unit_type == "kg":
            sale_mode = "kg"
            sale_units = 1
            price = float(out.get("piece_price") or out.get("effective_price") or p.get("price") or 0)
            base_price = float(out.get("seller_effective_price") or p.get("price") or 0)
        elif upb > 0 and ("quti" in variation):
            sale_mode = "box"
            sale_units = upb
            price = float(out.get("effective_box_price") or out.get("box_price") or (out.get("piece_price") or 0) * upb)
            base_price = float(out.get("seller_effective_box_price") or out.get("seller_box_price") or (out.get("seller_price") or p.get("price") or 0) * upb)
        else:
            # dona (yoki quti sozlanmagan)
            sale_mode = "piece"
            sale_units = 1
            price = float(out.get("piece_price") or out.get("effective_price") or p.get("price") or 0)
            base_price = float(out.get("seller_effective_price") or out.get("seller_price") or p.get("price") or 0)

        requested_units = int(it.qty) * sale_units
        stock_left = int(p.get("stock", 0) or 0)
        if stock_left < requested_units:
            label = "kg" if sale_mode == "kg" else "dona"
            raise HTTPException(
                400,
                f"{p['name']['uz']}: omborda yetarli emas ({stock_left} {label})",
            )
        subtotal_all += price * it.qty
        by_seller.setdefault(p["seller_id"], []).append({
            "item_id": uid(), "product_id": p["id"], "name": p["name"], "image": (p.get("images") or [""])[0],
            "price": price, "base_price": base_price, "qty": it.qty, "variation": it.variation,
            "sale_mode": sale_mode, "unit_type": unit_type, "units_per_box": upb,
            "ordered_units": requested_units,
            "delivery_status": "pending"})
    discount_all = 0.0
    promo = None
    if req.promo_code:
        promo = await db.promocodes.find_one({"code": req.promo_code.upper(), "active": True})
        if promo and (not promo.get("expires_at") or promo["expires_at"] > iso()) and promo.get("used", 0) < promo.get("limit", 0) and subtotal_all >= promo.get("min_cart", 0):
            discount_all = subtotal_all * promo["value"] / 100 if promo["type"] == "percent" else promo["value"]
            await db.promocodes.update_one({"id": promo["id"]}, {"$inc": {"used": 1}})
        else:
            promo = None
    group_id = uid()
    counter = await db.counters.find_one_and_update({"id": "order"}, {"$inc": {"seq": 1}}, upsert=True, return_document=True)
    base_num = 1000 + counter["seq"]
    addr_lat, addr_lng = req.address_lat, req.address_lng
    if addr_lat is None or addr_lng is None:
        geo = await geocode_best_effort(req.address_text)
        if geo:
            addr_lat, addr_lng = geo
    orders = []
    idx = 0
    for seller_id, items in by_seller.items():
        idx += 1
        seller_user = await db.users.find_one({"id": seller_id}, {"_id": 0})
        slat, slng = shop_location(seller_user)
        sub = sum(i["price"] * i["qty"] for i in items)
        seller_sub = sum(i["base_price"] * i["qty"] for i in items)
        share = sub / subtotal_all if subtotal_all else 0
        disc = round(discount_all * share)
        dfee = delivery_fee if idx == 1 else 0
        order = {
            "id": uid(), "number": f"#{base_num}-{idx}" if len(by_seller) > 1 else f"#{base_num}",
            "group_id": group_id, "client_id": user["id"],
            "client_name": f"{user['first_name']} {user.get('last_name', '')}".strip(), "client_phone": user["phone"],
            "seller_id": seller_id, "items": items, "subtotal": sub, "seller_subtotal": seller_sub, "delivery_fee": dfee,
            "discount": disc, "total": sub + dfee - disc, "promo_code": promo["code"] if promo else None,
            "status": "new", "address_text": req.address_text, "address_lat": addr_lat, "address_lng": addr_lng,
            "delivery_location": {"lat": addr_lat, "lng": addr_lng} if addr_lat is not None and addr_lng is not None else None,
            "pickup_location": {"lat": slat, "lng": slng},
            "delivery_method": req.delivery_method,
            "delivery_eta_days": default_eta_days if req.delivery_method == "courier" else 0,
            "payment_method": req.payment_method, "comment": req.comment, "courier_id": None,
            "client_note": (user.get("profile_note") or "").strip() or None,
            "status_history": [{"status": "new", "at": iso()}], "created_at": iso(),
        }
        await db.orders.insert_one(dict(order))
        orders.append({k: v for k, v in order.items() if k != "_id"})
        for it in items:
            ordered_units = int(it.get("ordered_units") or it.get("qty", 0) or 0)
            newp = await db.products.find_one_and_update({"id": it["product_id"]}, {"$inc": {"stock": -ordered_units, "sold": ordered_units}}, return_document=True)
            if newp and newp.get("stock", 0) <= 0:
                await notify(seller_id, "Mahsulot tugadi", f"{newp['name']['uz']} ombordagi qoldig'i tugadi")
        await notify(seller_id, "Yangi buyurtma", f"{order['number']} — {int(order['total']):,} so'm")
    admins = await db.users.find({"role": "admin"}).to_list(10)
    for a in admins:
        await notify(a["id"], "Yangi buyurtma", f"#{base_num} — {user['first_name']}, {int(subtotal_all + delivery_fee - discount_all):,} so'm")
    _MY_ORDERS_CACHE.pop(user["id"], None)
    return {"orders": orders, "group_id": group_id, "number": f"#{base_num}"}


# Short per-user cache for /orders/my — cuts repeat Atlas latency (often 200–500ms)
_MY_ORDERS_CACHE: Dict[str, Any] = {}  # user_id -> {"data": list, "expires_at": float}


@api_router.get("/orders/my")
async def my_orders(user=Depends(get_user)):
    uid_key = user["id"]
    now_ts = time.time()
    cached = _MY_ORDERS_CACHE.get(uid_key)
    if cached and now_ts < cached["expires_at"]:
        return cached["data"]

    try:
        proj = {
            "_id": 0,
            "id": 1,
            "number": 1,
            "group_id": 1,
            "status": 1,
            "total": 1,
            "subtotal": 1,
            "delivery_fee": 1,
            "discount": 1,
            "created_at": 1,
            "items": 1,
            "delivery_method": 1,
            "payment_method": 1,
            "address_text": 1,
            "status_history": 1,
            "courier_id": 1,
            "client_name": 1,
            "comment": 1,
            "promo_code": 1,
            "seller_id": 1,
            "returned_items_count": 1,
            "delivery_eta_days": 1,
        }
        raw = await db.orders.find(
            {"client_id": uid_key},
            proj,
        ).sort("created_at", -1).max_time_ms(8000).to_list(100)

        # Ro'yxat uchun rasm: yo'q yoki juda katta base64 → product dan olish
        need_pids = set()
        for o in raw:
            for it in (o.get("items") or [])[:6]:
                img = it.get("image") if isinstance(it, dict) else ""
                if not isinstance(img, str):
                    img = ""
                bad = (not img) or len(img) < 8 or (img.startswith("data:") and len(img) > 6000)
                if bad and it.get("product_id"):
                    need_pids.add(it["product_id"])

        prod_img: Dict[str, str] = {}
        if need_pids:
            prods = await db.products.find(
                {"id": {"$in": list(need_pids)}},
                {"_id": 0, "id": 1, "images": 1, "image": 1, "preview_image": 1},
            ).to_list(len(need_pids))
            for p in prods:
                try:
                    out = product_list_out(dict(p))
                    thumb = out.get("image") or out.get("preview_image") or ""
                    if not thumb:
                        imgs = out.get("images") or []
                        if imgs and isinstance(imgs[0], str):
                            thumb = imgs[0]
                    if thumb:
                        prod_img[p["id"]] = thumb
                except Exception:
                    # fallback raw first image
                    imgs = p.get("images") or []
                    if isinstance(imgs, list) and imgs and isinstance(imgs[0], str):
                        prod_img[p["id"]] = imgs[0][:50000] if imgs[0].startswith("data:") else imgs[0]
                    elif isinstance(p.get("image"), str):
                        prod_img[p["id"]] = p["image"]

        for o in raw:
            items = o.get("items") or []
            slim_items = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                row = {
                    "product_id": it.get("product_id"),
                    "name": it.get("name"),
                    "qty": it.get("qty"),
                    "price": it.get("price"),
                    "variation": it.get("variation"),
                    "delivery_status": it.get("delivery_status"),
                    "image": it.get("image") or "",
                }
                img = row["image"] if isinstance(row["image"], str) else ""
                if (not img) or len(img) < 8 or (img.startswith("data:") and len(img) > 6000):
                    pid = it.get("product_id")
                    if pid and pid in prod_img:
                        row["image"] = prod_img[pid]
                    else:
                        row["image"] = ""
                # list UI uchun juda katta base64 ni qisqartirmasdan qoldiramiz agar product_list_out bergan bo'lsa
                slim_items.append(row)
            o["items"] = slim_items

        data = [json_safe(o) for o in raw]
        _MY_ORDERS_CACHE[uid_key] = {"data": data, "expires_at": now_ts + 15.0}
        if len(_MY_ORDERS_CACHE) > 200:
            for k, _ in sorted(_MY_ORDERS_CACHE.items(), key=lambda x: x[1]["expires_at"])[:50]:
                _MY_ORDERS_CACHE.pop(k, None)
        return data
    except Exception as e:
        logger.exception("my_orders failed for user %s: %s", uid_key, e)
        if cached:
            return cached["data"]
        return []


@api_router.get("/orders/{oid}")
async def get_order(oid: str, user=Depends(get_user)):
    o = await db.orders.find_one({"id": oid}, {"_id": 0})
    if not o:
        raise HTTPException(404, "Buyurtma topilmadi")
    if o.get("courier_id"):
        c = await db.users.find_one({"id": o["courier_id"]}, {"_id": 0})
        if c:
            o["courier"] = {"name": c.get("first_name"), "phone": c.get("phone")}
    return json_safe(o)


@api_router.post("/orders/{oid}/cancel")
async def cancel_order(oid: str, user=Depends(get_user)):
    o = await db.orders.find_one({"id": oid, "client_id": user["id"]})
    if not o or o["status"] not in ("new", "confirmed"):
        raise HTTPException(400, "Bu buyurtmani bekor qilib bo'lmaydi")
    await set_order_status(o, "cancelled", "Mijoz bekor qildi")
    return {"ok": True}


def parse_iso_dt(value: Optional[str]):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def status_at(order: dict, target: str):
    for h in reversed(order.get("status_history") or []):
        if h.get("status") == target and h.get("at"):
            return h["at"]
    return None


def item_name(item: dict):
    name = item.get("name")
    if isinstance(name, dict):
        return name.get("uz") or name.get("ru") or name.get("en") or item.get("product_id") or "Mahsulot"
    return name or item.get("product_id") or "Mahsulot"


def item_line_total(item: dict) -> float:
    """Mijoz to'laydigan qator summasi (ortiqcha kg bilan)."""
    try:
        qty = float(item.get("qty", 0) or 0)
    except (TypeError, ValueError):
        qty = 0.0
    try:
        price = float(item.get("price", 0) or 0)
    except (TypeError, ValueError):
        price = 0.0
    try:
        extra = float(item.get("extra_client_price") or 0)
    except (TypeError, ValueError):
        extra = 0.0
    return price * qty + extra


def item_line_base_total(item: dict) -> float:
    """Sotuvchi ulushi (ortiqcha kg sof narxi bilan)."""
    try:
        qty = float(item.get("qty", 0) or 0)
    except (TypeError, ValueError):
        qty = 0.0
    try:
        base = float(item.get("base_price", item.get("price", 0)) or 0)
    except (TypeError, ValueError):
        base = 0.0
    try:
        extra = float(item.get("extra_seller_price") or 0)
    except (TypeError, ValueError):
        extra = 0.0
    return base * qty + extra


def order_reset_units(item: dict) -> int:
    ordered_units = item.get("ordered_units")
    if ordered_units is not None:
        try:
            return int(ordered_units)
        except Exception:
            pass
    qty = int(item.get("qty", 0) or 0)
    units_per_box = int(item.get("units_per_box", 0) or 0)
    sale_mode = item.get("sale_mode") or ("box" if units_per_box > 0 else "piece")
    sale_units = units_per_box if sale_mode == "box" and units_per_box > 0 else 1
    return qty * sale_units


def courier_cancel_deadline(order: dict):
    courier_at = status_at(order, "courier")
    dt = parse_iso_dt(courier_at)
    if not dt:
        return None
    return dt + timedelta(hours=1)


def can_courier_cancel(order: dict) -> bool:
    if order.get("status") != "courier" or not order.get("courier_id"):
        return False
    deadline = courier_cancel_deadline(order)
    if deadline is None:
        # status_history da courier vaqti yo'q bo'lsa ham 1 soat beramiz (created_at dan)
        created = parse_iso_dt(order.get("created_at"))
        if created:
            deadline = created + timedelta(hours=1)
        else:
            return True
    return now() <= deadline


def admin_dashboard_cutoff(settings: Optional[dict] = None):
    s = settings if settings is not None else None
    reset_at = (s or {}).get("dashboard_money_reset_at") if s is not None else None
    if not reset_at:
        reset_at = (s or {}).get("dashboard_stats_reset_at") if s is not None else None
    dt = parse_iso_dt(reset_at)
    return dt



async def _product_first_image(p: dict) -> str:
    if not p:
        return ""
    imgs = p.get("images")
    if isinstance(imgs, list) and imgs:
        x = imgs[0]
        if isinstance(x, str) and x.strip():
            return x.strip()
        if isinstance(x, dict):
            for k in ("url", "image", "src", "uri"):
                if x.get(k):
                    return str(x[k]).strip()
    for k in ("image", "preview_image", "main_image"):
        v = p.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


async def build_replacement_candidates_for_item(item: dict, seller_id: str):
    """Boshqa sotuvchilardan o'xshash (nom + kategoriya) mahsulotlar — rasm bilan."""
    original_product = await db.products.find_one(
        {"id": item.get("product_id")},
        {"_id": 0, "category_id": 1, "name": 1, "images": 1},
    )
    category_id = (original_product or {}).get("category_id")
    query = {
        "status": "approved",
        "hidden": {"$ne": True},
        "stock": {"$gt": 0},
        "seller_id": {"$ne": seller_id},
        "id": {"$ne": item.get("product_id")},
    }
    if category_id:
        query["category_id"] = category_id
    products = await db.products.find(query, {"_id": 0}).limit(50).to_list(50)
    # kategoriya bo'sh bo'lsa — umumiy qidiruv
    if not products:
        products = await db.products.find(
            {
                "status": "approved",
                "hidden": {"$ne": True},
                "stock": {"$gt": 0},
                "seller_id": {"$ne": seller_id},
                "id": {"$ne": item.get("product_id")},
            },
            {"_id": 0},
        ).sort([("sold", -1)]).limit(30).to_list(30)
    if not products:
        return []
    original_name = item_name(item).lower()
    seller_ids = list({p.get("seller_id") for p in products if p.get("seller_id")})
    sellers = await db.users.find(
        {"id": {"$in": seller_ids}},
        {"_id": 0, "id": 1, "first_name": 1, "seller_info": 1},
    ).to_list(len(seller_ids) or 1)
    seller_map = {s["id"]: s for s in sellers}
    ranked = []
    for p in products:
        try:
            out = product_out(p)
            name_uz = ""
            nm = p.get("name")
            if isinstance(nm, dict):
                name_uz = str(nm.get("uz") or nm.get("ru") or nm.get("en") or "")
            else:
                name_uz = str(nm or "")
            similarity = difflib.SequenceMatcher(None, original_name, name_uz.lower()).ratio()
            seller = seller_map.get(p.get("seller_id"))
            img = await _product_first_image(p)
            # list_out ba'zan rasmni qisqartiradi — shu yerda to'liq birinchi rasm
            ranked.append({
                "id": p["id"],
                "product_id": p["id"],
                "name": p.get("name"),
                "image": img,
                "price": float(out.get("display_price", out.get("effective_price", p.get("price", 0))) or 0),
                "seller_price": float(out.get("seller_display_price", out.get("seller_effective_price", p.get("price", 0))) or 0),
                "stock": int(out.get("stock_total_units", out.get("display_stock", p.get("stock", 0))) or 0),
                "sale_mode": out.get("sale_mode", "piece"),
                "units_per_box": int(out.get("units_per_box", 0) or 0),
                "seller_id": p.get("seller_id"),
                "seller_name": (
                    ((seller.get("seller_info") or {}).get("shop_name") or seller.get("first_name") or "Do'kon")
                    if seller else "Do'kon"
                ),
                "score": similarity,
            })
        except Exception:
            continue
    ranked.sort(key=lambda x: (-x["score"], x["price"]))
    return ranked[:10]


async def _enrich_order_item_image(item: dict) -> dict:
    """Rad etilgan itemda rasm yo'q bo'lsa — product dan to'ldirish."""
    it = dict(item or {})
    img = it.get("image")
    if isinstance(img, str) and len(img.strip()) > 10:
        return it
    pid = it.get("product_id")
    if not pid:
        return it
    prod = await db.products.find_one({"id": pid}, {"_id": 0, "images": 1, "image": 1, "preview_image": 1, "name": 1})
    if prod:
        it["image"] = await _product_first_image(prod)
        if not it.get("name") and prod.get("name"):
            it["name"] = prod.get("name")
    return it


async def build_rejected_order_payload(order: dict, only_rejected_items: bool = False):
    payload = {k: v for k, v in order.items() if k != "_id"}
    seller = await db.users.find_one({"id": order.get("seller_id")}, {"_id": 0})
    payload["rejected_seller"] = {
        "id": seller.get("id") if seller else order.get("seller_id"),
        "name": (
            (seller.get("seller_info") or {}).get(
                "shop_name",
                f"{seller.get('first_name', '')} {seller.get('last_name', '')}".strip(),
            )
            if seller else order.get("seller_id")
        ),
        "phone": seller.get("phone", "") if seller else "",
    }

    items = list(order.get("items") or [])
    if only_rejected_items:
        # qisman rad: faqat rad etilgan qatorlar
        rejected_rows = list(order.get("seller_rejected_items") or [])
        if rejected_rows:
            items = rejected_rows
        else:
            items = [it for it in items if it.get("seller_status") == "rejected"]

    payload["replacement_options"] = []
    for idx, item in enumerate(items):
        enriched = await _enrich_order_item_image(item)
        # item_index: asl buyurtmadagi index
        original_idx = item.get("item_index")
        if original_idx is None:
            # to'liq rad: tartib bo'yicha
            if only_rejected_items:
                # try match by product_id in order items
                original_idx = idx
                for j, oi in enumerate(order.get("items") or []):
                    if oi.get("product_id") == item.get("product_id"):
                        original_idx = j
                        break
            else:
                original_idx = idx
        payload["replacement_options"].append({
            "item_index": int(original_idx),
            "original_item": enriched,
            "similar_products": await build_replacement_candidates_for_item(
                enriched, order.get("seller_id")
            ),
        })

    deadline = parse_iso_dt((order.get("seller_rejection") or {}).get("reminder_due_at"))
    payload["admin_reminder_due_in_minutes"] = (
        max(0, int((deadline - now()).total_seconds() // 60)) if deadline else None
    )
    payload["reject_mode"] = "partial" if only_rejected_items and order.get("status") != "seller_rejected" else "full"
    return payload


async def reassign_rejected_order(order: dict, req: ResolveRejectedOrderReq, admin_user: dict):
    is_full = order.get("status") == "seller_rejected"
    is_partial = bool(order.get("seller_rejected_items"))
    if not is_full and not is_partial:
        raise HTTPException(400, "Buyurtma hali sotuvchi tomonidan rad etilmagan")
    items = order.get("items") or []
    if not items:
        raise HTTPException(400, "Buyurtmada mahsulot topilmadi")
    replacement_map = {int(r.item_index): r.product_id for r in (req.replacements or [])}
    # To'liq rad: barcha itemlar; qisman: faqat rad etilgan indexlar
    if is_full:
        if len(replacement_map) != len(items):
            raise HTTPException(400, "Har bir mahsulot uchun almashtirish tanlang")
    else:
        rejected_idxs = set()
        for it in (order.get("seller_rejected_items") or []):
            if it.get("item_index") is not None:
                rejected_idxs.add(int(it["item_index"]))
        if not rejected_idxs:
            for j, it in enumerate(items):
                if it.get("seller_status") == "rejected":
                    rejected_idxs.add(j)
        if not rejected_idxs:
            raise HTTPException(400, "Rad etilgan mahsulot topilmadi")
        if set(replacement_map.keys()) != rejected_idxs:
            raise HTTPException(400, "Har bir rad etilgan mahsulot uchun almashtirish tanlang")

    new_items = []
    seller_ids = set()
    new_subtotal = 0.0
    new_seller_subtotal = 0.0

    for idx, old_item in enumerate(items):
        pid = replacement_map.get(idx)
        if pid is None:
            # qisman rad: o'zgarmagan (qabul qilingan) qator
            qty = int(old_item.get("qty", 0) or 0)
            price = float(old_item.get("price", 0) or 0)
            base = float(old_item.get("base_price", price) or 0)
            new_items.append(dict(old_item))
            if old_item.get("seller_id"):
                seller_ids.add(old_item.get("seller_id"))
            else:
                seller_ids.add(order.get("seller_id"))
            new_subtotal += price * qty
            new_seller_subtotal += base * qty
            continue
        product = await db.products.find_one({"id": pid, "status": "approved"}, {"_id": 0})
        if not product or product.get("hidden"):
            raise HTTPException(404, "Tanlangan o'xshash mahsulot topilmadi")
        out = product_out(product)
        qty = int(old_item.get("qty", 0) or 0)
        # dona/quti: buyurtma variationiga qarab
        variation = str(old_item.get("variation") or "").lower()
        upb = int(out.get("units_per_box") or 0)
        if "quti" in variation and upb > 0:
            sale_units = upb
        else:
            sale_units = 1
        ordered_units = qty * sale_units
        if int(product.get("stock", 0) or 0) < ordered_units:
            name_uz = (product.get("name") or {})
            if isinstance(name_uz, dict):
                name_uz = name_uz.get("uz") or "Mahsulot"
            raise HTTPException(400, f"{name_uz}: omborda yetarli emas")
        seller_ids.add(product["seller_id"])
        new_price = float(out.get("piece_price") or out.get("effective_price") or product.get("price") or 0)
        if "quti" in variation and upb > 0:
            new_price = float(out.get("effective_box_price") or new_price * upb)
        new_base_price = float(out.get("seller_effective_price") or product.get("price") or 0)
        if "quti" in variation and upb > 0:
            new_base_price = float(out.get("seller_effective_box_price") or new_base_price * upb)
        img = ""
        imgs = product.get("images") or []
        if isinstance(imgs, list) and imgs:
            img = imgs[0] if isinstance(imgs[0], str) else ""
        new_items.append({
            **old_item,
            "product_id": product["id"],
            "name": product.get("name"),
            "image": img or old_item.get("image", ""),
            "price": new_price,
            "base_price": new_base_price,
            "sale_mode": out.get("sale_mode", "piece"),
            "units_per_box": upb,
            "ordered_units": ordered_units,
            "seller_status": "accepted",
            "delivery_status": "pending",
            "replacement_from": {
                "product_id": old_item.get("product_id"),
                "name": old_item.get("name"),
                "seller_id": order.get("seller_id"),
            },
        })
        new_subtotal += new_price * qty
        new_seller_subtotal += new_base_price * qty

    if len(seller_ids) != 1:
        raise HTTPException(400, "Barcha almashtirishlar bir xil sotuvchidan bo'lishi kerak")

    new_seller_id = list(seller_ids)[0]
    new_seller = await db.users.find_one({"id": new_seller_id}, {"_id": 0})
    if not new_seller:
        raise HTTPException(404, "Yangi sotuvchi topilmadi")

    for item in new_items:
        await db.products.update_one({"id": item["product_id"]}, {"$inc": {"stock": -int(item.get("ordered_units", 0) or 0), "sold": int(item.get("ordered_units", 0) or 0)}})

    discount = min(float(order.get("discount", 0) or 0), new_subtotal)
    delivery_fee = float(order.get("delivery_fee", 0) or 0)
    total = max(new_subtotal + delivery_fee - discount, 0)
    eta_days = max(0, int(req.eta_days if req.eta_days is not None else order.get("delivery_eta_days", 0) or 0))
    note = req.note or "Admin boshqa sotuvchidan mahsulot topdi"
    replacement_summary = [{
        "item_index": idx,
        "product_id": item.get("product_id"),
        "name": item_name(item),
        "seller_id": new_seller_id,
        "seller_name": (new_seller.get("seller_info") or {}).get("shop_name", new_seller.get("first_name", "Do'kon")),
    } for idx, item in enumerate(new_items)]

    await db.orders.update_one(
        {"id": order["id"]},
        {"$set": {
            "items": new_items,
            "seller_id": new_seller_id,
            "pickup_location": {"lat": shop_location(new_seller)[0], "lng": shop_location(new_seller)[1]},
            "subtotal": new_subtotal,
            "seller_subtotal": new_seller_subtotal,
            "total": total,
            "status": "packing",
            "delivery_eta_days": eta_days,
            "seller_rejection.resolved_at": iso(),
            "seller_rejected_items": [],
            "status": "confirmed",
            "seller_rejection.resolved_by": admin_user.get("id"),
            "seller_rejection.resolution_note": note,
            "replacement_summary": replacement_summary,
            "courier_id": None,
            "has_returns": False,
            "returned_items_count": 0,
            "delivered_items_count": 0,
        }, "$push": {"status_history": {"status": "packing", "at": iso(), "note": note}}}
    )
    await notify(order["client_id"], f"Buyurtma {order['number']}", "Admin buyurtmangiz uchun boshqa sotuvchidan mahsulot topdi. Buyurtma yana tayyorlanmoqda")
    await notify(new_seller_id, "Almashtirilgan buyurtma", f"{order['number']} sizga tayinlandi")
    admins = await db.users.find({"role": "admin"}, {"_id": 0}).to_list(20)
    for admin_u in admins:
        await notify(admin_u["id"], "Rad etilgan buyurtma yechildi", f"{order['number']} boshqa sotuvchiga o'tkazildi")
def delivered_item_qty(order: dict) -> int:
    items = order.get("items") or []
    if any(i.get("delivery_status") for i in items):
        return sum(int(i.get("qty", 0) or 0) for i in items if i.get("delivery_status") != "returned")
    return sum(int(i.get("qty", 0) or 0) for i in items)


def returned_item_qty(order: dict) -> int:
    return sum(int(i.get("qty", 0) or 0) for i in (order.get("items") or []) if i.get("delivery_status") == "returned")


def returned_item_amount(order: dict) -> float:
    return sum(item_line_base_total(i) for i in (order.get("items") or []) if i.get("delivery_status") == "returned")


def seller_today_snapshot(user: dict, orders: Optional[List[dict]] = None):
    si = user.get("seller_info", {}) or {}
    orders = orders if orders is not None else []
    today = local_day_key()
    reset_at = parse_iso_dt(si.get("stats_reset_at"))

    def after_reset(ts: Optional[str]) -> bool:
        if reset_at is None:
            return True
        dt = parse_iso_dt(ts)
        return bool(dt and dt >= reset_at)

    todays = []
    for o in orders:
        if not o:
            continue
        # bekor / to'liq rad etilganlarni aylanmaga kiritmaymiz
        st = o.get("status") or ""
        if st in ("cancelled", "seller_rejected"):
            continue
        created_ts = o.get("created_at") or status_at(o, "new") or ""
        if local_day_key(created_ts) != today:
            continue
        if not after_reset(created_ts):
            continue
        todays.append(o)

    return {
        "today_orders": len(todays),
        "today_amount": sum(order_seller_amount(o) for o in todays),
        "today_returns_count": sum(1 for o in todays if (o.get("returned_items_count") or returned_item_qty(o)) > 0),
        "today_returns_amount": sum(returned_item_amount(o) for o in todays),
        "stats_reset_at": si.get("stats_reset_at"),
        "today_orders_list": [
            {
                "id": o.get("id"),
                "number": o.get("number"),
                "status": o.get("status"),
                "created_at": o.get("created_at"),
                "amount": order_seller_amount(o),
                "returned_items_count": o.get("returned_items_count") or returned_item_qty(o),
            }
            for o in sorted(todays, key=lambda x: x.get("created_at") or "", reverse=True)[:30]
        ],
    }


def courier_fee_for_order(order: dict) -> float:
    delivered_qty = delivered_item_qty(order)
    if delivered_qty <= 0:
        return 0.0
    return float(order.get("original_delivery_fee", order.get("delivery_fee", 0)) or 0)


def order_cash_to_handover(order: dict) -> float:
    """
    Kuryer mijozdan olgan va admin/do'konga topshiradigan summa.
    - Yetkazilgan mahsulotlar narxi (mijoz narxi)
    - + yetkazish haqi (mijoz to'lagan)
    - - chegirma
    - Qaytarilgan mahsulotlar KIRMAYDI
    Kuryerning o'z haqi (daromad) bu yerga kirmaydi — bu to'liq inkasso summasi.
    """
    if not order:
        return 0.0
    # finalize dan keyin total allaqachon qaytarilganlarsiz
    if order.get("status") == "delivered":
        try:
            return max(0.0, float(order.get("total") or 0))
        except Exception:
            return 0.0
    items = order.get("items") or []
    has_flag = any(i.get("delivery_status") for i in items)
    goods = 0.0
    for i in items:
        st = i.get("delivery_status")
        if has_flag and st == "returned":
            continue
        if has_flag and st and st != "delivered":
            continue
        goods += item_line_total(i)
    try:
        fee = float(order.get("delivery_fee") or 0)
    except Exception:
        fee = 0.0
    try:
        sub = float(order.get("subtotal") or order.get("original_subtotal") or 0)
        disc = float(order.get("discount") or 0)
        if sub > 0 and goods < sub:
            disc = disc * (goods / sub)
    except Exception:
        disc = 0.0
    return max(0.0, goods + fee - disc)


async def build_courier_stats(user: dict, orders: Optional[List[dict]] = None):
    ci = user.get("courier_info", {}) or {}
    orders = orders if orders is not None else await db.orders.find({"courier_id": user["id"]}, {"_id": 0}).to_list(1000)
    today = local_day_key() if "local_day_key" in globals() else iso()[:10]
    reset_at = parse_iso_dt(ci.get("stats_reset_at"))

    def after_reset(ts: Optional[str]) -> bool:
        if reset_at is None:
            return True
        dt = parse_iso_dt(ts)
        return bool(dt and dt >= reset_at)

    active_orders = [o for o in orders if o.get("status") == "courier"]
    taken_today = []
    for o in orders:
        ts = status_at(o, "courier") or o.get("created_at") or ""
        day = local_day_key(ts) if "local_day_key" in globals() else (ts or "")[:10]
        if day == today:
            taken_today.append(o)
    delivered_today = []
    delivered_since_reset = []
    for o in orders:
        dts = status_at(o, "delivered")
        if not dts and o.get("status") != "delivered":
            continue
        if o.get("status") != "delivered" and not dts:
            continue
        # faqat yetkazilgan
        if o.get("status") not in ("delivered",) and not dts:
            continue
        if o.get("status") == "delivered" or dts:
            day = local_day_key(dts or o.get("created_at")) if "local_day_key" in globals() else (dts or "")[:10]
            if day == today:
                delivered_today.append(o)
            if after_reset(dts or o.get("delivery_completed_at") or o.get("created_at")):
                delivered_since_reset.append(o)

    cash_reset = sum(order_cash_to_handover(o) for o in delivered_since_reset)
    cash_today = sum(order_cash_to_handover(o) for o in delivered_today)
    # kuryer shaxsiy haqi (admin panelda asosiy emas)
    fee_reset = sum(courier_fee_for_order(o) for o in delivered_since_reset)
    fee_today = sum(courier_fee_for_order(o) for o in delivered_today)

    return {
        "deliveries": len(delivered_since_reset),
        # asosiy: topshiriladigan inkasso (mijozdan olingan, qaytarilganlarsiz)
        "cash_to_handover": cash_reset,
        "earnings": cash_reset,  # admin UI "daromad" o'rniga inkasso ko'rsatadi
        "courier_fee_earnings": fee_reset,
        "today_deliveries": len(delivered_today),
        "today_cash_to_handover": cash_today,
        "today_earnings": cash_today,
        "today_courier_fee": fee_today,
        "today_taken_count": len(taken_today),
        "today_taken_total": sum(float(o.get("total", 0) or 0) for o in taken_today if o.get("status") == "courier"),
        "active_count": len(active_orders),
        "active_total": sum(float(o.get("total", 0) or 0) for o in active_orders),
        "online": ci.get("online", False),
        "zone": ci.get("zone", ""),
        "stats_reset_at": ci.get("stats_reset_at"),
    }


async def finalize_courier_order(order: dict, selections: List[CourierFinalizeItemReq], courier_user: dict):
    if order.get("status") != "courier":
        raise HTTPException(400, "Buyurtma hali kuryerda emas")

    items = [dict(i) for i in (order.get("items") or [])]
    if not items:
        raise HTTPException(400, "Buyurtmada mahsulot topilmadi")

    selection_map = {int(s.index): s.action for s in selections}
    delivered_qty = 0
    returned_qty = 0
    delivered_total = 0.0
    returned_total = 0.0
    delivered_seller_total = 0.0
    returned_names = []

    for idx, item in enumerate(items):
        action = selection_map.get(idx, item.get("delivery_status") or "delivered")
        if action not in ("delivered", "returned"):
            raise HTTPException(400, "Mahsulot holati noto'g'ri")
        item["delivery_status"] = action
        item["finalized_at"] = iso()
        if action == "returned":
            returned_qty += int(item.get("qty", 0) or 0)
            returned_total += item_line_total(item)
            returned_names.append(item_name(item))
            await db.products.update_one({"id": item["product_id"]}, {"$inc": {"stock": int(item.get("qty", 0) or 0), "sold": -int(item.get("qty", 0) or 0)}})
        else:
            delivered_qty += int(item.get("qty", 0) or 0)
            delivered_total += item_line_total(item)
            delivered_seller_total += item_line_base_total(item)

    original_subtotal = float(order.get("original_subtotal", order.get("subtotal", 0)) or 0)
    original_discount = float(order.get("original_discount", order.get("discount", 0)) or 0)
    original_delivery_fee = float(order.get("original_delivery_fee", order.get("delivery_fee", 0)) or 0)
    original_total = float(order.get("original_total", order.get("total", 0)) or 0)
    delivered_discount = round(original_discount * (delivered_total / original_subtotal)) if original_subtotal else 0
    returned_discount = max(original_discount - delivered_discount, 0)
    final_delivery_fee = original_delivery_fee if delivered_qty > 0 else 0
    final_total = max(delivered_total + final_delivery_fee - delivered_discount, 0)
    courier_fee = final_delivery_fee

    note = "Kuryer yetkazib berishni yakunladi"
    if returned_qty > 0:
        note += f". Qaytgan mahsulotlar: {', '.join(returned_names)}"

    await db.orders.update_one(
        {"id": order["id"]},
        {
            "$set": {
                "items": items,
                "status": "delivered",
                "has_returns": returned_qty > 0,
                "delivered_items_count": delivered_qty,
                "returned_items_count": returned_qty,
                "delivered_subtotal": delivered_total,
                "returned_subtotal": returned_total,
                "seller_subtotal": delivered_seller_total,
                "original_subtotal": original_subtotal,
                "subtotal": delivered_total,
                "original_discount": original_discount,
                "discount": delivered_discount,
                "returned_discount": returned_discount,
                "original_delivery_fee": original_delivery_fee,
                "delivery_fee": final_delivery_fee,
                "original_total": original_total,
                "total": final_total,
                "delivery_completed_at": iso(),
            },
            "$push": {"status_history": {"status": "delivered", "at": iso(), "note": note}},
        },
    )

    client_body = "Holat: Yetkazildi"
    if returned_qty > 0:
        client_body += f". Qaytgan mahsulotlar soni: {returned_qty}"
    await notify(order["client_id"], f"Buyurtma {order['number']}", client_body)

    seller_body = f"{order['number']} yakunlandi. Yetkazilgan mahsulotlar: {delivered_qty} ta"
    if returned_qty > 0:
        seller_body += f", qaytganlar: {returned_qty} ta"
    await notify(order["seller_id"], "Buyurtma yakunlandi", seller_body)

    if delivered_seller_total > 0:
        await db.users.update_one({"id": order["seller_id"]}, {"$inc": {"seller_info.balance": delivered_seller_total}})
    if courier_fee > 0:
        await db.users.update_one({"id": courier_user["id"]}, {"$inc": {"courier_info.earnings": courier_fee, "courier_info.deliveries": 1}})

    admins = await db.users.find({"role": "admin"}, {"_id": 0}).to_list(20)
    for admin in admins:
        admin_body = f"{order['number']} • Kuryer: {courier_user.get('first_name', '')} • Mijoz: {order.get('client_name', '')}"
        if returned_qty > 0:
            admin_body += f" • Qaytdi: {returned_qty} ta"
        await notify(admin["id"], "Kuryer buyurtmani yopdi", admin_body)


async def set_order_status(order, status, note=""):
    await db.orders.update_one({"id": order["id"]}, {"$set": {"status": status}, "$push": {"status_history": {"status": status, "at": iso(), "note": note}}})
    # Invalidate client orders cache so list stays fresh
    cid = order.get("client_id")
    if cid:
        _MY_ORDERS_CACHE.pop(cid, None)
    labels = {"confirmed": "Tasdiqlandi", "packing": "Yig'ilmoqda", "courier": "Kuryerda", "delivered": "Yetkazildi", "cancelled": "Bekor qilindi", "new": "Yangi"}
    await notify(order["client_id"], f"Buyurtma {order['number']}", f"Holat: {labels.get(status, status)}")
    if status == "cancelled":
        for it in order["items"]:
            await db.products.update_one({"id": it["product_id"]}, {"$inc": {"stock": it["qty"], "sold": -it["qty"]}})
    if status == "delivered":
        earn = order.get("seller_subtotal", order["subtotal"])
        await db.users.update_one({"id": order["seller_id"]}, {"$inc": {"seller_info.balance": earn}})


# ---------- Notifications ----------
@api_router.get("/notifications")
async def notifications(user=Depends(get_user)):
    items = await db.notifications.find({"user_id": user["id"]}, {"_id": 0}).sort("created_at", -1).to_list(100)
    await db.notifications.update_many({"user_id": user["id"]}, {"$set": {"read": True}})
    return items


# ---------- Seller ----------
@api_router.post("/seller/apply")
async def seller_apply(req: SellerApplyReq, user=Depends(get_user)):
    if user.get("seller_info"):
        raise HTTPException(400, "Ariza allaqachon yuborilgan")
    shop_lat, shop_lng = req.shop_lat, req.shop_lng
    if shop_lat is None or shop_lng is None:
        addrs = user.get("addresses") or []
        if addrs and addrs[0].get("lat") is not None:
            shop_lat, shop_lng = addrs[0]["lat"], addrs[0]["lng"]
    await db.users.update_one({"id": user["id"]}, {"$set": {"seller_info": {
        "shop_name": req.shop_name, "document": req.document, "approved": False, "rejected": False,
        "commission": None, "balance": 0, "rating": 5.0, "applied_at": iso(),
        "shop_lat": shop_lat, "shop_lng": shop_lng}}})
    admins = await db.users.find({"role": "admin"}).to_list(10)
    for a in admins:
        await notify(a["id"], "Yangi sotuvchi arizasi", f"{req.shop_name} — {user['phone']}")
    return {"ok": True}


async def get_seller(user=Depends(get_user)):
    si = user.get("seller_info")
    if not si or not si.get("approved"):
        raise HTTPException(403, "Sotuvchi tasdiqlanmagan")
    return user




@api_router.post("/seller/search/by-image")
async def seller_search_by_image(image: UploadFile = File(...), user=Depends(get_seller)):
    """Faqat shu sotuvchining mahsulotlari orasidan rasm bo'yicha qidiruv."""
    t0 = time.time()
    try:
        data = await image.read()
    except Exception:
        raise HTTPException(400, "Rasm o'qilmadi")
    if not data or len(data) < 24:
        raise HTTPException(400, "Rasm bo'sh")
    data = _shrink_bytes(data, max_side=512)
    query_sig = _image_signature(data)
    query_feat = _image_features_from_bytes(data)

    my_products = await (
        db.products.find(
            {"seller_id": user["id"], "status": {"$ne": "deleted"}},
            {"_id": 0},
        )
        .limit(200)
        .to_list(200)
    )

    exact = []
    scored = []
    for p in my_products:
        try:
            feat = p.get("image_feat")
            sig = str(p.get("image_sig") or "")
            if not isinstance(feat, list) or len(feat) < 3:
                feat, sig = _feat_from_product(p)
            if not feat:
                continue
            if sig and query_sig and sig == query_sig:
                exact.append(p)
                continue
            scored.append((_feature_distance(query_feat, feat), p))
        except Exception:
            continue

    if exact:
        return {
            "items": [_visual_search_out(p) for p in exact[:20]],
            "exact": True,
            "scope": "seller",
            "took_ms": int((time.time() - t0) * 1000),
        }
    scored.sort(key=lambda x: x[0])
    top = [p for _, p in scored[:20]]
    return {
        "items": [_visual_search_out(p) for p in top],
        "exact": False,
        "scope": "seller",
        "took_ms": int((time.time() - t0) * 1000),
    }


@api_router.get("/seller/products")
async def seller_products(user=Depends(get_seller)):
    """Seller product list — birinchi rasmni ham qaytaramiz (card preview)."""
    try:
        pipeline = [
            {"$match": {"seller_id": user["id"]}},
            {"$project": {
                "_id": 0,
                "id": 1, "name": 1, "price": 1, "old_price": 1, "cost_price": 1,
                "box_price": 1, "units_per_box": 1, "unit_type": 1, "stock": 1, "status": 1,
                "hidden": 1, "pinned": 1, "sold": 1, "views": 1, "rating": 1,
                "category_id": 1, "created_at": 1, "seller_id": 1,
                "markup_percent": 1,
                # faqat birinchi rasm — card uchun yetarli, xotira tejaydi
                "images": {"$slice": [{"$ifNull": ["$images", []]}, 1]},
                "image": 1,
                "preview_image": 1,
            }},
            {"$sort": {"created_at": -1}},
            {"$limit": 100},
        ]
        raw = await db.products.aggregate(pipeline, maxTimeMS=12000, allowDiskUse=True).to_list(150)
        out = []
        for p in raw:
            try:
                p = dict(p)
                p.setdefault("images", [])
                p.setdefault("desc", {"uz": "", "ru": "", "en": ""})
                out.append(product_list_out(p))
            except Exception as e:
                logger.exception("seller product row failed: %s", e)
                # fallback: try keep first image if present on raw doc
                imgs = p.get("images") if isinstance(p.get("images"), list) else []
                first = ""
                if imgs:
                    first = str(imgs[0] or "")
                elif isinstance(p.get("image"), str):
                    first = p["image"]
                out.append(json_safe({
                    "id": p.get("id"),
                    "name": p.get("name") if isinstance(p.get("name"), dict) else {"uz": str(p.get("name") or ""), "ru": "", "en": ""},
                    "images": [first] if first else [],
                    "image": first or None,
                    "preview_image": first or None,
                    "price": p.get("price") or 0,
                    "status": p.get("status") or "pending",
                    "stock": p.get("stock") or 0,
                }))
        return out
    except Exception as e:
        logger.exception("seller_products failed: %s", e)
        return []


@api_router.post("/seller/products")
async def seller_add_product(req: ProductReq, user=Depends(get_seller)):
    p = {
        "id": uid(), "seller_id": user["id"],
        "name": {"uz": req.name_uz, "ru": req.name_ru or req.name_uz, "en": req.name_en or req.name_uz},
        "desc": {"uz": req.desc_uz, "ru": req.desc_ru or req.desc_uz, "en": req.desc_en or req.desc_uz},
        "category_id": req.category_id, "price": req.price, "old_price": req.old_price,
        "cost_price": req.cost_price or 0,
        "box_price": None if str(req.unit_type or "piece").lower() == "kg" else req.box_price,
        "units_per_box": 0 if str(req.unit_type or "piece").lower() == "kg" else max(int(req.units_per_box or 0), 0),
        "unit_type": "kg" if str(req.unit_type or "piece").lower() == "kg" else "piece",
        "images": req.images or ["https://images.unsplash.com/photo-1553456558-aff63285bdd1?w=600&q=80"],
        "stock": req.stock, "variations": req.variations, "status": "pending", "hidden": False,
        "pinned": False, "rating": 0, "reviews_count": 0, "views": 0, "sold": 0, "created_at": iso(),
    }
    await db.products.insert_one(dict(p))
    return product_out(p)


@api_router.put("/seller/products/{pid}")
async def seller_edit_product(pid: str, req: ProductReq, user=Depends(get_seller)):
    p = await db.products.find_one({"id": pid, "seller_id": user["id"]})
    if not p:
        raise HTTPException(404, "Topilmadi")
    upd = {
        "name": {"uz": req.name_uz, "ru": req.name_ru or req.name_uz, "en": req.name_en or req.name_uz},
        "desc": {"uz": req.desc_uz, "ru": req.desc_ru or req.desc_uz, "en": req.desc_en or req.desc_uz},
        "category_id": req.category_id, "price": req.price, "old_price": req.old_price,
        "cost_price": req.cost_price if req.cost_price is not None else p.get("cost_price", 0),
        "box_price": None if str(req.unit_type or "piece").lower() == "kg" else req.box_price,
        "units_per_box": 0 if str(req.unit_type or "piece").lower() == "kg" else max(int(req.units_per_box or 0), 0),
        "unit_type": "kg" if str(req.unit_type or "piece").lower() == "kg" else "piece",
        "stock": req.stock, "status": "pending",
    }
    if req.images:
        upd["images"] = req.images
    await db.products.update_one({"id": pid}, {"$set": upd})
    return {"ok": True}


@api_router.post("/seller/products/{pid}/toggle-hide")
async def seller_hide(pid: str, user=Depends(get_seller)):
    p = await db.products.find_one({"id": pid, "seller_id": user["id"]})
    if not p:
        raise HTTPException(404, "Topilmadi")
    await db.products.update_one({"id": pid}, {"$set": {"hidden": not p.get("hidden", False)}})
    return {"hidden": not p.get("hidden", False)}


@api_router.delete("/seller/products/{pid}")
async def seller_del_product(pid: str, user=Depends(get_seller)):
    await db.products.delete_one({"id": pid, "seller_id": user["id"]})
    return {"ok": True}


@api_router.post("/seller/products/{pid}/update")
async def seller_edit_product_post_alias(pid: str, req: ProductReq, user=Depends(get_seller)):
    return await seller_edit_product(pid, req, user)


@api_router.post("/seller/products/{pid}/delete")
async def seller_del_product_post_alias(pid: str, user=Depends(get_seller)):
    await db.products.delete_one({"id": pid, "seller_id": user["id"]})
    return {"ok": True}


def seller_order_out(o):
    """Sellers must never see buyer's personal data — only the order id/number and what they need to pack."""
    items = [{
        "product_id": i.get("product_id"), "name": i.get("name"), "image": i.get("image"),
        "price": i.get("base_price", i.get("price")), "qty": i.get("qty"), "variation": i.get("variation"),
        "delivery_status": i.get("delivery_status", "delivered" if o.get("status") == "delivered" else "pending"),
    } for i in o.get("items", [])]
    earn_from_items = sum(
        float(i.get("price") or 0) * int(i.get("qty") or 0)
        for i in items
        if i.get("delivery_status") != "returned"
    )
    return {
        "id": o["id"], "number": o["number"], "items": items,
        "earn_total": o.get("seller_subtotal", earn_from_items),
        "status": o["status"], "delivery_method": o.get("delivery_method"),
        "created_at": o.get("created_at"),
        "has_returns": o.get("has_returns", False),
        "returned_items_count": o.get("returned_items_count", 0),
        "delivered_items_count": o.get("delivered_items_count", 0),
        "seller_payment_received_at": o.get("seller_payment_received_at"),
        "seller_payment_confirmed": bool(o.get("seller_payment_confirmed") or o.get("seller_payment_received_at")),
    }


@api_router.get("/seller/orders")
async def seller_orders(user=Depends(get_seller)):
    try:
        pipeline = [
            {"$match": {"seller_id": user["id"]}},
            {"$project": {
                "_id": 0,
                "id": 1, "number": 1, "status": 1, "created_at": 1,
                "seller_subtotal": 1, "delivery_method": 1, "total": 1, "subtotal": 1,
                "has_returns": 1, "returned_items_count": 1, "delivered_items_count": 1,
                "seller_payment_received_at": 1, "seller_payment_confirmed": 1,
                "kg_extra_client_total": 1, "kg_extra_seller_total": 1, "kg_extra_markup_percent": 1,
                "items.product_id": 1,
                "items.name": 1,
                "items.price": 1,
                "items.base_price": 1,
                "items.qty": 1,
                "items.variation": 1,
                "items.delivery_status": 1,
                "items.sale_mode": 1,
                "items.unit_type": 1,
                "items.units_per_box": 1,
                "items.extra_qty": 1,
                "items.extra_seller_price": 1,
                "items.extra_client_price": 1,
                "items.extra_markup_percent": 1,
                "items.extra_note": 1,
            }},
            {"$sort": {"created_at": -1}},
            {"$limit": 80},
        ]
        raw = await db.orders.aggregate(pipeline, maxTimeMS=12000, allowDiskUse=True).to_list(80)
        # Har doim product.unit_type bo'yicha kg/dona ni aniqlash (eski buyurtmalar uchun ham)
        need_pids = set()
        for o in raw:
            for it in (o.get("items") or []):
                if it.get("product_id"):
                    need_pids.add(it["product_id"])
        prod_unit = {}
        if need_pids:
            prods = await db.products.find(
                {"id": {"$in": list(need_pids)}},
                {"_id": 0, "id": 1, "unit_type": 1},
            ).to_list(len(need_pids))
            for p in prods:
                prod_unit[p["id"]] = str(p.get("unit_type") or "piece").lower()
        for o in raw:
            for it in (o.get("items") or []):
                ut = str(it.get("unit_type") or "").lower()
                sm = str(it.get("sale_mode") or "").lower()
                if ut not in ("kg", "piece") or sm not in ("kg", "piece", "box"):
                    ut = prod_unit.get(it.get("product_id") or "", ut or "piece")
                if not ut:
                    ut = prod_unit.get(it.get("product_id") or "", "piece")
                it["unit_type"] = ut if ut in ("kg", "piece") else "piece"
                if sm == "box":
                    it["sale_mode"] = "box"
                elif it["unit_type"] == "kg" or sm == "kg":
                    it["sale_mode"] = "kg"
                    it["unit_type"] = "kg"
                else:
                    it["sale_mode"] = sm if sm in ("piece", "box") else "piece"
        return [json_safe(seller_order_out(o)) for o in raw]
    except Exception as e:
        logger.exception("seller_orders failed: %s", e)
        try:
            raw = await (
                db.orders.find(
                    {"seller_id": user["id"]},
                    {"_id": 0, "id": 1, "number": 1, "status": 1, "created_at": 1,
                     "seller_subtotal": 1, "delivery_method": 1, "total": 1, "subtotal": 1,
                     "seller_payment_received_at": 1, "seller_payment_confirmed": 1,
                     "kg_extra_client_total": 1, "kg_extra_seller_total": 1,
                     "items": 1},
                )
                .sort("created_at", -1)
                .max_time_ms(10000)
                .to_list(50)
            )
            return [json_safe(seller_order_out(o)) for o in raw]
        except Exception as e2:
            logger.exception("seller_orders fallback: %s", e2)
            return []


@api_router.post("/seller/orders/{oid}/action")
async def seller_order_action(oid: str, req: ActionReq, user=Depends(get_seller)):
    o = await db.orders.find_one({"id": oid, "seller_id": user["id"]}, {"_id": 0})
    if not o:
        raise HTTPException(404, "Topilmadi")

    async def _full_reject(order_doc, reason: str):
        for it in order_doc.get("items") or []:
            if it.get("seller_status") == "rejected":
                continue
            units = order_reset_units(it)
            await db.products.update_one({"id": it["product_id"]}, {"$inc": {"stock": units, "sold": -units}})
        rejection = {
            "rejected_at": iso(),
            "rejected_by": user["id"],
            "rejected_shop_name": (user.get("seller_info") or {}).get("shop_name", "Do'kon"),
            "reason": reason or "Sotuvchi rad etdi",
            "reminder_due_at": iso(now() + timedelta(hours=1)),
        }
        await db.orders.update_one(
            {"id": oid},
            {
                "$set": {"status": "seller_rejected", "seller_rejection": rejection, "courier_id": None},
                "$push": {"status_history": {"status": "seller_rejected", "at": iso(), "note": rejection["reason"]}},
            },
        )
        await notify(order_doc["client_id"], f"Buyurtma {order_doc['number']}", "Sotuvchi buyurtmani rad etdi. Admin boshqa variant qidirmoqda")
        admins = await db.users.find({"role": "admin"}, {"_id": 0}).to_list(20)
        for admin_u in admins:
            await notify(
                admin_u["id"],
                "Sotuvchi buyurtmani rad etdi",
                f"{order_doc['number']} • {(user.get('seller_info') or {}).get('shop_name', user.get('first_name', 'Sotuvchi'))}",
            )

    if req.action == "accept" and o["status"] == "new":
        items = list(o.get("items") or [])
        # partial: req.items berilgan bo'lsa har bir mahsulot bo'yicha
        if req.items is not None and len(req.items) > 0:
            decision_map = {}
            for d in req.items:
                act = (d.action or "accept").lower().strip()
                if act not in ("accept", "reject"):
                    act = "accept"
                decision_map[int(d.index)] = act

            accepted_items = []
            rejected_items = []
            for idx, it in enumerate(items):
                act = decision_map.get(idx, "accept")
                row = dict(it)
                if act == "reject":
                    row["seller_status"] = "rejected"
                    row["item_index"] = idx
                    units = order_reset_units(row)
                    await db.products.update_one(
                        {"id": row["product_id"]},
                        {"$inc": {"stock": units, "sold": -units}},
                    )
                    rejected_items.append(row)
                else:
                    row["seller_status"] = "accepted"
                    accepted_items.append(row)

            if not accepted_items:
                # hammasi rad
                await _full_reject(o, req.reason or "Sotuvchi barcha mahsulotlarni rad etdi")
                return {"ok": True, "mode": "full_reject"}

            # qisman yoki to'liq qabul — summalarni qayta hisoblash
            new_subtotal = sum(float(i.get("price") or 0) * int(i.get("qty") or 0) for i in accepted_items)
            new_seller_sub = sum(
                float(i.get("base_price", i.get("price") or 0) or 0) * int(i.get("qty") or 0)
                for i in accepted_items
            )
            delivery_fee = float(o.get("delivery_fee") or 0)
            discount = float(o.get("discount") or 0)
            # chegirma nisbatini saqlab qolish (qisman bo'lsa proporsional)
            old_sub = float(o.get("subtotal") or 0) or 1.0
            new_discount = round(discount * (new_subtotal / old_sub), 2) if discount else 0.0
            new_total = max(0.0, new_subtotal + delivery_fee - new_discount)

            prev_rejected = list(o.get("seller_rejected_items") or [])
            await db.orders.update_one(
                {"id": oid},
                {
                    "$set": {
                        "items": accepted_items,
                        "seller_rejected_items": prev_rejected + rejected_items,
                        "subtotal": new_subtotal,
                        "seller_subtotal": new_seller_sub,
                        "discount": new_discount,
                        "total": new_total,
                        "status": "confirmed",
                        "partial_accept": len(rejected_items) > 0,
                    },
                    "$push": {
                        "status_history": {
                            "status": "confirmed",
                            "at": iso(),
                            "note": (
                                f"Qisman qabul: {len(accepted_items)} ta qabul, {len(rejected_items)} ta rad"
                                if rejected_items
                                else "Sotuvchi qabul qildi"
                            ),
                        }
                    },
                },
            )
            if rejected_items:
                names = ", ".join(
                    str((it.get("name") or {}).get("uz") if isinstance(it.get("name"), dict) else it.get("name") or "?")
                    for it in rejected_items
                )
                await notify(
                    o["client_id"],
                    f"Buyurtma {o['number']}",
                    f"Sotuvchi ba'zi mahsulotlarni rad etdi: {names}. Qolganlari tasdiqlandi.",
                )
                admins = await db.users.find({"role": "admin"}, {"_id": 0}).to_list(20)
                for admin_u in admins:
                    await notify(
                        admin_u["id"],
                        "Qisman rad etilgan buyurtma",
                        f"{o['number']} • rad: {len(rejected_items)} ta, qabul: {len(accepted_items)} ta",
                    )
            else:
                await notify(o["client_id"], f"Buyurtma {o['number']}", "Sotuvchi buyurtmani tasdiqladi")
            return {
                "ok": True,
                "mode": "partial" if rejected_items else "full_accept",
                "accepted": len(accepted_items),
                "rejected": len(rejected_items),
            }

        # oddiy to'liq qabul (items yuborilmagan)
        await set_order_status(o, "confirmed")
        return {"ok": True, "mode": "full_accept"}

    elif req.action == "reject" and o["status"] in ("new", "confirmed"):
        await _full_reject(o, req.reason or "Sotuvchi rad etdi")
    elif req.action == "packed" and o["status"] == "confirmed":
        items = [dict(i) for i in (o.get("items") or [])]
        extras_map = {}
        if req.kg_extras:
            for e in req.kg_extras:
                try:
                    extras_map[int(e.index)] = e
                except Exception:
                    pass
        settings = await db.settings.find_one({"id": "main"}, {"_id": 0}) or {}
        try:
            pct = float(settings.get("kg_extra_markup_percent") if settings.get("kg_extra_markup_percent") is not None else 0)
        except Exception:
            pct = 0.0
        extra_client_sum = 0.0
        extra_seller_sum = 0.0
        for idx, it in enumerate(items):
            mode = str(it.get("sale_mode") or it.get("unit_type") or "piece").lower()
            if mode != "kg":
                continue
            ex = extras_map.get(idx)
            if not ex:
                continue
            try:
                eq = float(ex.extra_qty or 0)
            except Exception:
                eq = 0.0
            try:
                ep = float(ex.extra_price or 0)
            except Exception:
                ep = 0.0
            if eq <= 0 and ep <= 0:
                continue
            client_extra = round(ep * (1 + pct / 100.0)) if pct else round(ep)
            it["extra_qty"] = eq
            it["extra_unit"] = "kg"
            it["extra_seller_price"] = ep
            it["extra_markup_percent"] = pct
            it["extra_client_price"] = client_extra
            it["extra_note"] = f"Ortiqcha {eq} kg"
            extra_client_sum += client_extra
            extra_seller_sum += ep
        set_fields = {
            "status": "packing",
            "items": items,
        }
        if extra_client_sum > 0 or extra_seller_sum > 0:
            old_sub = float(o.get("subtotal") or 0)
            old_seller_sub = float(o.get("seller_subtotal") or 0)
            dfee = float(o.get("delivery_fee") or 0)
            disc = float(o.get("discount") or 0)
            new_sub = old_sub + extra_client_sum
            new_seller_sub = old_seller_sub + extra_seller_sum
            new_total = new_sub + dfee - disc
            set_fields.update({
                "subtotal": new_sub,
                "seller_subtotal": new_seller_sub,
                "total": new_total,
                "kg_extra_client_total": extra_client_sum,
                "kg_extra_seller_total": extra_seller_sum,
                "kg_extra_markup_percent": pct,
            })
            # sotuvchi daromadi (earn_total) ham yangilansin
            if o.get("earn_total") is not None:
                try:
                    set_fields["earn_total"] = float(o.get("earn_total") or 0) + extra_seller_sum
                except Exception:
                    pass
        note = "Yig'ildi"
        if extra_client_sum > 0:
            note += f" • ortiqcha kg mijoz: {int(extra_client_sum):,} so'm"
        await db.orders.update_one(
            {"id": oid, "seller_id": user["id"]},
            {
                "$set": set_fields,
                "$push": {"status_history": {"status": "packing", "at": iso(), "note": note}},
            },
        )
        try:
            await notify(o["client_id"], f"Buyurtma {o.get('number')}", "Buyurtma yig'ilmoqda" + (f". Ortiqcha og'irlik qo'shildi: {int(extra_client_sum):,} so'm" if extra_client_sum else ""))
        except Exception:
            pass
        return {"ok": True, "kg_extra_client_total": extra_client_sum, "kg_extra_seller_total": extra_seller_sum}
    elif req.action == "payment_received" and o["status"] == "delivered":
        if not o.get("seller_payment_received_at"):
            paid_at = iso()
            await db.orders.update_one(
                {"id": oid, "seller_id": user["id"]},
                {"$set": {
                    "seller_payment_received_at": paid_at,
                    "seller_payment_confirmed": True,
                }},
            )
            admins = await db.users.find({"role": "admin"}, {"_id": 0}).to_list(20)
            for admin_u in admins:
                await notify(
                    admin_u["id"],
                    "Sotuvchi pul oldi",
                    f"{o['number']} • {(user.get('seller_info') or {}).get('shop_name', user.get('first_name', 'Sotuvchi'))} pul olganini tasdiqladi",
                )
        return {"ok": True, "seller_payment_received_at": o.get("seller_payment_received_at") or iso()}
    else:
        raise HTTPException(400, "Noto'g'ri harakat")
    return {"ok": True}


@api_router.post("/seller/orders/{oid}/payment-received")
async def seller_payment_received(oid: str, user=Depends(get_seller)):
    """Seller confirms they received cash for a delivered order."""
    o = await db.orders.find_one({"id": oid, "seller_id": user["id"]}, {"_id": 0})
    if not o:
        raise HTTPException(404, "Topilmadi")
    if o.get("status") != "delivered":
        raise HTTPException(400, "Faqat yetkazilgan buyurtma uchun")
    if o.get("seller_payment_received_at"):
        return {
            "ok": True,
            "already": True,
            "seller_payment_received_at": o.get("seller_payment_received_at"),
            "seller_payment_confirmed": True,
        }
    paid_at = iso()
    await db.orders.update_one(
        {"id": oid, "seller_id": user["id"]},
        {"$set": {
            "seller_payment_received_at": paid_at,
            "seller_payment_confirmed": True,
        }},
    )
    admins = await db.users.find({"role": "admin"}, {"_id": 0}).to_list(20)
    shop = (user.get("seller_info") or {}).get("shop_name", user.get("first_name", "Sotuvchi"))
    for admin_u in admins:
        await notify(admin_u["id"], "Sotuvchi pul oldi", f"{o['number']} • {shop} pul olganini tasdiqladi")
    return {
        "ok": True,
        "seller_payment_received_at": paid_at,
        "seller_payment_confirmed": True,
    }


@api_router.get("/seller/stats")
async def seller_stats(user=Depends(get_seller)):
    try:
        day_start = (now() - timedelta(hours=36)).isoformat()
        orders = await (
            db.orders.find(
                {"seller_id": user["id"], "created_at": {"$gte": day_start}},
                {"_id": 0, "id": 1, "number": 1, "status": 1, "created_at": 1,
                 "seller_subtotal": 1, "earn_total": 1, "subtotal": 1, "total": 1,
                 "returned_items_count": 1, "status_history": 1,
                 "items.qty": 1, "items.delivery_status": 1, "items.price": 1,
                 "items.base_price": 1, "items.earn": 1, "items.seller_price": 1},
            )
            .max_time_ms(8000)
            .to_list(300)
        )
        snap = seller_today_snapshot(user, orders)
        # Top products without images
        prods = await (
            db.products.find(
                {"seller_id": user["id"]},
                {"_id": 0, "name": 1, "sold": 1, "views": 1},
            )
            .sort("sold", -1)
            .max_time_ms(8000)
            .to_list(10)
        )
        return json_safe({
            "today_orders": snap["today_orders"],
            "today_sales": snap["today_amount"],
            "today_returns_count": snap["today_returns_count"],
            "today_returns_amount": snap["today_returns_amount"],
            "stats_reset_at": snap.get("stats_reset_at"),
            "top_products": [
                {"name": ((p.get("name") or {}) if isinstance(p.get("name"), dict) else {}).get("uz") or "?",
                 "sold": p.get("sold", 0), "views": p.get("views", 0)}
                for p in prods[:5]
            ],
            "today_orders_list": snap["today_orders_list"],
        })
    except Exception as e:
        logger.exception("seller_stats failed: %s", e)
        return {
            "today_orders": 0, "today_sales": 0, "today_returns_count": 0,
            "today_returns_amount": 0, "top_products": [], "today_orders_list": [],
        }


# ---------- Courier ----------
def is_admin_courier(user: dict) -> bool:
    """True if this account is the single hub/admin courier."""
    if not user:
        return False
    if user.get("role") == "admin_courier":
        return True
    ci = user.get("courier_info") or {}
    return bool(ci.get("is_admin_courier"))


async def get_courier(user=Depends(get_user)):
    if user["role"] not in ("courier", "admin_courier"):
        raise HTTPException(403, "Faqat kuryerlar uchun")
    return user


async def get_admin_courier(user=Depends(get_courier)):
    if not is_admin_courier(user):
        raise HTTPException(403, "Faqat admin kuryer uchun")
    return user


@api_router.post("/courier/apply")
async def courier_apply(req: CourierApplyReq, user=Depends(get_user)):
    if user["role"] == "courier":
        raise HTTPException(400, "Siz allaqachon kuryersiz")
    if user["role"] in ("admin", "moderator"):
        raise HTTPException(400, "Bu amal uchun ruxsat yo'q")
    lat, lng = req.lat, req.lng
    if lat is None or lng is None:
        addrs = user.get("addresses") or []
        if addrs and addrs[0].get("lat") is not None:
            lat, lng = addrs[0]["lat"], addrs[0]["lng"]
    await db.users.update_one({"id": user["id"]}, {"$set": {
        "role": "courier",
        "courier_info": {"online": False, "zone": req.zone, "earnings": 0, "deliveries": 0, "lat": lat, "lng": lng, "stats_reset_at": None},
    }})
    u = await db.users.find_one({"id": user["id"]}, {"_id": 0})
    return public_user(u)


def slim_order_items_for_courier(items):
    """Kuryer ro'yxati uchun — base64 rasmlarni olib tashlash (tezlik)."""
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        row = {k: v for k, v in it.items() if k not in ("image", "images")}
        img = it.get("image")
        if isinstance(img, str) and (img.startswith("http://") or img.startswith("https://")) and len(img) < 500:
            row["image"] = img
        out.append(row)
    return out


async def order_with_route(o: dict):
    o = {k: v for k, v in o.items() if k != "_id"}
    if isinstance(o.get("items"), list):
        o["items"] = slim_order_items_for_courier(o["items"])
    seller = await db.users.find_one({"id": o["seller_id"]}, {"_id": 0})
    o["shop_name"] = seller.get("seller_info", {}).get("shop_name", "Do'kon") if seller else "Do'kon"
    o["shop_phone"] = seller.get("phone", "") if seller else ""
    o["shop_contact_name"] = (f"{seller.get('first_name', '')} {seller.get('last_name', '')}".strip() if seller else "")
    pu = o.get("pickup_location") or {}
    if pu.get("lat") is not None and pu.get("lng") is not None:
        o["shop_lat"], o["shop_lng"] = pu["lat"], pu["lng"]
    else:
        o["shop_lat"], o["shop_lng"] = shop_location(seller)
    dl = o.get("delivery_location") or {}
    if dl.get("lat") is not None and dl.get("lng") is not None:
        o["address_lat"], o["address_lng"] = dl["lat"], dl["lng"]
    if o.get("address_lat") is None or o.get("address_lng") is None:
        o["address_lat"], o["address_lng"] = TASHKENT_CENTER
        o["location_approx"] = True
    deadline = courier_cancel_deadline(o)
    o["can_cancel_courier"] = can_courier_cancel(o)
    o["courier_cancel_deadline"] = deadline.isoformat() if deadline else None
    return o


@api_router.post("/courier/toggle")
async def courier_toggle(req: ToggleReq, user=Depends(get_courier)):
    await db.users.update_one({"id": user["id"]}, {"$set": {"courier_info.online": req.online}})
    return {"online": req.online}


@api_router.get("/courier/available")
async def courier_available(user=Depends(get_courier)):
    """Admin kuryer: hali hubda tekshirilmagan packing buyurtmalar.
    Oddiy kuryer: admin kuryer chek chiqargan (hub-check) buyurtmalar.
    """
    base = {"status": "packing", "courier_id": None, "delivery_method": "courier"}
    if is_admin_courier(user):
        # Yangi yoki hali hub-check bo'lmaganlar
        q = {**base, "admin_courier_checked_at": {"$exists": False}}
    else:
        # Faqat hubdan o'tganlar
        q = {**base, "admin_courier_checked_at": {"$ne": None}}
    orders = await db.orders.find(q, {"_id": 0}).sort("created_at", 1).to_list(50)
    return [await order_with_route(o) for o in orders]


@api_router.get("/courier/my")
async def courier_my(user=Depends(get_courier)):
    orders = await db.orders.find({"courier_id": user["id"]}, {"_id": 0}).sort("created_at", -1).to_list(200)
    return [await order_with_route(o) for o in orders]


@api_router.post("/courier/orders/{oid}/accept")
async def courier_accept(oid: str, user=Depends(get_courier)):
    """Oddiy kuryer qabul qiladi — faqat admin kuryer hub-check qilgan buyurtma.
    Admin kuryer bu endpoint orqali o'zi yetkazishga olmaydi; /hub-check ishlatadi.
    """
    if is_admin_courier(user):
        raise HTTPException(400, "Admin kuryer yetkazishga olmaydi. Avval chek chiqaring (hub-check)")
    o = await db.orders.find_one({"id": oid, "status": "packing", "courier_id": None})
    if not o:
        raise HTTPException(400, "Buyurtma band yoki mavjud emas")
    if not o.get("admin_courier_checked_at"):
        raise HTTPException(400, "Buyurtma hali admin kuryer tomonidan tekshirilmagan")
    await db.orders.update_one({"id": oid}, {"$set": {"courier_id": user["id"]}})
    o["courier_id"] = user["id"]
    await set_order_status(o, "courier")
    return {"ok": True}


@api_router.post("/courier/orders/{oid}/hub-check")
async def courier_hub_check(oid: str, user=Depends(get_admin_courier)):
    """Admin kuryer: buyurtmani qabul qiladi, chek chiqarishga tayyorlaydi va
    oddiy kuryerlar ro'yxatiga chiqaradi. Status packing qoladi, courier_id bo'sh.
    """
    o = await db.orders.find_one({"id": oid, "status": "packing", "courier_id": None}, {"_id": 0})
    if not o:
        raise HTTPException(400, "Buyurtma band yoki mavjud emas")
    if o.get("admin_courier_checked_at"):
        raise HTTPException(400, "Bu buyurtma allaqachon hubdan o'tgan")
    checked_at = iso()
    await db.orders.update_one(
        {"id": oid},
        {
            "$set": {
                "admin_courier_id": user["id"],
                "admin_courier_checked_at": checked_at,
                "admin_courier_name": user.get("first_name") or "Admin kuryer",
            },
            "$push": {
                "status_history": {
                    "status": "packing",
                    "at": checked_at,
                    "note": "Admin kuryer chek chiqardi / hub-check",
                }
            },
        },
    )
    # Oddiy onlayn kuryerlarga xabar
    online_couriers = await db.users.find(
        {"role": "courier", "courier_info.online": True},
        {"_id": 0, "id": 1},
    ).to_list(100)
    for c in online_couriers:
        if c.get("id") == user["id"]:
            continue
        await notify(c["id"], "Yangi buyurtma tayyor", f"{o.get('number', oid)} — hubdan chiqdi, qabul qilishingiz mumkin")
    await notify(o["client_id"], f"Buyurtma {o.get('number', '')}", "Buyurtma ombordan chiqarildi, kuryer tayinlanmoqda")
    return {"ok": True, "checked_at": checked_at, "order_id": oid}


@api_router.get("/courier/hub-queue")
async def courier_hub_queue(user=Depends(get_admin_courier)):
    """Admin kuryer uchun: kutayotgan + allaqachon tekshirilgan (hali olinmagan) ro'yxat."""
    waiting = await db.orders.find(
        {"status": "packing", "courier_id": None, "delivery_method": "courier", "admin_courier_checked_at": {"$exists": False}},
        {"_id": 0},
    ).sort("created_at", 1).to_list(100)
    released = await db.orders.find(
        {"status": "packing", "courier_id": None, "delivery_method": "courier", "admin_courier_checked_at": {"$ne": None}},
        {"_id": 0},
    ).sort("admin_courier_checked_at", -1).to_list(50)
    return {
        "waiting": [await order_with_route(o) for o in waiting],
        "released": [await order_with_route(o) for o in released],
        "is_admin_courier": True,
    }


@api_router.get("/courier/me-flags")
async def courier_me_flags(user=Depends(get_courier)):
    return {
        "is_admin_courier": is_admin_courier(user),
        "role": user.get("role"),
        "online": bool((user.get("courier_info") or {}).get("online")),
        "zone": (user.get("courier_info") or {}).get("zone"),
    }


@api_router.post("/courier/orders/{oid}/cancel")
async def courier_cancel_taken_order(oid: str, user=Depends(get_courier)):
    """Kuryer qabulni bekor qiladi → packing + courier_id=None; statistika qayta hisoblanadi."""
    o = await db.orders.find_one({"id": oid, "courier_id": user["id"]}, {"_id": 0})
    if not o:
        raise HTTPException(404, "Buyurtma topilmadi yoki sizga biriktirilmagan")
    if o.get("status") != "courier":
        raise HTTPException(400, "Faqat kuryerda turgan buyurtmani bekor qilish mumkin")
    if not can_courier_cancel(o):
        raise HTTPException(400, "Bekor qilish muddati tugagan (1 soat)")
    # packing ga qaytarish — hub-check saqlanadi, oddiy kuryerlar yana oladi
    await db.orders.update_one(
        {"id": oid, "courier_id": user["id"]},
        {
            "$set": {
                "courier_id": None,
                "status": "packing",
            },
            "$unset": {"courier_accepted_at": ""},
            "$push": {
                "status_history": {
                    "status": "packing",
                    "at": iso(),
                    "note": f"Kuryer qabulni bekor qildi ({user.get('phone') or user.get('id')})",
                }
            },
        },
    )
    try:
        if o.get("client_id"):
            await notify(o["client_id"], f"Buyurtma {o.get('number')}", "Kuryer buyurtmani qaytardi. Yangi kuryer tayinlanadi")
        admins = await db.users.find({"role": "admin"}, {"_id": 0}).to_list(20)
        for admin_u in admins:
            await notify(admin_u["id"], "Kuryer buyurtmani qaytardi", f"{o.get('number')} yana bo'shatildi")
    except Exception:
        pass
    return {"ok": True, "order_id": oid, "status": "packing"}


@api_router.post("/courier/orders/{oid}/status")
async def courier_status(oid: str, req: StatusReq, user=Depends(get_courier)):
    o = await db.orders.find_one({"id": oid, "courier_id": user["id"]})
    if not o:
        raise HTTPException(404, "Topilmadi")
    if req.status == "delivered" and o["status"] == "courier":
        selections = [CourierFinalizeItemReq(index=idx, action="delivered") for idx, _ in enumerate(o.get("items") or [])]
        await finalize_courier_order(o, selections, user)
    else:
        raise HTTPException(400, "Noto'g'ri status")
    return {"ok": True}


@api_router.post("/courier/orders/{oid}/complete")
async def courier_complete_order(oid: str, req: CourierFinalizeReq, user=Depends(get_courier)):
    o = await db.orders.find_one({"id": oid, "courier_id": user["id"]})
    if not o:
        raise HTTPException(404, "Topilmadi")
    if not req.items:
        raise HTTPException(400, "Mahsulotlar ro'yxati bo'sh")
    await finalize_courier_order(o, req.items, user)
    return {"ok": True}


@api_router.get("/courier/stats")
async def courier_stats(user=Depends(get_courier)):
    orders = await db.orders.find({"courier_id": user["id"]}, {"_id": 0}).to_list(1000)
    return await build_courier_stats(user, orders)


# ---------- Admin ----------


@api_router.get("/admin/map-locations")
async def admin_map_locations(user=Depends(get_admin)):
    """Xarita markerlari:
    - Sotuvchi: yashil
    - Kuryer: sariq
    - Yangi mijoz (1 oy ichida buyurtma yo'q): ko'k
    - 1 oy ichida buyurtma bergan mijoz: qizil
    - Oxirgi 7 kun ichidagi buyurtma nuqtalari: pushti
    """
    try:
        now_dt = now()
        month_ago = iso(now_dt - timedelta(days=30))
        week_ago = iso(now_dt - timedelta(days=7))

        users = await (
            db.users.find(
                {},
                {"_id": 0, "id": 1, "role": 1, "first_name": 1, "last_name": 1, "phone": 1,
                 "addresses": 1, "location": 1, "saved_location": 1, "seller_info": 1, "courier_info": 1,
                 "created_at": 1},
            )
            .max_time_ms(12000)
            .to_list(800)
        )

        # Oxirgi 30 kun buyurtmalar — mijoz faolligi + yetkazish nuqtalari
        recent_orders = await (
            db.orders.find(
                {"created_at": {"$gte": month_ago}},
                {"_id": 0, "id": 1, "number": 1, "client_id": 1, "created_at": 1, "status": 1,
                 "address_lat": 1, "address_lng": 1, "delivery_location": 1, "address_text": 1,
                 "client_name": 1, "client_phone": 1},
            )
            .max_time_ms(12000)
            .to_list(2000)
        )

        # client_id -> eng so'nggi buyurtma vaqti
        last_order_at: Dict[str, str] = {}
        for o in recent_orders:
            cid = o.get("client_id")
            if not cid:
                continue
            at = o.get("created_at") or ""
            if cid not in last_order_at or at > last_order_at[cid]:
                last_order_at[cid] = at

        markers = []

        def add_client_point(mid: str, lat, lng, title: str, subtitle: str, color: str, role_label: str, mtype: str):
            try:
                markers.append({
                    "id": mid,
                    "type": mtype,
                    "color": color,
                    "lat": float(lat),
                    "lng": float(lng),
                    "title": title,
                    "subtitle": subtitle,
                    "role_label": role_label,
                })
            except (TypeError, ValueError):
                pass

        for u in users:
            name = f"{u.get('first_name') or ''} {u.get('last_name') or ''}".strip() or "—"
            phone = u.get("phone") or ""
            uid = u.get("id")
            si = u.get("seller_info") or {}
            ci = u.get("courier_info") or {}
            role = u.get("role") or "client"

            # ——— Sotuvchi: yashil ———
            if si.get("shop_lat") is not None and si.get("shop_lng") is not None:
                add_client_point(
                    f"seller-{uid}", si["shop_lat"], si["shop_lng"],
                    si.get("shop_name") or name, phone, "#16a34a", "Sotuvchi", "seller",
                )
            elif si.get("approved"):
                for a in (u.get("addresses") or []):
                    if a.get("lat") is not None and a.get("lng") is not None:
                        add_client_point(
                            f"seller-{uid}-addr", a["lat"], a["lng"],
                            si.get("shop_name") or name, a.get("text") or phone,
                            "#16a34a", "Sotuvchi", "seller",
                        )
                        break

            # ——— Kuryer: sariq ———
            if role in ("courier", "admin_courier") or ci:
                clat = ci.get("lat")
                clng = ci.get("lng")
                # saved_location fallback
                sl = u.get("saved_location") or {}
                if clat is None and sl.get("lat") is not None:
                    clat, clng = sl.get("lat"), sl.get("lng")
                if clat is None:
                    for a in (u.get("addresses") or []):
                        if a.get("lat") is not None and a.get("lng") is not None:
                            clat, clng = a["lat"], a["lng"]
                            break
                if clat is not None and clng is not None:
                    zone = ci.get("zone") or ""
                    online = "● Onlayn" if ci.get("online") else "○ Oflayn"
                    add_client_point(
                        f"courier-{uid}", clat, clng, name,
                        f"{phone} • {zone} • {online}".strip(" •"),
                        "#EAB308", "Kuryer", "courier",
                    )

            # ——— Mijoz: qizil (1 oy ichida buyurtma) / ko'k (yangi) ———
            is_seller_only = bool(si.get("approved")) and role == "client"
            if role == "client" and not is_seller_only:
                last_at = last_order_at.get(uid or "")
                if last_at and last_at >= month_ago:
                    color = "#DC2626"  # qizil — 1 oy ichida zakaz
                    mtype = "client_month"
                    role_label = "Mijoz (1 oy ichida)"
                else:
                    color = "#2563EB"  # ko'k — yangi / faol emas
                    mtype = "client_new"
                    role_label = "Yangi mijoz"

                points = []
                loc = u.get("saved_location") or u.get("location") or {}
                if loc.get("lat") is not None and loc.get("lng") is not None:
                    points.append(("loc", loc["lat"], loc["lng"], phone))
                for i, a in enumerate(u.get("addresses") or []):
                    if a.get("lat") is not None and a.get("lng") is not None:
                        points.append((f"a{i}", a["lat"], a["lng"], a.get("label") or a.get("text") or phone))
                # dublikatsiz birinchi nuqta yetarli, lekin barcha manzillarni ko'rsatamiz
                seen = set()
                for key, lat, lng, sub in points:
                    sk = (round(float(lat), 5), round(float(lng), 5))
                    if sk in seen:
                        continue
                    seen.add(sk)
                    add_client_point(f"client-{uid}-{key}", lat, lng, name, sub, color, role_label, mtype)

        # ——— Oxirgi 7 kun buyurtma yetkazish nuqtalari: pushti ———
        for o in recent_orders:
            if (o.get("created_at") or "") < week_ago:
                continue
            lat = o.get("address_lat")
            lng = o.get("address_lng")
            dl = o.get("delivery_location") or {}
            if lat is None:
                lat = dl.get("lat")
            if lng is None:
                lng = dl.get("lng")
            if lat is None or lng is None:
                continue
            add_client_point(
                f"order-week-{o.get('id')}",
                lat, lng,
                o.get("number") or "Buyurtma",
                f"{o.get('client_name') or ''} • {o.get('address_text') or o.get('client_phone') or ''}".strip(" •"),
                "#EC4899",  # pushti
                "Buyurtma (7 kun)",
                "order_week",
            )

        counts = {
            "client_month": sum(1 for m in markers if m["type"] == "client_month"),
            "client_new": sum(1 for m in markers if m["type"] == "client_new"),
            "client": sum(1 for m in markers if m["type"] in ("client_month", "client_new", "client")),
            "seller": sum(1 for m in markers if m["type"] == "seller"),
            "courier": sum(1 for m in markers if m["type"] == "courier"),
            "order_week": sum(1 for m in markers if m["type"] == "order_week"),
        }
        return json_safe({"markers": markers, "counts": counts})
    except Exception as e:
        logger.exception("admin_map_locations failed: %s", e)
        return {"markers": [], "counts": {"client": 0, "client_month": 0, "client_new": 0, "seller": 0, "courier": 0, "order_week": 0}}



@api_router.get("/admin/dashboard")
async def admin_dashboard(user=Depends(get_admin)):
    today = iso()[:10]
    settings = await db.settings.find_one({"id": "main"}, {"_id": 0}) or {}
    cutoff = admin_dashboard_cutoff(settings)

    # Only fields needed for stats — not full order docs
    proj = {"_id": 0, "created_at": 1, "status": 1, "total": 1, "items.product_id": 1, "items.base_price": 1, "items.price": 1, "items.qty": 1, "items.delivery_status": 1, "items.extra_client_price": 1, "items.extra_seller_price": 1, "items.extra_qty": 1}
    orders, products, clients, sellers, couriers_online, pending_products, pending_sellers, total_products, low_stock_count = await asyncio.gather(
        db.orders.find({}, proj).to_list(10000),
        db.products.find({"cost_price": {"$gt": 0}}, {"id": 1, "cost_price": 1, "_id": 0}).to_list(5000),
        db.users.count_documents({"role": "client"}),
        db.users.count_documents({"seller_info.approved": True}),
        db.users.count_documents({"role": "courier", "courier_info.online": True}),
        db.products.count_documents({"status": "pending"}),
        db.users.count_documents({"seller_info.approved": False, "seller_info.rejected": False, "seller_info": {"$exists": True}}),
        db.products.count_documents({"status": {"$ne": "deleted"}}),
        db.products.count_documents({
            "status": {"$in": ["approved", "pending"]},
            "hidden": {"$ne": True},
            "stock": {"$gt": 0, "$lt": 10},
        }),
    )

    def after_cutoff(order: dict) -> bool:
        if cutoff is None:
            return True
        created = parse_iso_dt(order.get("created_at"))
        return bool(created and created >= cutoff)

    money_orders = [o for o in orders if after_cutoff(o)]
    today_orders = [o for o in orders if (o.get("created_at") or "")[:10] == today]
    today_money_orders = [o for o in money_orders if (o.get("created_at") or "")[:10] == today]

    cost_map = {p["id"]: float(p.get("cost_price") or 0) for p in products}

    def calc_profit(order):
        """
        Sof foyda (platforma):
        1) asosiy: mijoz narxi - sotuvchi narxi (ustama)
        2) ortiqcha kg: extra_client - extra_seller
        3) agar ustama 0 va tannarx bor: base - cost_price
        """
        prof = 0.0
        for it in order.get("items") or []:
            if it.get("delivery_status") == "returned":
                continue
            try:
                qty = float(it.get("qty", 0) or 0)
                price = float(it.get("price", 0) or 0)
                base = float(it.get("base_price") if it.get("base_price") is not None else 0)
                if base <= 0:
                    base = price
                # platforma ustamasi
                margin = max(0.0, price - base) * qty
                if margin <= 0:
                    cp = float(cost_map.get(it.get("product_id"), 0) or 0)
                    if cp > 0:
                        margin = max(0.0, base - cp) * qty
                # ortiqcha kg foydasi
                extra_c = float(it.get("extra_client_price") or 0)
                extra_s = float(it.get("extra_seller_price") or 0)
                if extra_c > 0:
                    if extra_s > 0:
                        margin += max(0.0, extra_c - extra_s)
                    else:
                        # faqat client summa bor — foiz taxminan default markup
                        margin += max(0.0, extra_c * 0.0)  # agar seller yozmagan, platform 0
                        # yoki butun extra_c ni platformaga? yo'q — seller oladi
                prof += margin
            except (TypeError, ValueError):
                continue
        return prof

    today_profit = sum(calc_profit(o) for o in today_money_orders if o.get("status") not in ("cancelled", "seller_rejected"))
    total_profit = sum(calc_profit(o) for o in money_orders if o.get("status") == "delivered")
    today_sales_total = sum(float(o.get("total", 0) or 0) for o in today_money_orders if o.get("status") != "cancelled")
    profit_margin = round((today_profit / today_sales_total) * 100, 1) if today_sales_total > 0 else 0.0

    return {
        "today_orders": len(today_orders),
        "today_sales": today_sales_total,
        "today_profit": today_profit,
        "total_orders": len(orders),
        "total_sales": sum(float(o.get("total", 0) or 0) for o in money_orders if o.get("status") == "delivered"),
        "total_profit": total_profit,
        "profit_margin": profit_margin,
        "clients": clients,
        "sellers": sellers,
        "couriers_online": couriers_online,
        "pending_products": pending_products,
        "total_products": total_products,
        "low_stock_count": low_stock_count,
        "pending_sellers": pending_sellers,
        "new_orders": sum(1 for o in orders if o.get("status") == "new"),
        "dashboard_stats_reset_at": settings.get("dashboard_money_reset_at") or settings.get("dashboard_stats_reset_at"),
    }




@api_router.get("/admin/products/low-stock")
async def admin_low_stock_products(user=Depends(get_admin)):
    """10 tadan kam dona qolgan mahsulotlar (rasm bilan)."""
    try:
        prods = await (
            db.products.find(
                {
                    "status": {"$in": ["approved", "pending"]},
                    "hidden": {"$ne": True},
                    "stock": {"$gt": 0, "$lt": 10},
                },
                {
                    "_id": 0,
                    "id": 1,
                    "name": 1,
                    "images": 1,
                    "image": 1,
                    "stock": 1,
                    "price": 1,
                    "seller_id": 1,
                    "status": 1,
                    "units_per_box": 1,
                    "unit_type": 1,
                },
            )
            .sort("stock", 1)
            .limit(100)
            .to_list(100)
        )
        seller_ids = list({p.get("seller_id") for p in prods if p.get("seller_id")})
        sellers = await db.users.find(
            {"id": {"$in": seller_ids}},
            {"_id": 0, "id": 1, "first_name": 1, "seller_info.shop_name": 1, "phone": 1},
        ).to_list(len(seller_ids) or 1)
        smap = {s["id"]: s for s in sellers}
        items = []
        for p in prods:
            s = smap.get(p.get("seller_id")) or {}
            si = s.get("seller_info") or {}
            img = ""
            imgs = p.get("images") or []
            if isinstance(imgs, list) and imgs:
                img = imgs[0] if isinstance(imgs[0], str) else (imgs[0].get("url") if isinstance(imgs[0], dict) else "")
            if not img and isinstance(p.get("image"), str):
                img = p["image"]
            items.append({
                "id": p.get("id"),
                "name": p.get("name"),
                "image": img,
                "stock": int(p.get("stock") or 0),
                "price": float(p.get("price") or 0),
                "status": p.get("status"),
                "units_per_box": int(p.get("units_per_box") or 0),
                "unit_type": p.get("unit_type") or "piece",
                "seller_name": si.get("shop_name") or s.get("first_name") or "—",
                "seller_phone": s.get("phone") or "",
            })
        return json_safe({"items": items, "total": len(items)})
    except Exception as e:
        logger.exception("admin_low_stock: %s", e)
        return {"items": [], "total": 0}


@api_router.get("/admin/dashboard/history")
async def admin_dashboard_history(metric: str, user=Depends(get_admin)):
    today = iso()[:10]
    settings = await db.settings.find_one({"id": "main"}, {"_id": 0}) or {}
    cutoff = admin_dashboard_cutoff(settings)
    orders = await db.orders.find({}, {"_id": 0}).sort("created_at", -1).to_list(5000)
    money_orders = orders
    if cutoff is not None:
        money_orders = [o for o in orders if parse_iso_dt(o.get("created_at")) and parse_iso_dt(o.get("created_at")) >= cutoff]
    users = await db.users.find({}, {"_id": 0}).sort("created_at", -1).to_list(1000)
    products = await db.products.find({}, {"_id": 0}).sort("created_at", -1).to_list(1000)
    product_costs = {p["id"]: float(p.get("cost_price") or 0) for p in products}

    def order_profit(order: dict) -> float:
        total = 0.0
        for it in order.get("items", []):
            cost = product_costs.get(it.get("product_id"), 0)
            if cost <= 0 or it.get("delivery_status") == "returned":
                continue
            actual_price = float(it.get("base_price", it.get("price", 0)) or 0)
            total += max(0.0, actual_price - cost) * int(it.get("qty", 0) or 0)
        return total

    items = []
    title = metric
    if metric == "today_orders":
        title = "Bugungi buyurtmalar tarixi"
        items = [{"id": o["id"], "primary": o["number"], "secondary": f"{o.get('client_name', '')} • {o.get('status', '')}", "value": float(o.get("total", 0) or 0), "date": o.get("created_at")} for o in orders if (o.get("created_at") or "")[:10] == today]
    elif metric == "today_sales":
        title = "Bugungi savdo tarixi"
        items = [{"id": o["id"], "primary": o["number"], "secondary": o.get("client_name", ""), "value": float(o.get("total", 0) or 0), "date": o.get("created_at")} for o in money_orders if (o.get("created_at") or "")[:10] == today and o.get("status") != "cancelled"]
    elif metric == "today_profit":
        title = "Bugungi sof foyda manbalari"
        items = [{"id": o["id"], "primary": o["number"], "secondary": o.get("client_name", ""), "value": order_profit(o), "date": o.get("created_at")} for o in money_orders if (o.get("created_at") or "")[:10] == today and o.get("status") != "cancelled"]
    elif metric == "total_orders":
        title = "Barcha buyurtmalar"
        items = [{"id": o["id"], "primary": o["number"], "secondary": f"{o.get('client_name', '')} • {o.get('status', '')}", "value": float(o.get("total", 0) or 0), "date": o.get("created_at")} for o in orders]
    elif metric == "total_sales":
        title = "Jami savdo"
        items = [{"id": o["id"], "primary": o["number"], "secondary": o.get("client_name", ""), "value": float(o.get("total", 0) or 0), "date": o.get("created_at")} for o in money_orders if o.get("status") == "delivered"]
    elif metric == "total_profit":
        title = "Jami sof foyda"
        items = [{"id": o["id"], "primary": o["number"], "secondary": o.get("client_name", ""), "value": order_profit(o), "date": o.get("created_at")} for o in money_orders if o.get("status") == "delivered"]
    elif metric == "profit_margin":
        title = "Bugungi marja hisob-kitobi"
        items = [{"id": o["id"], "primary": o["number"], "secondary": o.get("client_name", ""), "value": order_profit(o), "date": o.get("created_at")} for o in money_orders if (o.get("created_at") or "")[:10] == today and o.get("status") != "cancelled"]
    elif metric == "clients":
        title = "Mijozlar ro'yxati"
        items = [{"id": u["id"], "primary": f"{u.get('first_name', '')} {u.get('last_name', '')}".strip() or u.get("phone", ""), "secondary": u.get("phone", ""), "value": 0, "date": u.get("created_at")} for u in users if u.get("role") == "client"]
    elif metric == "sellers":
        title = "Tasdiqlangan sotuvchilar"
        items = [{"id": u["id"], "primary": (u.get("seller_info") or {}).get("shop_name", "Do'kon"), "secondary": f"{u.get('first_name', '')} {u.get('last_name', '')}".strip() or u.get("phone", ""), "value": float((u.get("seller_info") or {}).get("balance", 0) or 0), "date": (u.get("seller_info") or {}).get("applied_at") or u.get("created_at")} for u in users if (u.get("seller_info") or {}).get("approved")]
    elif metric == "couriers_online":
        title = "Onlayn kuryerlar"
        items = [{"id": u["id"], "primary": f"{u.get('first_name', '')} {u.get('last_name', '')}".strip() or u.get("phone", ""), "secondary": u.get("phone", ""), "value": 0, "date": u.get("created_at")} for u in users if u.get("role") == "courier" and (u.get("courier_info") or {}).get("online")]
    elif metric == "new_orders":
        title = "Yangi buyurtmalar"
        items = [{"id": o["id"], "primary": o["number"], "secondary": o.get("client_name", ""), "value": float(o.get("total", 0) or 0), "date": o.get("created_at")} for o in orders if o.get("status") == "new"]
    elif metric == "pending_products":
        title = "Moderatsiyadagi mahsulotlar"
        items = [{"id": p["id"], "primary": p.get("name", {}).get("uz") or p.get("id"), "secondary": p.get("seller_id", ""), "value": float(product_out(p).get("display_price", p.get("price", 0)) or 0), "date": p.get("created_at")} for p in products if p.get("status") == "pending"]
    elif metric == "pending_sellers":
        title = "Kutilayotgan sotuvchilar"
        items = [{"id": u["id"], "primary": (u.get("seller_info") or {}).get("shop_name", "Do'kon"), "secondary": u.get("phone", ""), "value": 0, "date": (u.get("seller_info") or {}).get("applied_at") or u.get("created_at")} for u in users if u.get("seller_info") and not (u.get("seller_info") or {}).get("approved") and not (u.get("seller_info") or {}).get("rejected")]
    else:
        raise HTTPException(404, "Statistika turi topilmadi")

    return {"metric": metric, "title": title, "items": items[:100]}


@api_router.get("/admin/users")
async def admin_users(role: Optional[str] = None, q: Optional[str] = None, user=Depends(get_admin)):
    """Fast admin users list.
    Sellers/couriers: only today's (or recent) orders for stats — not full history.
    """
    try:
        query: Dict[str, Any] = {}
        if role == "seller":
            query["seller_info"] = {"$exists": True}
        elif role:
            query["role"] = role
        if q:
            rx = {"$regex": re.escape(q), "$options": "i"}
            query["$or"] = [{"first_name": rx}, {"last_name": rx}, {"phone": rx}]

        # Lean user projection — skip heavy nested junk
        user_proj = {
            "_id": 0,
            "id": 1,
            "phone": 1,
            "first_name": 1,
            "last_name": 1,
            "role": 1,
            "blocked": 1,
            "created_at": 1,
            "seller_info": 1,
            "courier_info": 1,
            "addresses": 1,
        }
        users = await (
            db.users.find(query, user_proj)
            .sort("created_at", -1)
            .max_time_ms(8000)
            .to_list(150)
        )

        if role == "seller":
            ids = [u["id"] for u in users if u.get("id")]
            if not ids:
                return []
            # UTC+5 kuni — 2 kunlik oyna (UTC siljishiga barqaror)
            day_start = (now() - timedelta(hours=36)).isoformat()
            all_orders = await (
                db.orders.find(
                    {"seller_id": {"$in": ids}, "created_at": {"$gte": day_start}},
                    {"_id": 0, "id": 1, "number": 1, "seller_id": 1, "status": 1, "total": 1,
                     "subtotal": 1, "seller_subtotal": 1, "earn_total": 1, "created_at": 1,
                     "client_name": 1, "returned_items_count": 1, "status_history": 1,
                     "items.product_id": 1, "items.name": 1, "items.qty": 1, "items.price": 1,
                     "items.base_price": 1, "items.earn": 1, "items.seller_price": 1,
                     "items.delivery_status": 1},
                )
                .max_time_ms(12000)
                .to_list(1000)
            )
            by_seller: Dict[str, List[dict]] = {}
            for o in all_orders:
                by_seller.setdefault(o.get("seller_id") or "", []).append(o)
            result = []
            for seller_user in users:
                orders = by_seller.get(seller_user["id"], [])
                snap = dict(seller_user)
                today_stats = seller_today_snapshot(seller_user, orders)
                snap["seller_today_summary"] = {
                    "today_orders": today_stats["today_orders"],
                    "today_amount": today_stats["today_amount"],
                    "today_returns_count": today_stats["today_returns_count"],
                    "today_returns_amount": today_stats["today_returns_amount"],
                    "stats_reset_at": today_stats.get("stats_reset_at"),
                }
                snap["seller_today_orders"] = today_stats.get("today_orders_list") or []
                result.append(json_safe(snap))
            return result

        if role != "courier":
            return json_safe(users)

        # Couriers — use courier_info for summary; light order stats only
        ids = [u["id"] for u in users if u.get("id")]
        all_orders = []
        if ids:
            all_orders = await (
                db.orders.find(
                    {"courier_id": {"$in": ids}},
                    {"_id": 0, "id": 1, "number": 1, "courier_id": 1, "status": 1, "total": 1,
                     "subtotal": 1, "delivery_fee": 1, "discount": 1, "delivered_subtotal": 1,
                     "created_at": 1, "delivery_completed_at": 1, "client_name": 1, "client_phone": 1,
                     "status_history": 1,
                     "items.qty": 1, "items.price": 1, "items.base_price": 1,
                     "items.delivery_status": 1, "items.name": 1},
                )
                .max_time_ms(10000)
                .to_list(800)
            )
        by_courier: Dict[str, List[dict]] = {}
        for o in all_orders:
            by_courier.setdefault(o.get("courier_id") or "", []).append(o)

        result = []
        for courier_user in users:
            orders = by_courier.get(courier_user["id"], [])
            # Inline light stats — avoid extra DB inside build_courier_stats
            stats = await build_courier_stats(courier_user, orders)
            delivered_orders = [o for o in orders if o.get("status") == "delivered" or status_at(o, "delivered")]
            daily: Dict[str, Dict[str, Any]] = {}
            recent_orders = []
            for o in delivered_orders[:40]:
                delivered_ts = status_at(o, "delivered") or o.get("created_at")
                day = (delivered_ts or "")[:10]
                delivered_products = delivered_item_qty(o)
                returned_products = returned_item_qty(o)
                bucket = daily.setdefault(day, {"date": day, "orders": 0, "delivered_products": 0, "returned_products": 0, "recipients": []})
                bucket["orders"] += 1
                bucket["delivered_products"] += delivered_products
                bucket["returned_products"] += returned_products
                if len(bucket["recipients"]) < 5:
                    bucket["recipients"].append(o.get("client_name") or o.get("client_phone") or o.get("number"))
                if len(recent_orders) < 8:
                    recent_orders.append({
                        "id": o.get("id"),
                        "number": o.get("number"),
                        "date": delivered_ts,
                        "client_name": o.get("client_name", ""),
                        "client_phone": o.get("client_phone", ""),
                        "delivered_products": delivered_products,
                        "returned_products": returned_products,
                        "items": [{
                            "name": item_name(i),
                            "qty": int(i.get("qty", 0) or 0),
                            "delivery_status": i.get("delivery_status", "delivered"),
                        } for i in (o.get("items") or [])[:5]],
                    })
            snap = dict(courier_user)
            snap["courier_stats_summary"] = stats
            snap["courier_daily_history"] = sorted(daily.values(), key=lambda x: x["date"], reverse=True)[:7]
            snap["courier_recent_orders"] = recent_orders
            result.append(json_safe(snap))
        return result
    except Exception as e:
        logger.exception("admin_users failed: %s", e)
        return []


@api_router.post("/admin/users/{target_id}/block")
async def admin_block(target_id: str, req: BlockReq, user=Depends(get_admin)):
    await db.users.update_one({"id": target_id}, {"$set": {"blocked": req.blocked}})
    return {"ok": True}


@api_router.post("/admin/couriers/{target_id}/reset-stats")
async def admin_reset_courier_stats(target_id: str, user=Depends(get_admin)):
    courier_user = await db.users.find_one({"id": target_id, "role": "courier"}, {"_id": 0})
    if not courier_user:
        raise HTTPException(404, "Kuryer topilmadi")
    reset_at = iso()
    await db.users.update_one(
        {"id": target_id},
        {"$set": {"courier_info.earnings": 0, "courier_info.deliveries": 0, "courier_info.stats_reset_at": reset_at}},
    )
    await notify(target_id, "Kuryer statistikasi yangilandi", "Admin statistik hisoblagichlarini 0 ga tushirdi")
    return {"ok": True, "reset_at": reset_at}


@api_router.post("/admin/sellers/{target_id}/reset-stats")
async def admin_reset_seller_stats(target_id: str, user=Depends(get_admin)):
    seller_user = await db.users.find_one({"id": target_id, "seller_info": {"$exists": True}}, {"_id": 0})
    if not seller_user:
        raise HTTPException(404, "Sotuvchi topilmadi")
    reset_at = iso()
    await db.users.update_one({"id": target_id}, {"$set": {"seller_info.stats_reset_at": reset_at}})
    await notify(target_id, "Sotuvchi statistikasi yangilandi", "Admin bugungi statistika hisoblagichini 0 ga tushirdi")
    return {"ok": True, "reset_at": reset_at}


@api_router.post("/admin/sellers/{target_id}/approve")
async def admin_approve_seller(target_id: str, user=Depends(get_admin)):
    await db.users.update_one({"id": target_id}, {"$set": {"seller_info.approved": True, "seller_info.rejected": False}})
    await notify(target_id, "Tabriklaymiz!", "Sotuvchi arizangiz tasdiqlandi. Mahsulot joylashtirishingiz mumkin")
    return {"ok": True}


@api_router.post("/admin/sellers/{target_id}/reject")
async def admin_reject_seller(target_id: str, user=Depends(get_admin)):
    await db.users.update_one({"id": target_id}, {"$set": {"seller_info.rejected": True}})
    await notify(target_id, "Ariza rad etildi", "Sotuvchi arizangiz rad etildi")
    return {"ok": True}


@api_router.get("/admin/products")
async def admin_products(
    status: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=300),
    user=Depends(get_admin),
):
    """Fast admin product list — aggregation projects tiny fields BEFORE sort
    so Atlas free-tier does not hit QueryExceededMemoryLimitNoDiskUseAllowed.
    """
    try:
        match: Dict[str, Any] = {}
        if status and status.strip():
            match["status"] = status.strip()
        lim = min(int(limit or 100), 100)
        sk = max(int(skip or 0), 0)
        pipeline = [
            {"$match": match} if match else {"$match": {}},
            {"$project": {
                "_id": 0,
                "id": 1,
                "name": 1,
                "price": 1,
                "old_price": 1,
                "status": 1,
                "stock": 1,
                "sold": 1,
                "pinned": 1,
                "hidden": 1,
                "seller_id": 1,
                "category_id": 1,
                "subcategory_id": 1,
                "rating": 1,
                "reviews_count": 1,
                "created_at": 1,
                "markup_percent": 1,
            }},
            {"$sort": {"created_at": -1}},
            {"$skip": sk},
            {"$limit": lim},
        ]
        raw = await db.products.aggregate(pipeline, maxTimeMS=15000, allowDiskUse=True).to_list(lim)
        out = []
        for p in raw:
            try:
                name = p.get("name")
                if not isinstance(name, dict):
                    name = {"uz": str(name or ""), "ru": "", "en": ""}
                price = float(p.get("price") or 0)
                markup = float(p.get("markup_percent") or SETTINGS_CACHE.get("default_markup_percent") or 0)
                display = round(price * (1 + markup / 100)) if markup else price
                out.append({
                    "id": p.get("id"),
                    "name": {
                        "uz": str(name.get("uz") or ""),
                        "ru": str(name.get("ru") or ""),
                        "en": str(name.get("en") or ""),
                    },
                    "images": [],
                    "price": price,
                    "old_price": p.get("old_price"),
                    "display_price": display,
                    "effective_price": display,
                    "status": p.get("status") or "pending",
                    "stock": p.get("stock") or 0,
                    "sold": p.get("sold") or 0,
                    "pinned": bool(p.get("pinned")),
                    "hidden": bool(p.get("hidden")),
                    "seller_id": p.get("seller_id"),
                    "category_id": p.get("category_id"),
                    "rating": p.get("rating") or 0,
                    "reviews_count": p.get("reviews_count") or 0,
                    "created_at": p.get("created_at"),
                    "markup_percent": markup,
                    "out_of_stock": (p.get("stock") or 0) <= 0,
                })
            except Exception as e:
                logger.exception("admin product row failed: %s", e)
                out.append({
                    "id": (p or {}).get("id"),
                    "name": {"uz": "Xatolik", "ru": "", "en": ""},
                    "images": [],
                    "price": 0,
                    "display_price": 0,
                    "status": (p or {}).get("status") or "pending",
                    "out_of_stock": True,
                })
        return out
    except Exception as e:
        logger.exception("admin_products fatal: %s", e)
        return []


@api_router.post("/admin/products/{pid}/moderate")
async def admin_moderate(pid: str, req: ActionReq, user=Depends(get_admin)):
    p = await db.products.find_one({"id": pid})
    if not p:
        raise HTTPException(404, "Topilmadi")
    pname = ((p.get("name") or {}) if isinstance(p.get("name"), dict) else {}).get("uz") or "Mahsulot"
    if req.action == "approve":
        await db.products.update_one({"id": pid}, {"$set": {"status": "approved"}})
        await notify(p["seller_id"], "Mahsulot tasdiqlandi", pname)
    elif req.action == "reject":
        await db.products.update_one({"id": pid}, {"$set": {"status": "rejected"}})
        await notify(p["seller_id"], "Mahsulot rad etildi", f"{pname}: {req.reason}")
    elif req.action == "pin":
        await db.products.update_one({"id": pid}, {"$set": {"pinned": not p.get("pinned", False)}})
    elif req.action == "delete":
        await db.products.delete_one({"id": pid})
    return {"ok": True}


@api_router.post("/admin/products/{pid}/markup")
async def admin_product_markup(pid: str, req: MarkupReq, user=Depends(get_admin)):
    """Admin sets how many percent to add on top of this seller's price. Buyers see price*(1+percent/100); seller keeps seeing their own raw price."""
    p = await db.products.find_one({"id": pid})
    if not p:
        raise HTTPException(404, "Topilmadi")
    percent = max(0.0, req.percent)
    await db.products.update_one({"id": pid}, {"$set": {"markup_percent": percent}})
    return product_out(await db.products.find_one({"id": pid}))


@api_router.post("/admin/products/bulk-markup")
async def admin_bulk_markup(req: BulkMarkupReq, user=Depends(get_admin)):
    """Add req.percent on top of every product's seller price at once (market-wide)."""
    percent = max(0.0, req.percent)
    q = {"markup_percent": {"$exists": False}} if req.only_without_override else {}
    result = await db.products.update_many(q, {"$set": {"markup_percent": percent}})
    return {"ok": True, "updated": result.modified_count}


@api_router.post("/admin/categories")
async def admin_add_category(req: CategoryReq, user=Depends(get_admin)):
    c = {
        "id": uid(),
        "name": {"uz": req.name_uz, "ru": req.name_ru or req.name_uz, "en": req.name_en or req.name_uz},
        "icon": req.icon,
        "parent_id": req.parent_id,
        "order": req.order,
        "preview_image": req.preview_image,
    }
    await db.categories.insert_one(dict(c))
    # Invalidate categories cache so new category appears immediately
    _CATEGORIES_CACHE["data"] = None
    _CATEGORIES_CACHE["expires_at"] = 0.0
    return {k: v for k, v in c.items() if k != "_id"}


@api_router.put("/admin/categories/{cid}")
async def admin_update_category(cid: str, req: CategoryReq, user=Depends(get_admin)):
    existing = await db.categories.find_one({"id": cid}, {"_id": 0})
    if not existing:
        raise HTTPException(404, "Topilmadi")
    upd = {
        "name": {"uz": req.name_uz, "ru": req.name_ru or req.name_uz, "en": req.name_en or req.name_uz},
        "icon": req.icon,
        "parent_id": req.parent_id,
        "order": req.order,
    }
    # Only overwrite preview_image when client explicitly sends a value
    if req.preview_image is not None:
        upd["preview_image"] = req.preview_image
    await db.categories.update_one({"id": cid}, {"$set": upd})
    # Invalidate categories cache
    _CATEGORIES_CACHE["data"] = None
    _CATEGORIES_CACHE["expires_at"] = 0.0
    updated = await db.categories.find_one({"id": cid}, {"_id": 0})
    return updated


@api_router.post("/admin/categories/{cid}")
async def admin_update_category_post(cid: str, req: CategoryReq, user=Depends(get_admin)):
    """POST alias for PUT — works on hosts/proxies that block raw PUT."""
    return await admin_update_category(cid, req)


@api_router.delete("/admin/categories/{cid}")
async def admin_del_category(cid: str, user=Depends(get_admin)):
    await db.categories.delete_many({"$or": [{"id": cid}, {"parent_id": cid}]})
    _CATEGORIES_CACHE["data"] = None
    _CATEGORIES_CACHE["expires_at"] = 0.0
    return {"ok": True}


@api_router.get("/admin/orders")
async def admin_orders(
    status: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    user=Depends(get_admin),
):
    """Minimal projection — no $map on items (images caused empty results / memory errors)."""
    try:
        match: Dict[str, Any] = {}
        if status and status.strip():
            match["status"] = status.strip()
        lim = min(int(limit or 50), 100)
        sk = max(int(skip or 0), 0)
        pipeline = [
            {"$match": match} if match else {"$match": {}},
            {"$project": {
                "_id": 0,
                "id": 1,
                "number": 1,
                "group_id": 1,
                "status": 1,
                "total": 1,
                "subtotal": 1,
                "delivery_fee": 1,
                "discount": 1,
                "created_at": 1,
                "delivery_method": 1,
                "payment_method": 1,
                "address_text": 1,
                "client_id": 1,
                "client_name": 1,
                "client_phone": 1,
                "comment": 1,
                "promo_code": 1,
                "seller_id": 1,
                "courier_id": 1,
                "delivery_eta_days": 1,
                "seller_rejection": 1,
                "seller_payment_received_at": 1,
                "seller_payment_confirmed": 1,
                "has_returns": 1,
                "returned_items_count": 1,
                "items_count": {"$size": {"$ifNull": ["$items", []]}},
            }},
            {"$sort": {"created_at": -1}},
            {"$skip": sk},
            {"$limit": lim},
        ]
        raw = await db.orders.aggregate(pipeline, maxTimeMS=15000, allowDiskUse=True).to_list(lim)
        # attach empty items so UI does not crash on o.items.map
        for o in raw:
            o.setdefault("items", [])
        return json_safe(raw)
    except Exception as e:
        logger.exception("admin_orders failed: %s", e)
        # last-resort ultra-simple find
        try:
            q: Dict[str, Any] = {}
            if status and status.strip():
                q["status"] = status.strip()
            raw = await (
                db.orders.find(q, {"_id": 0, "id": 1, "number": 1, "status": 1, "total": 1,
                                   "created_at": 1, "client_name": 1, "client_phone": 1,
                                   "address_text": 1, "delivery_eta_days": 1,
                                   "seller_payment_received_at": 1, "seller_payment_confirmed": 1})
                .sort("created_at", -1)
                .max_time_ms(10000)
                .to_list(50)
            )
            for o in raw:
                o.setdefault("items", [])
            return json_safe(raw)
        except Exception as e2:
            logger.exception("admin_orders fallback failed: %s", e2)
            return []


@api_router.post("/admin/orders/{oid}/status")
async def admin_order_status(oid: str, req: StatusReq, user=Depends(get_admin)):
    o = await db.orders.find_one({"id": oid})
    if not o:
        raise HTTPException(404, "Topilmadi")
    await set_order_status(o, req.status, "Admin o'zgartirdi")
    return {"ok": True}


@api_router.post("/admin/orders/{oid}/eta")
async def admin_order_eta(oid: str, req: EtaReq, user=Depends(get_admin)):
    o = await db.orders.find_one({"id": oid}, {"_id": 0})
    if not o:
        raise HTTPException(404, "Topilmadi")
    eta_days = max(0, int(req.eta_days or 0))
    await db.orders.update_one({"id": oid}, {"$set": {"delivery_eta_days": eta_days, "delivery_eta_updated_at": iso(), "delivery_eta_updated_by": user["id"]}})
    await notify(o["client_id"], f"Buyurtma {o['number']}", f"Taxminiy yetib borish muddati: {eta_days} kun")
    return {"ok": True, "eta_days": eta_days}


@api_router.get("/admin/rejected-orders")
async def admin_rejected_orders(user=Depends(get_admin)):
    """To'liq rad (seller_rejected) + qisman rad (seller_rejected_items)."""
    try:
        full = await (
            db.orders.find({"status": "seller_rejected"}, {"_id": 0})
            .sort("created_at", -1)
            .limit(50)
            .to_list(50)
        )
        partial = await (
            db.orders.find(
                {
                    "status": {"$ne": "seller_rejected"},
                    "seller_rejected_items.0": {"$exists": True},
                    "seller_rejection.resolved_at": {"$exists": False},
                },
                {"_id": 0},
            )
            .sort("created_at", -1)
            .limit(50)
            .to_list(50)
        )
        result = []
        seen = set()
        for o in full:
            oid = o.get("id")
            if oid in seen:
                continue
            seen.add(oid)
            result.append(await build_rejected_order_payload(o, only_rejected_items=False))
        for o in partial:
            oid = o.get("id")
            if oid in seen:
                continue
            seen.add(oid)
            # qisman: faqat rad etilganlar
            payload = await build_rejected_order_payload(o, only_rejected_items=True)
            if payload.get("replacement_options"):
                result.append(payload)
        return json_safe(result)
    except Exception as e:
        logger.exception("admin_rejected_orders failed: %s", e)
        return []



@api_router.post("/admin/rejected-orders/{oid}/resolve")
async def admin_resolve_rejected_order(oid: str, req: ResolveRejectedOrderReq, user=Depends(get_admin)):
    order = await db.orders.find_one({"id": oid}, {"_id": 0})
    if not order:
        raise HTTPException(404, "Buyurtma topilmadi")
    await reassign_rejected_order(order, req, user)
    return {"ok": True}


@api_router.post("/admin/dashboard/reset-stats")
async def admin_reset_dashboard_stats(user=Depends(get_admin)):
    reset_at = iso()
    await db.settings.update_one({"id": "main"}, {"$set": {"dashboard_money_reset_at": reset_at, "dashboard_stats_reset_at": reset_at}}, upsert=True)
    admins = await db.users.find({"role": "admin"}, {"_id": 0}).to_list(20)
    for admin_u in admins:
        await notify(admin_u["id"], "Pul statistikasi 0 qilindi", "Admin paneldagi pul ko'rsatkichlari yangi hisobdan boshlanadi")
    return {"ok": True, "reset_at": reset_at}


@api_router.post("/admin/banners")
async def admin_add_banner(req: BannerReq, user=Depends(get_admin)):
    b = {"id": uid(), **req.dict(), "active": True, "order": 0, "created_at": iso()}
    await db.banners.insert_one(dict(b))
    return {k: v for k, v in b.items() if k != "_id"}


async def _admin_update_banner(bid: str, req: BannerReq):
    existing = await db.banners.find_one({"id": bid}, {"_id": 0})
    if not existing:
        raise HTTPException(404, "Topilmadi")
    # If client didn't send a new image, keep the existing one so
    # "just changing the title" doesn't wipe the banner picture.
    new_image = req.image
    if not new_image or not new_image.strip():
        new_image = existing.get("image")
    upd = {
        "image": new_image,
        "title": req.title,
        "link_type": req.link_type,
        "link_id": req.link_id,
        "expires_at": req.expires_at,
    }
    await db.banners.update_one({"id": bid}, {"$set": upd})
    updated = await db.banners.find_one({"id": bid}, {"_id": 0})
    return updated


@api_router.put("/admin/banners/{bid}")
async def admin_update_banner_put(bid: str, req: BannerReq, user=Depends(get_admin)):
    return await _admin_update_banner(bid, req)


@api_router.post("/admin/banners/{bid}")
async def admin_update_banner_post(bid: str, req: BannerReq, user=Depends(get_admin)):
    return await _admin_update_banner(bid, req)


@api_router.delete("/admin/banners/{bid}")
async def admin_del_banner(bid: str, user=Depends(get_admin)):
    await db.banners.delete_one({"id": bid})
    return {"ok": True}


@api_router.post("/admin/banners/{bid}/delete")
async def admin_del_banner_post_alias(bid: str, user=Depends(get_admin)):
    """POST alias for DELETE on hosts/proxies that block raw DELETE."""
    await db.banners.delete_one({"id": bid})
    return {"ok": True}


@api_router.get("/admin/promocodes")
async def admin_promos(user=Depends(get_admin)):
    try:
        raw = await db.promocodes.find({}, {"_id": 0}).max_time_ms(8000).to_list(100)
        return json_safe(raw)
    except Exception as e:
        logger.exception("admin_promos failed: %s", e)
        return []


@api_router.post("/admin/promocodes")
async def admin_add_promo(req: PromoCreateReq, user=Depends(get_admin)):
    p = {"id": uid(), "code": req.code.upper(), "type": req.type, "value": req.value, "min_cart": req.min_cart,
         "limit": req.limit, "used": 0, "expires_at": req.expires_at, "active": True, "created_at": iso()}
    await db.promocodes.insert_one(dict(p))
    return {k: v for k, v in p.items() if k != "_id"}


@api_router.delete("/admin/promocodes/{pid}")
async def admin_del_promo(pid: str, user=Depends(get_admin)):
    await db.promocodes.delete_one({"id": pid})
    return {"ok": True}


@api_router.post("/admin/promocodes/{pid}/delete")
async def admin_del_promo_post_alias(pid: str, user=Depends(get_admin)):
    await db.promocodes.delete_one({"id": pid})
    return {"ok": True}


@api_router.post("/admin/flash-sale")
async def admin_flash(req: FlashReq, user=Depends(get_admin)):
    await db.products.update_one({"id": req.product_id}, {"$set": {"flash_sale": {"price": req.price, "ends_at": iso(now() + timedelta(hours=req.hours))}}})
    return {"ok": True}


@api_router.get("/admin/reviews")
async def admin_reviews(user=Depends(get_admin)):
    try:
        raw = await db.reviews.find({}, {"_id": 0}).sort("created_at", -1).max_time_ms(8000).to_list(200)
        return json_safe(raw)
    except Exception as e:
        logger.exception("admin_reviews failed: %s", e)
        return []


@api_router.delete("/admin/reviews/{rid}")
async def admin_del_review(rid: str, user=Depends(get_admin)):
    await db.reviews.delete_one({"id": rid})
    return {"ok": True}


@api_router.get("/admin/sms-log")
async def admin_sms_log(user=Depends(get_admin)):
    try:
        raw = await db.sms_log.find({}, {"_id": 0}).sort("sent_at", -1).max_time_ms(8000).to_list(200)
        return json_safe(raw)
    except Exception as e:
        logger.exception("admin_sms_log failed: %s", e)
        return []


@api_router.get("/admin/settings")
async def admin_get_settings(user=Depends(get_admin)):
    try:
        return json_safe(await db.settings.find_one({"id": "main"}, {"_id": 0}) or {})
    except Exception as e:
        logger.exception("admin_settings failed: %s", e)
        return {}


@api_router.put("/admin/settings")
async def admin_set_settings(req: SettingsReq, user=Depends(get_admin)):
    upd = {k: v for k, v in req.dict().items() if v is not None}
    await db.settings.update_one({"id": "main"}, {"$set": upd}, upsert=True)
    s = await db.settings.find_one({"id": "main"}, {"_id": 0}) or {}
    SETTINGS_CACHE["default_markup_percent"] = s.get("default_markup_percent", 0) or 0
    SETTINGS_CACHE["default_delivery_eta_days"] = int(s.get("default_delivery_eta_days") or 0)
    SETTINGS_CACHE["kg_extra_markup_percent"] = float(s.get("kg_extra_markup_percent") or 0)
    return json_safe(s)


@api_router.post("/admin/couriers")
async def admin_add_courier(req: CourierCreateReq, user=Depends(get_admin)):
    phone = re.sub(r"[^\d+]", "", req.phone)
    if await db.users.find_one({"phone": phone}):
        raise HTTPException(400, "Bu raqam ro'yxatda bor")
    # Admin kuryer tizimda faqat bitta bo'lishi kerak
    if req.is_admin_courier:
        existing_ac = await db.users.find_one({
            "$or": [
                {"role": "admin_courier"},
                {"courier_info.is_admin_courier": True},
            ]
        })
        if existing_ac:
            raise HTTPException(400, "Admin kuryer allaqachon mavjud. Faqat bitta admin kuryer bo'lishi mumkin")
    role = "admin_courier" if req.is_admin_courier else "courier"
    c = {
        "id": uid(), "phone": phone, "first_name": req.first_name, "last_name": "", "role": role,
        "language": "uz", "blocked": False, "referral_code": f"UZ{random.randint(10000, 99999)}",
        "favorites": [], "addresses": [],
        "courier_info": {
            "online": False, "zone": req.zone, "earnings": 0, "deliveries": 0,
            "stats_reset_at": None, "is_admin_courier": bool(req.is_admin_courier),
        },
        "created_at": iso(),
    }
    await db.users.insert_one(dict(c))
    return {k: v for k, v in c.items() if k != "_id"}


@api_router.get("/settings/public")
async def public_settings():
    s = await db.settings.find_one({"id": "main"}, {"_id": 0}) or {}
    return {
        "delivery_fee": s.get("delivery_fee", 15000),
        "min_order": s.get("min_order", 0),
        "work_hours": s.get("work_hours", "09:00 - 21:00"),
        "contact": s.get("contact", "+998 71 200 00 00"),
        "default_delivery_eta_days": int(s.get("default_delivery_eta_days") or 0),
        "kg_extra_markup_percent": float(s.get("kg_extra_markup_percent") or 0),
    }


@api_router.get("/download/source")
async def download_source():
    from fastapi.responses import FileResponse
    path = "/app/uzmarket_source_final.zip"
    if not os.path.exists(path):
        raise HTTPException(404, "Zip topilmadi")
    return FileResponse(path, filename="uzmarket_source_final.zip", media_type="application/zip")


app.include_router(api_router)

app.add_middleware(MethodOverrideMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=500)
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- Seed ----------
IMG = {
    "phone": "https://images.unsplash.com/photo-1623824204241-f851d3bcfaf5?w=600&q=80",
    "phone2": "https://images.unsplash.com/photo-1610945265064-0e34e5519bbf?w=600&q=80",
    "laptop": "https://images.unsplash.com/photo-1496181133206-80ce9b88a853?w=600&q=80",
    "headphones": "https://images.unsplash.com/photo-1505740420928-5e560c06d30e?w=600&q=80",
    "watch": "https://images.unsplash.com/photo-1523275335684-37898b6baf30?w=600&q=80",
    "tshirt": "https://images.unsplash.com/photo-1521572163474-6864f9cf17ab?w=600&q=80",
    "sneakers": "https://images.unsplash.com/photo-1542291026-7eec264c27ff?w=600&q=80",
    "jacket": "https://images.unsplash.com/photo-1551028719-00167b16eac5?w=600&q=80",
    "sofa": "https://images.unsplash.com/photo-1555041469-a586c61ea9bc?w=600&q=80",
    "lamp": "https://images.unsplash.com/photo-1507473885765-e6ed057f782c?w=600&q=80",
    "kettle": "https://images.unsplash.com/photo-1594213114663-d94db9b17125?w=600&q=80",
    "perfume": "https://images.unsplash.com/photo-1541643600914-78b084683601?w=600&q=80",
    "cream": "https://images.unsplash.com/photo-1556228720-195a672e8a03?w=600&q=80",
    "ball": "https://images.unsplash.com/photo-1614632537190-23e4146777db?w=600&q=80",
    "dumbbell": "https://images.unsplash.com/photo-1517836357463-d25dfeac3438?w=600&q=80",
    "honey": "https://images.unsplash.com/photo-1587049352846-4a222e784d38?w=600&q=80",
    "nuts": "https://images.unsplash.com/photo-1508061253366-f7da158b6d46?w=600&q=80",
    "banner1": "https://images.unsplash.com/photo-1558769132-cb1aea458c5e?w=1200&q=80",
    "banner2": "https://images.unsplash.com/photo-1607082348824-0a96f2a4b9da?w=1200&q=80",
    "banner3": "https://images.unsplash.com/photo-1472851294608-062f824d29cc?w=1200&q=80",
}


REMINDER_TASK: Optional[asyncio.Task] = None


async def send_pending_order_reminders_once():
    cutoff = iso(now() - timedelta(hours=1))
    pending_orders = await db.orders.find({
        "status": "new",
        "created_at": {"$lte": cutoff},
        "seller_confirmation_reminder_sent_at": {"$exists": False},
    }, {"_id": 0}).to_list(500)
    if not pending_orders:
        return 0
    admins = await db.users.find({"role": "admin"}, {"_id": 0}).to_list(50)
    sent = 0
    for order in pending_orders:
        await notify(order["seller_id"], "Buyurtmani tasdiqlang", f"{order['number']} buyurtmasi 1 soatdan beri tasdiqlanmagan")
        for admin_u in admins:
            await notify(admin_u["id"], "Sotuvchi hali tasdiqlamadi", f"{order['number']} buyurtmasi hali ham tasdiqlanmagan")
        await db.orders.update_one({"id": order["id"]}, {"$set": {"seller_confirmation_reminder_sent_at": iso()}})
        sent += 1
    return sent


async def send_rejected_order_admin_reminders_once():
    admins = await db.users.find({"role": "admin"}, {"_id": 0}).to_list(50)
    orders = await db.orders.find({
        "status": "seller_rejected",
        "seller_rejection.reminder_due_at": {"$lte": iso()},
        "seller_rejection.admin_reminder_sent_at": {"$exists": False},
    }, {"_id": 0}).to_list(200)
    sent = 0
    for order in orders:
        for admin_u in admins:
            await notify(admin_u["id"], "Rad etilgan buyurtma kutilmoqda", f"{order['number']} bo'yicha 1 soat ichida javob berilmadi")
        await db.orders.update_one({"id": order["id"]}, {"$set": {"seller_rejection.admin_reminder_sent_at": iso()}})
        sent += 1
    return sent


async def reminder_worker():
    while True:
        try:
            await send_pending_order_reminders_once()
            await send_rejected_order_admin_reminders_once()
        except Exception:
            logger.exception("Pending-order reminder worker failed")
        await asyncio.sleep(300)


@app.on_event("startup")
async def ensure_indexes():
    """Create critical indexes once — biggest speed win for products/admin/orders."""
    try:
        await asyncio.gather(
            db.products.create_index("id", unique=True),
            db.products.create_index([("status", 1), ("hidden", 1), ("created_at", -1)]),
            db.products.create_index([("seller_id", 1), ("created_at", -1)]),
            db.products.create_index([("category_id", 1), ("status", 1)]),
            db.products.create_index([("subcategory_id", 1), ("status", 1)]),
            db.products.create_index([("pinned", -1), ("sold", -1)]),
            db.products.create_index([("flash_sale.ends_at", 1)]),
            db.users.create_index("id", unique=True),
            db.users.create_index("phone", unique=True),
            db.users.create_index([("role", 1)]),
            db.users.create_index([("seller_info.approved", 1)]),
            db.orders.create_index("id", unique=True),
            db.orders.create_index([("created_at", -1)]),
            db.orders.create_index([("status", 1), ("created_at", -1)]),
            # Orders store client_id (not user_id) — index must match the real field
            db.orders.create_index([("client_id", 1), ("created_at", -1)]),
            db.orders.create_index([("seller_id", 1), ("created_at", -1)]),
            # Courier queries filter by courier_id
            db.orders.create_index([("courier_id", 1), ("created_at", -1)]),
            db.orders.create_index([("status", 1), ("courier_id", 1), ("delivery_method", 1)]),
            db.categories.create_index("id", unique=True),
            db.categories.create_index([("order", 1)]),
            db.notifications.create_index([("user_id", 1), ("created_at", -1)]),
            db.banners.create_index([("active", 1), ("order", 1)]),
            return_exceptions=True,
        )
        logger.info("MongoDB indexes ensured")
    except Exception as e:
        logger.warning("Index creation warning: %s", e)


@app.on_event("startup")
async def load_settings_cache():
    s = await db.settings.find_one({"id": "main"}) or {}
    SETTINGS_CACHE["default_markup_percent"] = s.get("default_markup_percent", 0) or 0


@app.on_event("startup")
async def start_reminder_worker():
    global REMINDER_TASK
    if REMINDER_TASK is None or REMINDER_TASK.done():
        REMINDER_TASK = asyncio.create_task(reminder_worker())


@app.on_event("startup")
async def ensure_admin_courier_account():
    """Mavjud DB da ham yagona admin kuryer bo'lishini kafolatlaydi."""
    try:
        existing = await db.users.find_one({
            "$or": [
                {"role": "admin_courier"},
                {"courier_info.is_admin_courier": True},
            ]
        })
        if existing:
            # role va flag sinxron
            await db.users.update_one(
                {"id": existing["id"]},
                {"$set": {"role": "admin_courier", "courier_info.is_admin_courier": True}},
            )
            return
        phone = "+998906666666"
        if await db.users.find_one({"phone": phone}):
            await db.users.update_one(
                {"phone": phone},
                {"$set": {
                    "role": "admin_courier",
                    "courier_info.is_admin_courier": True,
                    "courier_info.zone": "Toshkent",
                }},
            )
            logger.info("Existing user %s promoted to admin_courier", phone)
            return
        u = {
            "id": uid(), "phone": phone, "first_name": "Admin", "last_name": "Kuryer",
            "role": "admin_courier", "language": "uz", "blocked": False,
            "referral_code": f"UZ{random.randint(10000, 99999)}",
            "favorites": [], "addresses": [],
            "courier_info": {
                "online": True, "zone": "Toshkent", "earnings": 0, "deliveries": 0,
                "stats_reset_at": None, "is_admin_courier": True,
            },
            "created_at": iso(),
        }
        await db.users.insert_one(dict(u))
        logger.info("Admin courier account created: %s", phone)
    except Exception as e:
        logger.warning("ensure_admin_courier_account: %s", e)


@app.on_event("startup")
async def seed():
    """Faqat asosiy admin + sozlamalar. Demo sotuvchi/mahsulot/buyurtma YO'Q."""
    try:
        ADMIN_PHONE = "+998902149795"
        # Eski demo admin raqamini yangisiga ko'chirish (bir marta)
        old = await db.users.find_one({"phone": "+998900000000", "role": "admin"})
        if old and not await db.users.find_one({"phone": ADMIN_PHONE}):
            await db.users.update_one({"id": old["id"]}, {"$set": {"phone": ADMIN_PHONE}})
            logger.info("Admin phone migrated to %s", ADMIN_PHONE)

        admin = await db.users.find_one({"phone": ADMIN_PHONE})
        if not admin:
            admin = {
                "id": uid(),
                "phone": ADMIN_PHONE,
                "first_name": "Admin",
                "last_name": "Boshqaruvchi",
                "role": "admin",
                "language": "uz",
                "blocked": False,
                "referral_code": f"UZ{random.randint(10000, 99999)}",
                "favorites": [],
                "addresses": [],
                "created_at": iso(),
            }
            await db.users.insert_one(dict(admin))
            logger.info("Admin account created: %s", ADMIN_PHONE)
        else:
            # role kafolati
            if admin.get("role") != "admin":
                await db.users.update_one({"id": admin["id"]}, {"$set": {"role": "admin"}})

        # Demo test akkauntlarni o'chirish (static seed qoldiqlari)
        DEMO_PHONES = [
            "+998900000000",
            "+998901111111",
            "+998902222222",
            "+998903333333",
            "+998904444444",
            "+998905555555",
        ]
        demo_users = await db.users.find({"phone": {"$in": DEMO_PHONES}}, {"_id": 0, "id": 1, "phone": 1}).to_list(50)
        demo_ids = [u["id"] for u in demo_users]
        if demo_ids:
            await db.users.delete_many({"id": {"$in": demo_ids}})
            await db.products.delete_many({"seller_id": {"$in": demo_ids}})
            await db.orders.delete_many({
                "$or": [
                    {"seller_id": {"$in": demo_ids}},
                    {"client_id": {"$in": demo_ids}},
                    {"courier_id": {"$in": demo_ids}},
                ]
            })
            await db.reviews.delete_many({"client_id": {"$in": demo_ids}})
            logger.info("Removed %s demo users and related data: %s", len(demo_ids), [u["phone"] for u in demo_users])

        # Minimal sozlamalar (yo'q bo'lsa)
        if not await db.settings.find_one({"id": "main"}):
            await db.settings.update_one(
                {"id": "main"},
                {"$set": {
                    "id": "main",
                    "delivery_fee": 15000,
                    "min_order": 0,
                    "commission_default": 10,
                    "default_markup_percent": 0,
                    "default_delivery_eta_days": 2,
                    "work_hours": "09:00 - 21:00",
                    "contact": "+998902149795",
                }},
                upsert=True,
            )
            logger.info("Default settings created")

        logger.info("Seed done (no demo catalog)")
    except Exception as e:
        logger.exception("seed failed: %s", e)


@app.on_event("shutdown")
async def shutdown_db_client():
    global REMINDER_TASK
    if REMINDER_TASK and not REMINDER_TASK.done():
        REMINDER_TASK.cancel()
        try:
            await REMINDER_TASK
        except asyncio.CancelledError:
            pass
    client.close()