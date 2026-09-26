"""
simple_vpn_bot.py — один файл, ничего больше не нужно.

Установка:
    pip install aiogram aiohttp qrcode[pil] Pillow

Запуск:
    python simple_vpn_bot.py
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
from urllib.parse import quote

import aiohttp
import qrcode
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, Message

# ======================= НАСТРОЙКИ =======================
BOT_TOKEN = "8895375582:AAH5IyyJeuOmVJaHtiWZjVsgBIDtdqT0MXo"

H1_API_URL   = "http://us3.h1cloud.net:25497/api"
H1_API_TOKEN = "36ff3a283c124bbfad97a81fe9e7dcdb1499926463da4c7bbc9f1a3c4e072c8f"
H1_VERIFY_SSL = False

# Публичный адрес подписки. {uuid} подставится автоматически.
SUB_PUBLIC_URL = "http://us3.h1cloud.net:25497/sub/{uuid}"

SUB_DAYS         = 30   # срок подписки в днях
SUB_TRAFFIC_GB   = 0    # 0 = без лимита
SUB_DEVICES      = 0    # 0 = без лимита устройств
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("simple_vpn_bot")

if "ВСТАВЬ" in BOT_TOKEN or "ВСТАВЬ" in H1_API_TOKEN:
    raise SystemExit("Заполни BOT_TOKEN и H1_API_TOKEN в блоке НАСТРОЙКИ")


# ---------- H1Cloud API client ----------
def sanitize_name(value: str, fallback: str = "client") -> str:
    """Узел принимает только [A-Za-z0-9._-] в именах клиентов."""
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", (value or "").strip())
    text = re.sub(r"_+", "_", text).strip("._-")
    return (text or fallback)[:64]


class H1CloudError(Exception):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


class H1CloudClient:
    def __init__(self, base_url: str, token: str, *, verify_ssl: bool = False, timeout: int = 20):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._ssl = bool(verify_ssl)
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self._token}",
                }
            )
        return self._session

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        session = await self._get_session()
        url = f"{self._base_url}{path}"
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        try:
            async with session.request(method, url, ssl=self._ssl, timeout=timeout, **kwargs) as resp:
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise H1CloudError(f"узел недоступен: {exc}", code="unreachable") from exc
        except Exception as exc:  # noqa: BLE001
            raise H1CloudError("некорректный ответ узла", code="bad_response") from exc

        if not isinstance(data, dict):
            raise H1CloudError("некорректный ответ узла", code="bad_response")
        if not data.get("ok", False):
            code = str(data.get("error") or "unknown")
            raise H1CloudError(f"узел вернул ошибку: {code}", code=code)
        return data

    async def get_client(self, name: str) -> dict | None:
        safe = sanitize_name(name)
        try:
            data = await self._request("GET", f"/clients/{quote(safe, safe='')}")
        except H1CloudError as exc:
            if exc.code in ("user_not_found", "not_found"):
                return None
            raise
        client = data.get("client")
        return client if isinstance(client, dict) else data

    async def create_client(
        self,
        name: str,
        days: int,
        *,
        traffic_limit_gb: int = 0,
        device_limit: int = 0,
    ) -> dict:
        payload: dict = {"name": sanitize_name(name), "days": max(1, days)}
        if traffic_limit_gb > 0:
            payload["traffic_limit_gb"] = traffic_limit_gb
        if device_limit > 0:
            payload["device_limit"] = device_limit
        return await self._request("POST", "/create", json=payload)

    async def get_links(self, name: str) -> list[str]:
        client = await self.get_client(name)
        if not client:
            return []
        result: list[str] = []
        links = client.get("links")
        if isinstance(links, dict):
            result.extend(v for v in links.values() if isinstance(v, str) and v.startswith("vless://"))
        elif isinstance(links, list):
            result.extend(v for v in links if isinstance(v, str) and v.startswith("vless://"))
        link = client.get("link")
        if isinstance(link, str) and link.startswith("vless://") and link not in result:
            result.append(link)
        return result

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


h1 = H1CloudClient(H1_API_URL, H1_API_TOKEN, verify_ssl=H1_VERIFY_SSL)


# ---------- helpers ----------
def make_qr(data: str) -> bytes:
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def vpn_name_for(telegram_id: int) -> str:
    return sanitize_name(f"tg{telegram_id}_vpn")


def subscription_url(uuid: str | None, vpn_name: str) -> str:
    if not SUB_PUBLIC_URL:
        return ""
    return SUB_PUBLIC_URL.replace("{uuid}", uuid or vpn_name)


# ---------- bot ----------
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


HELP_TEXT = (
    "📖 <b>Что умеет бот</b>\n\n"
    "/start — выдать (или показать уже созданную) VPN-подписку\n"
    "/vpn   — то же самое (повторно)\n"
    "/help  — эта справка\n\n"
    "После /start ты получишь:\n"
    "• ссылку-подписку (её нужно вставить в клиент — v2rayNG, Streisand, FoXray, Hiddify и т.п.)\n"
    "• QR-код для быстрого добавления\n"
    "• прямым текстом VLESS-ключ(и) на случай, если клиент не умеет подписки"
)


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        f"👋 Привет, {message.from_user.full_name}!\n\n"
        "Сейчас выдам твою VPN-подписку. Это займёт пару секунд…"
    )
    try:
        await issue_subscription(message)
    except H1CloudError as exc:
        log.warning("H1Cloud error for %s: %s", message.from_user.id, exc)
        await message.answer(f"❌ Не удалось выдать подписку: <code>{exc}</code>")


@dp.message(Command("vpn"))
async def cmd_vpn(message: Message) -> None:
    await message.answer("⏳ Достаю твою подписку…")
    try:
        await issue_subscription(message)
    except H1CloudError as exc:
        log.warning("H1Cloud error for %s: %s", message.from_user.id, exc)
        await message.answer(f"❌ Не удалось получить подписку: <code>{exc}</code>")


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


async def issue_subscription(message: Message) -> None:
    telegram_id = message.from_user.id
    vpn_name = vpn_name_for(telegram_id)

    client = await h1.get_client(vpn_name)
    if client is None:
        log.info("Creating client %s for telegram_id=%s", vpn_name, telegram_id)
        await h1.create_client(
            vpn_name,
            SUB_DAYS,
            traffic_limit_gb=SUB_TRAFFIC_GB,
            device_limit=SUB_DEVICES,
        )
        client = await h1.get_client(vpn_name)
    else:
        log.info("Reusing existing client %s for telegram_id=%s", vpn_name, telegram_id)

    uuid = str(client.get("uuid")) if client else None
    links = await h1.get_links(vpn_name)
    sub_url = subscription_url(uuid, vpn_name)

    lines: list[str] = ["✅ <b>Твоя VPN-подписка готова!</b>", ""]
    if sub_url:
        lines.append("🔗 <b>Ссылка подписки:</b>")
        lines.append(f"<code>{sub_url}</code>")
    if links:
        lines.append("")
        lines.append("🔑 <b>VLESS-ключ(и):</b>")
        for link in links:
            lines.append(f"<code>{link}</code>")
    lines.append("")
    lines.append(
        "📱 Импортируй ссылку-подписку или ключ в клиент "
        "(v2rayNG, Streisand, FoXray, Hiddify и т.п.)."
    )
    await message.answer("\n".join(lines))

    qr_source = sub_url or (links[0] if links else None)
    if qr_source:
        try:
            await message.answer_photo(
                BufferedInputFile(make_qr(qr_source), filename="vpn_qr.png"),
                caption="📷 QR-код для быстрого добавления",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("QR send failed: %s", exc)


async def main() -> None:
    log.info("Bot started")
    try:
        await dp.start_polling(bot)
    finally:
        await h1.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())