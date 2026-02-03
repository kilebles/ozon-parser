import asyncio
from pathlib import Path

import httpx

from app.logging_config import get_logger
from app.settings import settings

logger = get_logger(__name__)


class TelegramNotifier:
    """Send notifications to Telegram."""

    def __init__(self) -> None:
        self.bot_token = settings.bot_token
        self._chat_id: str | None = settings.telegram_chat_id or None
        self._base_url = f"https://api.telegram.org/bot{self.bot_token}"

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token)

    async def _get_chat_id(self) -> str | None:
        """Get chat_id from the first message sent to the bot."""
        if self._chat_id:
            return self._chat_id

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{self._base_url}/getUpdates")
                data = response.json()

                if data.get("ok") and data.get("result"):
                    # Get chat_id from the most recent message
                    for update in reversed(data["result"]):
                        if "message" in update:
                            self._chat_id = str(update["message"]["chat"]["id"])
                            logger.info(f"Auto-detected Telegram chat_id: {self._chat_id}")
                            return self._chat_id

                logger.warning(
                    "No messages found. Send any message to the bot first to enable notifications."
                )
        except Exception as e:
            logger.error(f"Failed to get Telegram chat_id: {e}")

        return None

    async def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a text message."""
        if not self.enabled:
            return False

        chat_id = await self._get_chat_id()
        if not chat_id:
            return False

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
                if response.status_code == 200:
                    return True
                logger.error(f"Telegram API error: {response.text}")
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")

        return False

    async def send_photo(
        self, photo: bytes | Path, caption: str | None = None
    ) -> bool:
        """Send a photo with optional caption."""
        if not self.enabled:
            return False

        chat_id = await self._get_chat_id()
        if not chat_id:
            return False

        try:
            if isinstance(photo, Path):
                photo = photo.read_bytes()

            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    f"{self._base_url}/sendPhoto",
                    data={"chat_id": chat_id, "caption": caption or ""},
                    files={"photo": ("screenshot.png", photo, "image/png")},
                )
                if response.status_code == 200:
                    return True
                logger.error(f"Telegram API error: {response.text}")
        except Exception as e:
            logger.error(f"Failed to send Telegram photo: {e}")

        return False

    def send_message_sync(self, text: str) -> bool:
        """Synchronous wrapper for send_message."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're in an async context, create a task
                asyncio.create_task(self.send_message(text))
                return True
            return loop.run_until_complete(self.send_message(text))
        except RuntimeError:
            # No event loop, create one
            return asyncio.run(self.send_message(text))

    def send_photo_sync(self, photo: bytes | Path, caption: str | None = None) -> bool:
        """Synchronous wrapper for send_photo."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(self.send_photo(photo, caption))
                return True
            return loop.run_until_complete(self.send_photo(photo, caption))
        except RuntimeError:
            return asyncio.run(self.send_photo(photo, caption))


# Global instance
_notifier: TelegramNotifier | None = None


def get_telegram_notifier() -> TelegramNotifier:
    global _notifier
    if _notifier is None:
        _notifier = TelegramNotifier()
    return _notifier
