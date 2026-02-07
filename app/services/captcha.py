"""
RuCaptcha integration for automatic captcha solving.

Supports:
- reCAPTCHA v2 (visible and invisible)
- Image captcha (text recognition)
"""

import asyncio

import httpx

from app.logging_config import get_logger
from app.settings import settings

logger = get_logger(__name__)


class CaptchaSolverError(Exception):
    """Raised when captcha solving fails."""
    pass


class RuCaptchaSolver:
    """
    Async client for RuCaptcha API.

    API docs: https://rucaptcha.com/api-docs
    """

    BASE_URL = "https://rucaptcha.com"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or settings.ru_captcha_api_key
        if not self.api_key:
            raise CaptchaSolverError(
                "RuCaptcha API key not configured. "
                "Set RUCAPTCHA_API_KEY in .env file."
            )

    async def solve_recaptcha_v2(
        self,
        site_key: str,
        page_url: str,
        invisible: bool = False,
    ) -> str:
        """
        Solve reCAPTCHA v2.

        Args:
            site_key: The site key from reCAPTCHA element (data-sitekey)
            page_url: URL of the page with captcha
            invisible: True if invisible reCAPTCHA

        Returns:
            The solved captcha token (g-recaptcha-response)
        """
        logger.info(f"Solving reCAPTCHA v2 for {page_url}")

        # Submit captcha task
        async with httpx.AsyncClient(timeout=30) as client:
            submit_data = {
                "key": self.api_key,
                "method": "userrecaptcha",
                "googlekey": site_key,
                "pageurl": page_url,
                "json": 1,
            }
            if invisible:
                submit_data["invisible"] = 1

            response = await client.post(
                f"{self.BASE_URL}/in.php",
                data=submit_data,
            )
            result = response.json()

            if result.get("status") != 1:
                error = result.get("request", "Unknown error")
                raise CaptchaSolverError(f"Failed to submit captcha: {error}")

            task_id = result["request"]
            logger.info(f"Captcha task submitted, ID: {task_id}")

        # Poll for result
        return await self._poll_result(task_id)

    async def solve_turnstile(
        self,
        site_key: str,
        page_url: str,
    ) -> str:
        """
        Solve Cloudflare Turnstile captcha.

        Args:
            site_key: The site key from Turnstile element (data-sitekey)
            page_url: URL of the page with captcha

        Returns:
            The solved captcha token
        """
        logger.info(f"Solving Cloudflare Turnstile for {page_url}")

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.BASE_URL}/in.php",
                data={
                    "key": self.api_key,
                    "method": "turnstile",
                    "sitekey": site_key,
                    "pageurl": page_url,
                    "json": 1,
                },
            )
            result = response.json()

            if result.get("status") != 1:
                error = result.get("request", "Unknown error")
                raise CaptchaSolverError(f"Failed to submit Turnstile: {error}")

            task_id = result["request"]
            logger.info(f"Turnstile task submitted, ID: {task_id}")

        return await self._poll_result(task_id)

    async def solve_image_captcha(self, image_base64: str) -> str:
        """
        Solve image captcha (text recognition).

        Args:
            image_base64: Base64 encoded image

        Returns:
            Recognized text from the image
        """
        logger.info("Solving image captcha")

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.BASE_URL}/in.php",
                data={
                    "key": self.api_key,
                    "method": "base64",
                    "body": image_base64,
                    "json": 1,
                },
            )
            result = response.json()

            if result.get("status") != 1:
                error = result.get("request", "Unknown error")
                raise CaptchaSolverError(f"Failed to submit image captcha: {error}")

            task_id = result["request"]
            logger.info(f"Image captcha task submitted, ID: {task_id}")

        return await self._poll_result(task_id)

    async def _poll_result(
        self,
        task_id: str,
        max_attempts: int = 60,
        poll_interval: float = 2.0,
    ) -> str:
        """
        Poll for captcha solution.

        Args:
            task_id: Task ID from submission
            max_attempts: Maximum polling attempts (default 60 = 2 minutes)
            poll_interval: Seconds between polls

        Returns:
            Solved captcha response
        """
        async with httpx.AsyncClient(timeout=30) as client:
            for attempt in range(max_attempts):
                await asyncio.sleep(poll_interval)

                response = await client.get(
                    f"{self.BASE_URL}/res.php",
                    params={
                        "key": self.api_key,
                        "action": "get",
                        "id": task_id,
                        "json": 1,
                    },
                )
                result = response.json()

                if result.get("status") == 1:
                    solution = result["request"]
                    logger.info(f"Captcha solved after {attempt + 1} attempts")
                    return solution

                error = result.get("request", "")
                if error == "CAPCHA_NOT_READY":
                    logger.debug(f"Captcha not ready, attempt {attempt + 1}/{max_attempts}")
                    continue

                # Real error
                raise CaptchaSolverError(f"Captcha solving failed: {error}")

        raise CaptchaSolverError(f"Captcha solving timeout after {max_attempts} attempts")

    async def get_balance(self) -> float:
        """Get current account balance in rubles."""
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{self.BASE_URL}/res.php",
                params={
                    "key": self.api_key,
                    "action": "getbalance",
                    "json": 1,
                },
            )
            result = response.json()

            if result.get("status") != 1:
                error = result.get("request", "Unknown error")
                raise CaptchaSolverError(f"Failed to get balance: {error}")

            return float(result["request"])
