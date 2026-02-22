"""
Enterprise Production-Hardened Processors
Version 6.0
"""

import os
import logging
import base64
import asyncio
import subprocess
import mimetypes
import re
import time
import random
from typing import Optional, Tuple, List, Dict, Any, AsyncGenerator
from openai import AsyncOpenAI

import config

logger = logging.getLogger(__name__)

# =============================================================================
# TOKEN UTILITIES
# =============================================================================

def estimate_tokens(text: str) -> int:
    """Approximate token estimator (~4 chars per token)."""
    return max(1, int(len(text) / 4))


def trim_messages_to_token_limit(messages: List[Dict], max_tokens: int) -> List[Dict]:
    """Sliding window trimming."""
    total = 0
    trimmed = []

    for msg in reversed(messages):
        total += estimate_tokens(msg.get("content", ""))
        if total > max_tokens:
            break
        trimmed.insert(0, msg)

    return trimmed


# =============================================================================
# SAFE FILE UTILITIES
# =============================================================================

async def safe_remove(path: str):
    try:
        if os.path.exists(path):
            await asyncio.to_thread(os.remove, path)
    except Exception:
        pass


async def safe_rmdir(path: str):
    try:
        if os.path.exists(path):
            await asyncio.to_thread(os.rmdir, path)
    except Exception:
        pass


# =============================================================================
# GROQ CLIENT MANAGER (HARDENED)
# =============================================================================

class GroqClientManager:

    def __init__(self):
        self._clients: List[AsyncOpenAI] = []
        self._health: Dict[int, Dict[str, Any]] = {}
        self._initialized = False
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(config.MAX_PARALLEL_REQUESTS)

    def is_initialized(self):
        return self._initialized

    async def initialize(self, api_keys: str):
        async with self._lock:
            if self._initialized:
                return

            keys = [k.strip() for k in api_keys.split(",") if k.strip()]
            if not keys:
                raise Exception("No GROQ_API_KEYS configured")

            for idx, key in enumerate(keys):
                client = AsyncOpenAI(
                    api_key=key,
                    base_url="https://api.groq.com/openai/v1",
                    timeout=config.GROQ_TIMEOUT,
                )
                self._clients.append(client)
                self._health[idx] = {
                    "failures": 0,
                    "disabled_until": 0,
                    "last_used": 0,
                }

            self._initialized = True
            logger.info(f"Groq clients initialized: {len(self._clients)}")

    def _available_clients(self):
        now = time.time()
        return [
            (idx, client)
            for idx, client in enumerate(self._clients)
            if self._health[idx]["disabled_until"] < now
        ]

    def _select_client(self):
        available = self._available_clients()
        if not available:
            raise Exception("All Groq keys disabled")

        available.sort(key=lambda x: self._health[x[0]]["last_used"])
        idx, client = available[0]
        self._health[idx]["last_used"] = time.time()
        return idx, client

    def _register_failure(self, idx: int):
        self._health[idx]["failures"] += 1

        if self._health[idx]["failures"] >= config.MAX_FAILURES_BEFORE_DISABLE:
            cooldown = config.CIRCUIT_BREAKER_TIMEOUT
            self._health[idx]["disabled_until"] = time.time() + cooldown
            self._health[idx]["failures"] = 0
            logger.warning(f"Groq key {idx} disabled for {cooldown}s")

    def _register_success(self, idx: int):
        self._health[idx]["failures"] = 0

    async def make_request(self, func, *args, **kwargs):
        if not self._initialized:
            raise Exception("GroqClientManager not initialized")

        async with self._semaphore:

            attempts = len(self._clients) * config.GROQ_RETRY_COUNT

            for attempt in range(attempts):
                idx, client = self._select_client()

                try:
                    result = await asyncio.wait_for(
                        func(client, *args, **kwargs),
                        timeout=config.REQUEST_TIMEOUT
                    )
                    self._register_success(idx)
                    return result

                except Exception as e:
                    self._register_failure(idx)

                    backoff = min(
                        config.MAX_BACKOFF_SECONDS,
                        (2 ** attempt) + random.uniform(0, 1)
                    )
                    await asyncio.sleep(backoff)

            raise Exception("All Groq clients failed after retries")


groq_client_manager = GroqClientManager()


# =============================================================================
# TEXT PROCESSOR
# =============================================================================

