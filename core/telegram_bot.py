"""
core/telegram_bot.py — Project Oracle C2

Commands:
  /now                              — Autonomous post (AI picks everything)
  /now-reel [topic:<...>, (music:<...>)]  — Manual reel with free-form input
  /now-feed [topic:<...>, (music:<...>)]  — Manual feed with free-form input
  /post <topic>                     — Add to reel queue (legacy, still works)
  /feed <topic>                     — Add to feed queue (legacy)
  /status                           — Quota, mode, stats
  /queue                            — View queue
  /clear                            — Clear queue
  /done <id>                        — Approve pending video → post to Instagram
  /no <id>                          — Reject and discard
  /custom                           — Show/change review mode
  /custom auto | /custom review
  /help                             — All commands
"""

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger("oracle.telegram")

TG_API = "https://api.telegram.org/bot{token}/{method}"


class TelegramBot:

    def __init__(self):
        self.token   = os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not self.token:
            raise EnvironmentError("TELEGRAM_BOT_TOKEN not set.")

    # ── Core API ───────────────────────────────────────────────────────────────

    async def send_message(self, text: str, chat_id: str = None) -> dict:
        cid = chat_id or self.chat_id
        url = TG_API.format(token=self.token, method="sendMessage")
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(url, json={"chat_id": cid, "text": text, "parse_mode": "HTML"})
        return r.json()

    async def send_video(self, video_path: Path, caption: str, chat_id: str = None) -> dict:
        cid = chat_id or self.chat_id
        url = TG_API.format(token=self.token, method="sendVideo")
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(
                url,
                data={"chat_id": cid, "caption": caption, "parse_mode": "HTML"},
                files={"video": (video_path.name, video_path.read_bytes(), "video/mp4")},
            )
        return r.json()

    async def send_video_for_review(
        self, video_path: Path, caption: str, hook: str, pending_id: str, topic: str
    ):
        review_caption = (
            f"🎬 <b>Review Required</b>\n"
            f"─────────────────────\n"
            f"📌 Topic: <b>{topic}</b>\n"
            f"💬 Hook: <i>{hook[:80]}</i>\n"
            f"─────────────────────\n"
            f"✅ <b>/done {pending_id}</b> — Approve &amp; post to Instagram\n"
            f"❌ <b>/no {pending_id}</b> — Reject &amp; discard\n"
            f"─────────────────────\n"
            f"📝 Caption preview:\n{caption[:200]}..."
        )
        await self.send_video(video_path, review_caption)
        log.info(f"Sent video for review: pending_id={pending_id}")

    # ── Command Parser ─────────────────────────────────────────────────────────

    def _parse_manual_command(self, text: str) -> tuple[str, str]:
        """
        Parses: /now-reel [topic:<content>, (music:<content>)]
        Returns (topic_raw, music_raw). Both may be empty strings.
        Supports free-form, any language, optional music field.
        """
        topic_raw = ""
        music_raw = ""

        # Extract topic: content
        t_match = re.search(r'topic\s*:\s*(.+?)(?:,\s*(?:music\s*:|$)|\)?\s*$)', text, re.IGNORECASE | re.DOTALL)
        if t_match:
            topic_raw = t_match.group(1).strip().rstrip(",").strip()

        # Extract music: content (optional, may be in parens)
        m_match = re.search(r'music\s*:\s*(.+?)(?:\)|$)', text, re.IGNORECASE | re.DOTALL)
        if m_match:
            music_raw = m_match.group(1).strip().rstrip(")").strip()

        # Fallback: if no topic: keyword, treat everything as topic
        if not topic_raw:
            # Strip the command prefix
            raw = re.sub(r'^/now-(?:reel|feed)\s*', '', text, flags=re.IGNORECASE).strip()
            raw = raw.strip("[]()").strip()
            topic_raw = raw

        return topic_raw, music_raw

    # ── Update Handler ─────────────────────────────────────────────────────────

    async def handle_update(self, update: dict) -> Optional[str]:
        from core.state_manager import StateManager
        state = StateManager()

        msg     = update.get("message", {})
        text    = msg.get("text", "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))

        if not text or not chat_id:
            return None

        if self.chat_id and chat_id != str(self.chat_id):
            await self.send_message("⛔ Unauthorized.", chat_id)
            return None

        log.info(f"Received command: '{text}'")

        # ── /now — Autonomous mode ─────────────────────────────────────────────
        if text.strip() == "/now":
            mode_note = "👁️ Sending for review first." if state.get_review_mode() == "review" else "🤖 Auto-posting."
            await self.send_message(
                f"🧠 Autonomous mode — AI is picking topic, image, music & style.\n{mode_note}\n⏳ ~3-10 minutes...",
                chat_id,
            )
            await self._run_and_notify(
                post_type="reel", mode="autonomous",
                topic_raw="", music_raw="", chat_id=chat_id,
            )
            return "autonomous"

        # ── /now-reel [...] — Manual reel ──────────────────────────────────────
        elif text.lower().startswith("/now-reel"):
            topic_raw, music_raw = self._parse_manual_command(text)
            if not topic_raw:
                reply = (
                    "❌ Usage:\n"
                    "<code>/now-reel [topic: your idea here, (music: violin sad)]</code>\n\n"
                    "Music is optional. Topic can be in any language."
                )
                await self.send_message(reply, chat_id)
                return reply
            mode_note = "👁️ Sending for review first." if state.get_review_mode() == "review" else "🤖 Auto-posting."
            await self.send_message(
                f"🎬 Creating Reel...\n📝 Topic: <i>{topic_raw[:80]}</i>\n🎵 Music hint: <i>{music_raw or 'AI will decide'}</i>\n{mode_note}\n⏳ ~3-10 minutes...",
                chat_id,
            )
            await self._run_and_notify(
                post_type="reel", mode="manual",
                topic_raw=topic_raw, music_raw=music_raw, chat_id=chat_id,
            )
            return "manual_reel"

        # ── /now-feed [...] — Manual feed ──────────────────────────────────────
        elif text.lower().startswith("/now-feed"):
            topic_raw, music_raw = self._parse_manual_command(text)
            if not topic_raw:
                reply = (
                    "❌ Usage:\n"
                    "<code>/now-feed [topic: your idea here, (music: soft piano)]</code>\n\n"
                    "Music is optional. Topic can be in any language."
                )
                await self.send_message(reply, chat_id)
                return reply
            mode_note = "👁️ Sending for review first." if state.get_review_mode() == "review" else "🤖 Auto-posting."
            await self.send_message(
                f"📸 Creating Feed post...\n📝 Topic: <i>{topic_raw[:80]}</i>\n🎵 Music hint: <i>{music_raw or 'AI will decide'}</i>\n{mode_note}\n⏳ ~3-10 minutes...",
                chat_id,
            )
            await self._run_and_notify(
                post_type="feed", mode="manual",
                topic_raw=topic_raw, music_raw=music_raw, chat_id=chat_id,
            )
            return "manual_feed"

        # ── /post <topic> — Add to reel queue ─────────────────────────────────
        elif text.startswith("/post "):
            topic = text[6:].strip()
            if not topic:
                reply = "❌ Usage: /post &lt;topic&gt;"
            else:
                item_id = state.add_to_queue(topic, "reel")
                reply = f"✅ Added to Reel queue!\nTopic: <b>{topic}</b>\nID: <code>{item_id}</code>"
            await self.send_message(reply, chat_id)
            return reply

        # ── /feed <topic> — Add to feed queue ─────────────────────────────────
        elif text.startswith("/feed "):
            topic = text[6:].strip()
            if not topic:
                reply = "❌ Usage: /feed &lt;topic&gt;"
            else:
                item_id = state.add_to_queue(topic, "feed")
                reply = f"✅ Added to Feed queue!\nTopic: <b>{topic}</b>\nID: <code>{item_id}</code>"
            await self.send_message(reply, chat_id)
            return reply

        # ── /done [id] ─────────────────────────────────────────────────────────
        elif text.startswith("/done"):
            parts = text.split()
            if len(parts) < 2:
                pending = state.get_all_pending_posts()
                if not pending:
                    reply = "📭 No pending videos."
                else:
                    lines = ["📋 <b>Pending Videos:</b>"]
                    for p in pending:
                        lines.append(f"• <b>{p['topic']}</b> — /done {p['id']} | /no {p['id']}")
                    reply = "\n".join(lines)
            else:
                reply = await self._handle_done(parts[1].strip(), chat_id, state)
            await self.send_message(reply, chat_id)
            return reply

        # ── /no [id] ───────────────────────────────────────────────────────────
        elif text.startswith("/no"):
            parts = text.split()
            if len(parts) < 2:
                pending = state.get_all_pending_posts()
                if not pending:
                    reply = "📭 No pending videos."
                else:
                    lines = ["📋 <b>Pending Videos:</b>"]
                    for p in pending:
                        lines.append(f"• <b>{p['topic']}</b> — /done {p['id']} | /no {p['id']}")
                    reply = "\n".join(lines)
            else:
                pending_id = parts[1].strip()
                pending = state.get_pending_post(pending_id)
                if not pending:
                    reply = f"❌ Not found: <code>{pending_id}</code>"
                else:
                    state.remove_pending_post(pending_id)
                    reply = f"🗑️ Rejected.\nTopic: <b>{pending['topic']}</b>"
            await self.send_message(reply, chat_id)
            return reply

        # ── /custom ────────────────────────────────────────────────────────────
        elif text == "/custom":
            current = state.get_review_mode()
            reply = (
                f"⚙️ <b>Review Mode</b>\n"
                f"Current: <b>{current.upper()}</b>\n\n"
                f"/custom auto — Post directly to Instagram\n"
                f"/custom review — Send to you for approval first"
            )
            await self.send_message(reply, chat_id)
            return reply

        elif text == "/custom auto":
            state.set_review_mode("auto")
            reply = "✅ Mode: <b>AUTO</b> — Videos post directly."
            await self.send_message(reply, chat_id)
            return reply

        elif text == "/custom review":
            state.set_review_mode("review")
            reply = "✅ Mode: <b>REVIEW</b> — You approve each video first."
            await self.send_message(reply, chat_id)
            return reply

        # ── /status ────────────────────────────────────────────────────────────
        elif text == "/status":
            s = state.get_stats()
            last = s.get("last_post")
            last_str = f"\n🕐 Last: <b>{last['topic']}</b> ({last['date'][:10]})" if last else ""
            mode_emoji = "👁️" if s["review_mode"] == "review" else "🤖"
            reply = (
                f"📊 <b>Project Oracle</b>\n{'─'*28}\n"
                f"🤖 Gemini: {s['gemini_used']}/{s['gemini_limit']}\n"
                f"🖼️ Images: {s['images_used']}/{s['image_limit']}\n"
                f"📋 Queue: {s['queue_length']}\n"
                f"⏳ Pending: {s['pending_count']}\n"
                f"📸 Total posts: {s['total_posts']}"
                f"{last_str}\n"
                f"🔄 Resets: {s['reset_date']}\n"
                f"{mode_emoji} Mode: <b>{s['review_mode'].upper()}</b>"
            )
            await self.send_message(reply, chat_id)
            return reply

        # ── /queue ─────────────────────────────────────────────────────────────
        elif text == "/queue":
            queue = state.get_topic_queue()
            if not queue:
                reply = "📭 Queue is empty."
            else:
                lines = [f"📋 <b>Queue ({len(queue)})</b>"]
                for i, item in enumerate(queue, 1):
                    lines.append(f"{i}. [{item['type']}] {item['topic']} <code>({item['id']})</code>")
                reply = "\n".join(lines)
            await self.send_message(reply, chat_id)
            return reply

        # ── /clear ─────────────────────────────────────────────────────────────
        elif text == "/clear":
            state._state["topic_queue"] = []
            state._save()
            reply = "🗑️ Queue cleared."
            await self.send_message(reply, chat_id)
            return reply

        # ── /help ──────────────────────────────────────────────────────────────
        elif text == "/help":
            reply = (
                "🤖 <b>Project Oracle Commands</b>\n"
                "─────────────────────────\n"
                "<b>Instant post:</b>\n"
                "/now — AI picks everything autonomously\n"
                "/now-reel [topic: ..., (music: ...)] — Custom reel\n"
                "/now-feed [topic: ..., (music: ...)] — Custom feed\n\n"
                "<b>Queue:</b>\n"
                "/post &lt;topic&gt; — Add to reel queue\n"
                "/feed &lt;topic&gt; — Add to feed queue\n"
                "/queue — View queue\n"
                "/clear — Clear queue\n\n"
                "<b>Review:</b>\n"
                "/done &lt;id&gt; — Approve → post\n"
                "/no &lt;id&gt; — Reject\n\n"
                "<b>Settings:</b>\n"
                "/custom — View/change review mode\n"
                "/status — Quota &amp; stats\n"
                "/help — This message\n\n"
                "<b>Examples:</b>\n"
                "<code>/now-reel [topic: mera dost dhoka dega, (music: violin sad slow)]</code>\n"
                "<code>/now-reel [topic: life is unfair but keep going]</code>\n"
                "<code>/now-feed [topic: success mindset, (music: epic cinematic)]</code>"
            )
            await self.send_message(reply, chat_id)
            return reply

        else:
            reply = "❓ Unknown command. Try /help"
            await self.send_message(reply, chat_id)
            return reply

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _handle_done(self, pending_id: str, chat_id: str, state) -> str:
        pending = state.get_pending_post(pending_id)
        if not pending:
            return f"❌ Not found: <code>{pending_id}</code>"

        await self.send_message(f"⏳ Publishing <b>{pending['topic']}</b>...", chat_id)
        try:
            import main as oracle_main
            result = await oracle_main.publish_pending(pending_id)
            if result["status"] == "success":
                return (
                    f"✅ <b>Posted!</b>\n"
                    f"Topic: <b>{pending['topic']}</b>\n"
                    f"IG ID: <code>{result.get('ig_post_id')}</code>"
                )
            return f"⚠️ {result.get('status')}: {result.get('message', '')}"
        except Exception as e:
            return f"❌ Failed: {str(e)[:200]}"

    async def _run_and_notify(
        self, post_type: str, mode: str,
        topic_raw: str, music_raw: str, chat_id: str,
    ):
        import main as oracle_main
        try:
            result = await oracle_main.run_pipeline(
                post_type=post_type,
                topic_raw=topic_raw,
                music_raw=music_raw,
                mode=mode,
            )
            if result["status"] == "success":
                msg = (
                    f"✅ <b>Posted!</b>\n"
                    f"Topic: <b>{result.get('topic', '')}</b>\n"
                    f"IG ID: <code>{result.get('ig_post_id')}</code>"
                )
            elif result["status"] == "pending_review":
                msg = (
                    f"👆 <b>Video ready for review!</b>\n"
                    f"Topic: <b>{result.get('topic', '')}</b>\n"
                    f"✅ /done {result['pending_id']}\n"
                    f"❌ /no {result['pending_id']}"
                )
            else:
                msg = f"⚠️ Status: {result['status']}"
        except Exception as e:
            msg = f"❌ Pipeline error: {str(e)[:300]}"
        await self.send_message(msg, chat_id)

    async def start_polling(self):
        log.info("Starting Telegram polling...")
        offset = 0
        while True:
            try:
                url = TG_API.format(token=self.token, method="getUpdates")
                async with httpx.AsyncClient(timeout=35) as c:
                    r = await c.get(url, params={"offset": offset, "timeout": 30, "allowed_updates": ["message"]})
                for update in r.json().get("result", []):
                    offset = update["update_id"] + 1
                    await self.handle_update(update)
            except Exception as e:
                log.error(f"Polling error: {e}")
                await asyncio.sleep(5)