"""Multimodal Google Gemini REST client for Hard Wear voice & text queries.

Reads the API key dynamically from App Lab's Cloud LLM Brick Configuration (`API_KEY` env var)
so secrets never live in code. Supports inline WAV audio (`audio/wav`) + rolling text memory.
"""

import base64
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request

GEMINI_MODELS = [
    "gemini-flash-lite-latest",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
]

ASCII_MAP = str.maketrans({
    "\u2018": "'",
    "\u2019": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u2013": "-",
    "\u2014": "-",
    "\u2026": "...",
})


def clean_for_oled(text: str) -> str:
    text = text.translate(ASCII_MAP).replace("\n", " ").replace("\r", " ")
    # Keep printable 7-bit ASCII (0x20..0x7E) for the 5x7 OLED bitmap font
    cleaned = "".join(ch if 0x20 <= ord(ch) <= 0x7E else " " for ch in text)
    return " ".join(cleaned.split())


class GeminiAudioAssistant:
    def __init__(self, system_prompt: str, max_turns: int = 6):
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self._lock = threading.Lock()
        self._history = []  # list of {"role": "user"|"model", "parts": [{"text": ...}]}

    def _get_api_key(self) -> str:
        return (os.getenv("GEMINI_API_KEY") or os.getenv("API_KEY") or "").strip()

    def _validate_key(self, key: str):
        if not key:
            raise RuntimeError("Set your Gemini API_KEY in Cloud LLM Brick config.")
        if key.startswith("sk-ant-"):
            raise RuntimeError("API_KEY is still an Anthropic key! Paste a Gemini key (AIza...) in Cloud LLM config.")

    def _post_generate(self, key: str, model: str, payload: dict) -> str:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{urllib.parse.quote(model)}:generateContent?key={urllib.parse.quote(key)}"
        )
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        candidates = data.get("candidates") or []
        if not candidates:
            raise RuntimeError("Empty response from Gemini.")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        texts = [p.get("text", "") for p in parts if "text" in p]
        answer = clean_for_oled(" ".join(texts))
        if not answer:
            raise RuntimeError("No text returned by Gemini.")
        return answer

    def _call_with_fallback(self, key: str, current_user_parts: list, history_label: str) -> str:
        with self._lock:
            contents = list(self._history) + [{"role": "user", "parts": current_user_parts}]

        payload = {
            "system_instruction": {"parts": [{"text": self.system_prompt}]},
            "contents": contents,
            "generationConfig": {
                "maxOutputTokens": 120,
                "temperature": 0.7,
            },
        }

        last_err = None
        for model in GEMINI_MODELS:
            try:
                answer = self._post_generate(key, model, payload)
                with self._lock:
                    self._history.append({"role": "user", "parts": [{"text": history_label}]})
                    self._history.append({"role": "model", "parts": [{"text": answer}]})
                    if len(self._history) > self.max_turns * 2:
                        self._history = self._history[-(self.max_turns * 2):]
                print(f"[gemini:{model}] {answer}", flush=True)
                return answer
            except urllib.error.HTTPError as e:
                err_body = ""
                try:
                    err_body = e.read().decode("utf-8", errors="ignore")
                except Exception:
                    pass
                print(f"[gemini:{model}] HTTP {e.code}: {err_body[:180]}", flush=True)
                # Try next model name on 404 (model alias not found); stop immediately on 400/401/403
                last_err = f"HTTP {e.code}"
                if e.code != 404:
                    try:
                        msg = json.loads(err_body).get("error", {}).get("message", "")
                        if msg:
                            last_err = clean_for_oled(msg)[:80]
                    except Exception:
                        pass
                    break
            except Exception as e:
                last_err = clean_for_oled(str(e))[:80]
                print(f"[gemini:{model}] error: {last_err}", flush=True)
                break

        raise RuntimeError(last_err or "Gemini request failed")

    def ask_audio(self, wav_path: str) -> str:
        key = self._get_api_key()
        self._validate_key(key)

        with open(wav_path, "rb") as f:
            wav_bytes = f.read()

        b64_audio = base64.b64encode(wav_bytes).decode("ascii")
        parts = [
            {
                "inline_data": {
                    "mime_type": "audio/wav",
                    "data": b64_audio,
                }
            },
            {
                "text": (
                    "Listen to what the wearer just said into their Hard Wear badge microphone "
                    "and answer directly. If the audio is silent or unintelligible, playfully "
                    "ask them to hold the knob and try speaking a bit closer."
                )
            },
        ]
        return self._call_with_fallback(key, parts, "[Spoken voice message from user]")

    def ask_text(self, prompt: str) -> str:
        key = self._get_api_key()
        self._validate_key(key)
        parts = [{"text": prompt}]
        return self._call_with_fallback(key, parts, prompt)
