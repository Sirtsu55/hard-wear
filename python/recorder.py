"""Receives mic audio from the sketch over the Bridge and saves the clip as WAV.

Protocol (sketch -> Python):
    audio_start()                 reset chunk buffer
    audio_chunk(index, hex)       little-endian int16 mono samples, hex-encoded
    audio_end(total_samples)      write the recorded clip to recording.wav
"""

import os
import threading
import wave

RATE = 8000
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")


class WavRecorder:
    def __init__(self):
        self._lock = threading.Lock()
        self._chunks = {}

    def start(self):
        with self._lock:
            self._chunks = {}

    def add_chunk(self, index: int, hex_data: str):
        raw = bytes.fromhex(hex_data)
        with self._lock:
            self._chunks[int(index)] = raw

    def _write_wav(self) -> tuple[str, int]:
        with self._lock:
            data = b"".join(self._chunks[i] for i in sorted(self._chunks))
            got = len(data) // 2
            self._chunks = {}
        # Auto-normalize quiet speech so far-field wearable voice clips are loud & clear for Gemini
        if got > 0:
            import array
            samples = array.array("h")
            samples.frombytes(data)
            peak = max(abs(samples[0]), *(abs(s) for s in samples)) if got > 1 else abs(samples[0])
            if 50 < peak < 18000:
                gain = min(12.0, 22000.0 / peak)
                for idx in range(len(samples)):
                    v = int(samples[idx] * gain)
                    if v > 32767:
                        v = 32767
                    elif v < -32768:
                        v = -32768
                    samples[idx] = v
                data = samples.tobytes()
                print(f"[mic] peak={peak} auto-gain={gain:.1f}x", flush=True)
            else:
                print(f"[mic] peak={peak}", flush=True)
        os.makedirs(OUT_DIR, exist_ok=True)
        path = os.path.abspath(os.path.join(OUT_DIR, "recording.wav"))
        tmp_path = path + ".tmp"
        with wave.open(tmp_path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(data)
        os.replace(tmp_path, path)
        return path, got

    def finish(self, expected_samples: int = 0) -> str:
        path, got = self._write_wav()
        if expected_samples and got != expected_samples:
            print(f"[mic] warning: got {got} of {expected_samples} samples", flush=True)
        return path

