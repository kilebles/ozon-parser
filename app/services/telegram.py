import asyncio
from pathlib import Path

import httpx

from app.logging_config import get_logger
from app.settings import settings

logger = get_logger(__name__)


class TelegramNotifier:
    """Send notifications to all users who started the bot."""

    def __init__(self) -> None:
        self.bot_token = settings.bot_token
        self._base_url = f"https://api.telegram.org/bot{self.bot_token}"
        self._chat_ids: set[int] = set()

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token)

    async def _fetch_chat_ids(self) -> set[int]:
        """Get all chat_ids from users who messaged the bot."""
        if not self.enabled:
            return set()

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{self._base_url}/getUpdates")
                data = response.json()

                if data.get("ok") and data.get("result"):
                    for update in data["result"]:
                        if "message" in update:
                            chat_id = update["message"]["chat"]["id"]
                            self._chat_ids.add(chat_id)

                if not self._chat_ids:
                    logger.warning(
                        "No users found. Someone needs to /start the bot first."
                    )
        except Exception as e:
            logger.error(f"Failed to fetch Telegram chat_ids: {e}")

        return self._chat_ids

    async def _send_to_chat(
        self, chat_id: int, text: str, parse_mode: str = "HTML"
    ) -> bool:
        """Send message to a specific chat."""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{self._base_url}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": parse_mode,
                    },
                )
                return response.status_code == 200
        except Exception as e:
            logger.error(f"Failed to send to {chat_id}: {e}")
            return False

    async def _send_photo_to_chat(
        self, chat_id: int, photo: bytes, caption: str | None = None
    ) -> bool:
        """Send photo to a specific chat."""
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    f"{self._base_url}/sendPhoto",
                    data={
                        "chat_id": chat_id,
                        "caption": caption or "",
                        "parse_mode": "HTML",
                    },
                    files={"photo": ("screenshot.png", photo, "image/png")},
                )
                return response.status_code == 200
        except Exception as e:
            logger.error(f"Failed to send photo to {chat_id}: {e}")
            return False

    async def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a text message to all users."""
        if not self.enabled:
            return False

        chat_ids = await self._fetch_chat_ids()
        if not chat_ids:
            return False

        results = await asyncio.gather(
            *[self._send_to_chat(cid, text, parse_mode) for cid in chat_ids]
        )
        return any(results)

    async def send_photo(
        self, photo: bytes | Path, caption: str | None = None
    ) -> bool:
        """Send a photo to all users."""
        if not self.enabled:
            return False

        chat_ids = await self._fetch_chat_ids()
        if not chat_ids:
            return False

        if isinstance(photo, Path):
            photo = photo.read_bytes()

        results = await asyncio.gather(
            *[self._send_photo_to_chat(cid, photo, caption) for cid in chat_ids]
        )
        return any(results)


# Global instance
_notifier: TelegramNotifier | None = None


def get_telegram_notifier() -> TelegramNotifier:
    global _notifier
    if _notifier is None:
        _notifier = TelegramNotifier()
    return _notifier
