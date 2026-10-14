"""Alerts: always logged to stdout; optionally pushed to Telegram or Discord. Never raises."""

from __future__ import annotations

import logging

import httpx

from core.config import env

log = logging.getLogger("alerts")


class Notifier:
    def __init__(self, backend: str = "stdout"):
        self.backend = backend
        self._http = httpx.AsyncClient(timeout=10)
        if backend == "telegram" and not (env("TELEGRAM_BOT_TOKEN") and env("TELEGRAM_CHAT_ID")):
            log.warning("telegram alerts configured but TELEGRAM_BOT_TOKEN/CHAT_ID missing; stdout only")
            self.backend = "stdout"
        if backend == "discord" and not env("DISCORD_WEBHOOK_URL"):
            log.warning("discord alerts configured but DISCORD_WEBHOOK_URL missing; stdout only")
            self.backend = "stdout"

    async def send(self, level: str, message: str) -> None:
        log.log(logging.getLevelName(level.upper()) if isinstance(logging.getLevelName(level.upper()), int)
                else logging.INFO, message)
        text = f"[{level.upper()}] {message}"
        try:
            if self.backend == "telegram":
                await self._http.post(
                    f"https://api.telegram.org/bot{env('TELEGRAM_BOT_TOKEN')}/sendMessage",
                    json={"chat_id": env("TELEGRAM_CHAT_ID"), "text": text[:4000]},
                )
            elif self.backend == "discord":
                await self._http.post(env("DISCORD_WEBHOOK_URL"), json={"content": text[:1900]})
        except Exception as e:  # alerting must never take the system down
            log.error("alert delivery failed: %s", e)

    async def aclose(self) -> None:
        await self._http.aclose()