class TextProcessor:

    async def _chat(self, prompt: str, text: str, model_type: str, temperature: float) -> str:
        if not groq_client_manager.is_initialized():
            raise Exception("Groq not initialized")

        max_model_tokens = config.MODEL_TOKEN_LIMITS[model_type]
        text_tokens = estimate_tokens(text)

        if text_tokens > max_model_tokens:
            cut_ratio = max_model_tokens / text_tokens
            text = text[:int(len(text) * cut_ratio)]

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": text}
        ]

        messages = trim_messages_to_token_limit(
            messages,
            max_model_tokens
        )

        async def call(client):
            response = await client.chat.completions.create(
                model=config.GROQ_MODELS[model_type],
                messages=messages,
                temperature=temperature,
            )
            return response.choices[0].message.content

        return await groq_client_manager.make_request(call)

    async def basic_correction(self, text: str):
        return await self._chat(
            config.BASIC_CORRECTION_PROMPT,
            text,
            "basic",
            config.BASIC_CORRECTION_TEMPERATURE,
        )

    async def premium_correction(self, text: str):
        return await self._chat(
            config.PREMIUM_CORRECTION_PROMPT,
            text,
            "premium",
            config.PREMIUM_CORRECTION_TEMPERATURE,
        )

    async def summarize_text(self, text: str):
        if len(text) < config.MIN_CHARS_FOR_SUMMARY:
            return config.ERROR_TEXT_TOO_SHORT_FOR_SUMMARY

        return await self._chat(
            config.SUMMARIZATION_PROMPT,
            text,
            "reasoning",
            config.SUMMARIZATION_TEMPERATURE,
        )


text_processor = TextProcessor()


# =============================================================================
# DIALOGUE MANAGER (NO GLOBAL STORAGE)
# =============================================================================

class DialogueManager:

    def __init__(self):
        self._store: Dict[int, Dict[int, Dict[str, Any]]] = {}

    def add_document_context(self, user_id: int, message_id: int, text: str):
        if user_id not in self._store:
            self._store[user_id] = {}

        self._store[user_id][message_id] = {
            "text": text,
            "history": [],
            "last_accessed": time.time()
        }

    def get_document_context(self, user_id: int, message_id: int):
        ctx = self._store.get(user_id, {}).get(message_id)
        if ctx:
            ctx["last_accessed"] = time.time()
        return ctx

    async def answer_document_question(self, user_id: int, message_id: int, question: str):

        ctx = self.get_document_context(user_id, message_id)
        if not ctx:
            return "Контекст не найден"

        messages = [
            {"role": "system", "content": config.SUMMARIZATION_PROMPT},
            {"role": "user", "content": ctx["text"]},
        ]

        messages.extend(ctx["history"])
        messages.append({"role": "user", "content": question})

        max_tokens = config.MODEL_TOKEN_LIMITS["reasoning"]

        messages = trim_messages_to_token_limit(messages, max_tokens)

        async def call(client):
            response = await client.chat.completions.create(
                model=config.GROQ_MODELS["reasoning"],
                messages=messages,
                temperature=config.MODEL_TEMPERATURES["reasoning"],
            )
            return response.choices[0].message.content

        reply = await groq_client_manager.make_request(call)

        ctx["history"].append({"role": "user", "content": question})
        ctx["history"].append({"role": "assistant", "content": reply})

        return reply


dialogue_manager = DialogueManager()


# =============================================================================
# MAIN ENTRY
# =============================================================================

async def process_content(
    file_path: Optional[str],
    text_content: Optional[str],
    content_type: str
) -> Tuple[str, str, str]:

    if not groq_client_manager.is_initialized():
        await groq_client_manager.initialize(
            os.environ.get("GROQ_API_KEYS", "")
        )

    processed_text = ""
    original_text = text_content or ""
    file_type = "unknown"

    if content_type == "text":
        processed_text = text_content
        file_type = "text"

    elif content_type == "photo" and file_path:
        with open(file_path, "rb") as f:
            image_bytes = f.read()

        base64_image = base64.b64encode(image_bytes).decode("utf-8")

        async def call(client):
            response = await client.chat.completions.create(
                model=config.GROQ_MODELS["vision"],
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": config.OCR_PROMPT},
                        {"type": "image_url", "image_url": {
                            "url": f"data:image/jpeg;base64,{base64_image}"
                        }}
                    ]
                }]
            )
            return response.choices[0].message.content

        processed_text = await groq_client_manager.make_request(call)
        file_type = "image"

    if not original_text and processed_text:
        original_text = processed_text

    return processed_text, original_text, file_type
