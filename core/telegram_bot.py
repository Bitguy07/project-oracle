"""
core/telegram_bot.py
Telegram Bot — Command & Control (C2) interface for Project Oracle.

Commands:
  /post <topic>        — Add a topic to the Reel queue
  /feed <topic>        — Add a topic to the Feed queue
  /status              — Show quota usage and queue
  /queue               — List pending topics
  /clear               — Clear the topic queue
  /now <topic>         — Run pipeline immediately (skips queue)

Webhook vs Polling:
  - In GitHub Actions: use /set_webhook to configure Telegram to POST to your
    Actions webhook URL, then handle the single update in the action run.
  - In local dev: use long-polling mode (start_polling).
"""

import asyncio
import json
import logging
import os
from typing import Optional

import httpx

log = logging.getLogger("oracle.telegram")

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


class TelegramBot:
    def __init__(self):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID")  # Your personal chat ID
        if not self.token:
            raise EnvironmentError("TELEGRAM_BOT_TOKEN not set.")

    # ── Core API ───────────────────────────────────────────────────────────────

    async def send_message(self, text: str, chat_id: Optional[str] = None) -> dict:
        cid = chat_id or self.chat_id
        url = TELEGRAM_API.format(token=self.token, method="sendMessage")
        payload = {
            "chat_id": cid,
            "text": text,
            "parse_mode": "HTML",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(url, json=payload)
            return r.json()

    async def get_updates(self, offset: int = 0, timeout: int = 30) -> list:
        url = TELEGRAM_API.format(token=self.token, method="getUpdates")
        params = {"offset": offset, "timeout": timeout, "allowed_updates": ["message"]}
        async with httpx.AsyncClient(timeout=timeout + 5) as client:
            r = await client.get(url, params=params)
            data = r.json()
            return data.get("result", [])

    async def set_webhook(self, webhook_url: str) -> dict:
        url = TELEGRAM_API.format(token=self.token, method="setWebhook")
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(url, json={"url": webhook_url})
            return r.json()

    async def delete_webhook(self) -> dict:
        url = TELEGRAM_API.format(token=self.token, method="deleteWebhook")
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(url, json={})
            return r.json()

    # ── Command Handler ────────────────────────────────────────────────────────

    async def handle_update(self, update: dict) -> Optional[str]:
        """
        Process a single Telegram update. Returns reply text.
        Called both from webhook handler and polling loop.
        """
        from core.state_manager import StateManager
        state = StateManager()

        msg = update.get("message", {})
        text = msg.get("text", "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))

        if not text or not chat_id:
            return None

        # Security: only respond to your own chat ID
        if self.chat_id and chat_id != str(self.chat_id):
            log.warning(f"Ignoring message from unknown chat_id: {chat_id}")
            await self.send_message("⛔ Unauthorized.", chat_id)
            return None

        log.info(f"Received command: '{text}'")

        # ── Command routing ───────────────────────────────────────────────────
        if text.startswith("/post "):
            topic = text[6:].strip()
            if not topic:
                reply = "❌ Usage: /post <topic>"
            else:
                item_id = state.add_to_queue(topic, "reel")
                reply = f"✅ Added to Reel queue!\nTopic: <b>{topic}</b>\nID: {item_id}"

        elif text.startswith("/feed "):
            topic = text[6:].strip()
            if not topic:
                reply = "❌ Usage: /feed <topic>"
            else:
                item_id = state.add_to_queue(topic, "feed")
                reply = f"✅ Added to Feed queue!\nTopic: <b>{topic}</b>\nID: {item_id}"

        elif text == "/status":
            stats = state.get_stats()
            last = stats.get("last_post")
            last_str = f"\n🕐 Last: <b>{last['topic']}</b> ({last['date'][:10]})" if last else ""
            reply = (
                f"📊 <b>Project Oracle Status</b>\n"
                f"{'─'*28}\n"
                f"🤖 Gemini: {stats['gemini_used']}/{stats['gemini_limit']} calls\n"
                f"🖼 Images: {stats['images_used']}/{stats['image_limit']}\n"
                f"📋 Queue: {stats['queue_length']} topic(s)\n"
                f"📸 Total posts: {stats['total_posts']}"
                f"{last_str}\n"
                f"🔄 Resets: {stats['reset_date']}"
            )

        elif text == "/queue":
            queue = state.get_topic_queue()
            if not queue:
                reply = "📭 Queue is empty."
            else:
                lines = [f"📋 <b>Topic Queue ({len(queue)} items)</b>"]
                for i, item in enumerate(queue, 1):
                    lines.append(f"{i}. [{item['type']}] {item['topic']} <code>({item['id']})</code>")
                reply = "\n".join(lines)

        elif text == "/clear":
            state._state["topic_queue"] = []
            state._save()
            reply = "🗑️ Queue cleared."

        elif text.startswith("/now "):
            topic = text[5:].strip()
            if not topic:
                reply = "❌ Usage: /now <topic>"
            else:
                reply = f"🚀 Starting pipeline for: <b>{topic}</b>\nCheck back in ~2 minutes..."
                await self.send_message(reply, chat_id)
                # Run pipeline directly (not as background task)
                await self._run_and_notify(topic, chat_id)
                return reply

        elif text == "/help":
            reply = (
                "🤖 <b>Project Oracle Commands</b>\n"
                "─────────────────────────\n"
                "/post &lt;topic&gt; — Add Reel to queue\n"
                "/feed &lt;topic&gt; — Add Feed post to queue\n"
                "/now &lt;topic&gt; — Post immediately\n"
                "/status — Show quota & stats\n"
                "/queue — View pending topics\n"
                "/clear — Clear the queue\n"
                "/help — Show this message"
            )

        else:
            reply = "❓ Unknown command. Try /help"

        await self.send_message(reply, chat_id)
        return reply

    async def _run_and_notify(self, topic: str, chat_id: str):
        """Run pipeline and notify via Telegram when done."""
        import main as oracle_main
        try:
            result = await oracle_main.run_pipeline(topic)
            if result["status"] == "success":
                msg = f"✅ Posted! Topic: <b>{topic}</b>\nIG ID: <code>{result.get('ig_post_id')}</code>"
            else:
                msg = f"⚠️ Result: {result['status']} for topic: <b>{topic}</b>"
        except Exception as e:
            msg = f"❌ Pipeline error: {str(e)[:200]}"

        await self.send_message(msg, chat_id)

    # ── Webhook Handler (for GitHub Actions) ──────────────────────────────────

    async def handle_webhook_payload(self, payload: dict):
        """
        Call this from your GitHub Actions webhook trigger step.
        Reads the Telegram update from the env var TELEGRAM_UPDATE.
        """
        await self.handle_update(payload)

    # ── Long Polling (for local dev) ───────────────────────────────────────────

    async def start_polling(self):
        """Start long-polling loop. For local development only."""
        log.info("Starting Telegram long-polling...")
        offset = 0
        while True:
            try:
                updates = await self.get_updates(offset=offset, timeout=30)
                for update in updates:
                    offset = update["update_id"] + 1
                    await self.handle_update(update)
            except Exception as e:
                log.error(f"Polling error: {e}")
                await asyncio.sleep(5)
