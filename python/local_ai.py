"""Offline fallback: local speech-to-text (faster-whisper tiny.en) + local LLM (arduino:llm brick).

Both are loaded lazily. The STT model is unloaded after STT_IDLE_UNLOAD_S of inactivity
to keep RAM free for the llama.cpp container.
"""

import gc
import os
import re
import threading
import time
import wave

ASCII_MAP = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u2026": "...",
})


def clean_for_oled(text: str) -> str:
    """Keep printable 7-bit ASCII only (the OLED uses a 5x7 bitmap font)."""
    text = text.translate(ASCII_MAP).replace("\n", " ").replace("\r", " ")
    cleaned = "".join(ch if 0x20 <= ord(ch) <= 0x7E else " " for ch in text)
    return " ".join(cleaned.split())


HERE = os.path.dirname(os.path.abspath(__file__))
STT_DIR = os.path.join(HERE, "stt_models")  # persistent cache so it works offline
STT_MODEL = "tiny.en"
STT_IDLE_UNLOAD_S = 45
LLM_TIMEOUT_S = 45
MAX_REPLY_WORDS = 25
MAX_REPLY_CHARS = 160  # stream safety cap (x3) only


class Cancelled(Exception):
    pass


class LocalSTT:
    def __init__(self):
        self._lock = threading.Lock()
        self._model = None
        self._last_use = 0.0
        self._timer = None

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            os.makedirs(STT_DIR, exist_ok=True)
            t = time.time()
            self._model = WhisperModel(
                STT_MODEL, device="cpu", compute_type="int8", cpu_threads=4, download_root=STT_DIR
            )
            print(f"[stt] loaded {STT_MODEL} in {time.time() - t:.1f}s", flush=True)

    def _schedule_unload(self):
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(STT_IDLE_UNLOAD_S, self.unload)
        self._timer.daemon = True
        self._timer.start()

    def unload(self):
        with self._lock:
            if self._model is not None and time.time() - self._last_use >= STT_IDLE_UNLOAD_S - 1:
                self._model = None
                gc.collect()
                print("[stt] unloaded (idle)", flush=True)

    @staticmethod
    def _read_wav(path):
        import numpy as np

        with wave.open(path) as w:
            rate = w.getframerate()
            raw = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
        if rate != 16000 and len(raw):
            n = int(len(raw) * 16000 / rate)
            raw = np.interp(np.linspace(0, len(raw) - 1, n), np.arange(len(raw)), raw).astype(np.float32)
        return raw

    def transcribe(self, wav_path: str) -> str:
        audio = self._read_wav(wav_path)
        if len(audio) < 1600:
            return ""
        with self._lock:
            self._load()
            t = time.time()
            segs, _ = self._model.transcribe(
                audio, language="en", beam_size=1, vad_filter=False, condition_on_previous_text=False
            )
            text = " ".join(s.text.strip() for s in segs).strip()
            self._last_use = time.time()
            print(f"[stt] {time.time() - t:.1f}s -> {text!r}", flush=True)
        self._schedule_unload()
        return text


_THINK_RE = re.compile(r"<think>.*?</think>", re.S)


class LocalLLM:
    def __init__(self, system_prompt: str):
        self.system_prompt = system_prompt
        self._llm = None
        self._init_lock = threading.Lock()
        self._chat_lock = threading.Lock()

    def _get(self):
        with self._init_lock:
            if self._llm is None:
                from arduino.app_bricks.llm import LargeLanguageModel

                self._llm = LargeLanguageModel(system_prompt=self.system_prompt).with_memory(max_messages=6)
            return self._llm

    def ask(self, prompt: str, is_cancelled=lambda: False) -> str:
        """Stream a reply; abort on cancel or timeout. Raises Cancelled / RuntimeError."""
        llm = self._get()
        with self._chat_lock:
            start = time.time()
            out = []
            gen = llm.chat_stream(prompt)
            try:
                for tok in gen:
                    out.append(tok)
                    if is_cancelled():
                        llm.stop_stream()
                        raise Cancelled()
                    if time.time() - start > LLM_TIMEOUT_S:
                        llm.stop_stream()
                        break
                    if len("".join(out)) > MAX_REPLY_CHARS * 3:
                        llm.stop_stream()
                        break
            except Cancelled:
                raise
            text = _THINK_RE.sub("", "".join(out))
            text = text.split("</think>")[-1]
            text = clean_for_oled(text)
            words = text.split()
            if len(words) > MAX_REPLY_WORDS:
                cut = " ".join(words[:MAX_REPLY_WORDS])
                end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
                text = cut[: end + 1] if end > 20 else cut.rstrip(" ,;:-") + "..."
            if not text:
                raise RuntimeError("Local AI gave no answer.")
            print(f"[llm] {time.time() - start:.1f}s -> {text}", flush=True)
            return text
