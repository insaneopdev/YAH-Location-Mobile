"""
core_navigation.py
==================
Macro-navigation module for the Context-Aware Assistive Navigation System
(Project: HamLerc).

Enhancements over v1:
  - Overview polyline decoding → smooth, accurate route line on map (hundreds of
    GPS points instead of just N step endpoints).
  - HTML entity decoding (&amp; → &, etc.) so instructions read cleanly.
  - Route summary (total distance + ETA) printed before navigation starts.
  - Numbered, clickable step markers on the map with full instruction in popup.
  - Route alternatives: fetches up to 3 options, lets user pick before navigating.
  - Sensible API flags: avoid=ferries for walking routes.
  - "Pass by" hints separated from the main manoeuvre instruction.

NOTE: All I/O bottoms out in speak() / listen() — swap those two for TTS/STT
      and nothing else changes.

Dependencies: requests  (pip install requests)
"""

import asyncio
import html as html_module
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser

import requests

# Optional: Windows Location API (pip install winsdk)
try:
    from winsdk.windows.devices.geolocation import Geolocator, PositionAccuracy
    _WINSDK_OK = True
except ImportError:
    _WINSDK_OK = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GOOGLE_MAPS_API_KEY = "AIzaSyDfw239syi91gepUcfKVuqTxKH5XwLDXWQ"

# How many alternative routes to request (1 = no alternatives, max 3)
MAX_ALTERNATIVES = 3

# Delay (seconds) between announcing each navigation step (simulation only)
# Set to 0 — pyttsx3's runAndWait() already blocks until speech finishes.
STEP_PRINT_DELAY = 0.0


# ---------------------------------------------------------------------------
# I/O Abstraction Layer  (TTS + STT)
# ---------------------------------------------------------------------------
# speak() uses pyttsx3 (offline Windows SAPI voice).
# listen() uses SpeechRecognition with Google's free cloud ASR.
# Both still print to the terminal so anyone watching can follow along.

import subprocess
import sys
import pyttsx3                       # used inside the TTS subprocess script
import speech_recognition as sr     # used by _listen_google

# TTS rate and volume — tweak here if needed
_TTS_RATE   = 250   # words per minute  (faster setup, less time waiting)
_TTS_VOLUME = 1.0   # 0.0 – 1.0

# Background TTS queue management (sequential, non-blocking speech)
import queue as queue_module
_tts_queue = queue_module.Queue()
_tts_worker = None
_tts_stop_flag = False
_tts_current_process = None  # Track currently playing TTS process
_tts_process_lock = threading.Lock()  # Protect process access


def _tts_worker_thread() -> None:
    """Dedicated thread that processes TTS messages sequentially (no overlapping)."""
    global _tts_stop_flag, _tts_current_process
    while not _tts_stop_flag:
        try:
            message = _tts_queue.get(timeout=0.5)
            if message is None:
                break
            
            spoken = _sanitize_for_speech(message)
            if not spoken:
                _tts_queue.task_done()
                continue

            # Escape for PowerShell
            safe = (
                spoken
                .replace('"', ' ')
                .replace('`', ' ')
                .replace('$', ' ')
                .replace('\n', '. ')
            )

            # ── Primary: System.Speech via PowerShell ────────────────────────────
            tmp_ps1 = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".ps1", delete=False, encoding="utf-8"
                ) as f:
                    f.write(
                        "Add-Type -AssemblyName System.Speech\n"
                        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
                        "$s.Volume = 100\n"
                        "$s.Rate = -2\n"
                        f'$s.Speak("{safe}")\n'
                    )
                    tmp_ps1 = f.name

                # Store process so we can kill it if needed
                with _tts_process_lock:
                    _tts_current_process = subprocess.Popen(
                        [
                            "powershell", "-NoProfile", "-NonInteractive",
                            "-ExecutionPolicy", "Bypass", "-File", tmp_ps1,
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                
                # Wait for process to finish
                _tts_current_process.wait(timeout=60)
                
                with _tts_process_lock:
                    _tts_current_process = None
                
                _tts_queue.task_done()
                continue
            except Exception as exc:
                print(f"[TTS] PowerShell failed: {exc}")
                with _tts_process_lock:
                    _tts_current_process = None
                _tts_queue.task_done()
            finally:
                if tmp_ps1:
                    try:
                        os.unlink(tmp_ps1)
                    except OSError:
                        pass

            # ── Fallback: pyttsx3 ──────────────────────────────────────────────
            try:
                engine = pyttsx3.init()
                engine.setProperty("rate",   _TTS_RATE)
                engine.setProperty("volume", _TTS_VOLUME)
                engine.say(spoken)
                engine.runAndWait()
                engine.stop()
            except Exception as exc2:
                print(f"[TTS] pyttsx3 failed: {exc2}")

        except queue_module.Empty:
            continue
        except Exception as e:
            print(f"[TTS] Worker error: {e}")


def _start_tts_worker() -> None:
    """Start the TTS worker thread if not already running."""
    global _tts_worker
    if _tts_worker is None or not _tts_worker.is_alive():
        _tts_stop_flag = False
        _tts_worker = threading.Thread(target=_tts_worker_thread, daemon=True)
        _tts_worker.start()


def speak(message: str) -> None:
    """
    Queue a message for TTS playback (non-blocking).
    
    Messages are played sequentially (no overlapping audio).
    Main thread continues immediately so mic can listen while TTS plays.
    
    Uses Windows System.Speech .NET via a temp PowerShell script —
    the most reliable method on Windows 10/11. Falls back to pyttsx3.
    """
    print(f"\n[NAV] {message}")
    _start_tts_worker()
    _tts_queue.put(message)


def _stop_tts_immediately() -> None:
    """Kill the currently playing TTS audio immediately (instant cutoff)."""
    global _tts_current_process
    with _tts_process_lock:
        if _tts_current_process and _tts_current_process.poll() is None:
            try:
                _tts_current_process.terminate()
                _tts_current_process.wait(timeout=1)
            except Exception:
                try:
                    _tts_current_process.kill()
                except Exception:
                    pass
            _tts_current_process = None







def _sanitize_for_speech(text: str) -> str:
    """
    Strip every visual-formatting symbol from a message before passing it to
    the TTS engine, so the output sounds natural with no symbols read aloud.

    Rules applied line-by-line:
      - Pure separator lines (----, ════, ──── etc.) → skipped entirely.
      - === wrappers → removed, leaving the inner text.
      - [N] / [S] / [D] option markers → "option N" / "start" / "destination"
      - · (middle dot) → ","
      - — (em dash) / – → ","
      - ↳  → ","
      - ~ → "about "
      - GPS: (lat, lng) lines → skipped (meaningless spoken aloud)
      - (lat, lng) coordinate pairs → skipped
      - {, } → removed
      - Stray colons left at line start after stripping → removed
      - Multiple spaces / blank lines → collapsed
    """
    lines = text.split("\n")
    out: list[str] = []

    for raw_line in lines:
        line = raw_line.strip()

        # Skip pure separator / decoration lines
        if not line or re.match(r"^[-─═=*\s]+$", line):
            continue

        # Skip GPS coordinate lines
        if re.match(r"GPS\s*[:：]", line, re.IGNORECASE):
            continue
        if re.match(r"^\(?-?\d{1,3}\.\d+\s*,\s*-?\d{1,3}\.\d+\)?$", line):
            continue

        # === wrappers
        line = re.sub(r"=+", "", line)

        # [1], [2] … option markers
        line = re.sub(r"\[(\d)\]", r"option \1", line)

        # Character substitutions
        line = line.replace("↳", ",")
        line = line.replace("·", ",")
        line = line.replace("—", ",").replace("–", ",")
        line = line.replace("~", "about ")
        line = re.sub(r"[{}]", "", line)

        # Clean up leftover punctuation artefacts
        line = re.sub(r"\s{2,}", " ", line)
        line = re.sub(r"^[,:\s]+", "", line)   # leading comma/colon
        line = re.sub(r"[,\s]+$", "", line)    # trailing comma

        line = line.strip()
        if line:
            out.append(line)

    return ".  ".join(out)   # pause between lines when spoken




# ── Whisper offline STT (primary — handles any vocabulary incl. place names)
import struct
import wave
import json
import urllib.request
import zipfile
import io
import pyaudio
import json

# Optional: Google Cloud Speech-to-Text for high-accuracy recognition
try:
    from google.cloud import speech_v1 as gcs_speech
    _GCS_OK = True
except Exception:
    _GCS_OK = False

# Optional: VOSK offline recognizer (grammar-driven, fast, no cloud)
try:
    import vosk
    _VOSK_OK = True
except Exception:
    _VOSK_OK = False

_vosk_model = None

def _get_vosk_model():
    global _vosk_model
    if _vosk_model is not None:
        return _vosk_model
    # Try common local model locations: bundled folder or environment variable
    candidates = []
    script_dir = os.path.dirname(__file__)
    candidates.append(os.path.join(script_dir, "vosk-model-small-en-in-0.4"))
    candidates.append(os.path.join(script_dir, "vosk-model-small-en-us-0.15"))
    env_path = os.environ.get("VOSK_MODEL_PATH")
    if env_path:
        candidates.insert(0, env_path)

    for path in candidates:
        if path and os.path.isdir(path):
            try:
                _vosk_model = vosk.Model(path)
                print(f"[VOSK] Loaded model from {path}")
                return _vosk_model
            except Exception as exc:
                print(f"[VOSK] Failed to load model at {path}: {exc}")
    _vosk_model = None
    return None


def _listen_vosk(timeout_secs: int = 6, phrases: list[str] | None = None) -> str | None:
    """
    Recognise speech using a local VOSK model with an optional phrase list (grammar).

    Returns the recognised text or None.
    """
    if not _VOSK_OK:
        return None

    model = _get_vosk_model()
    if model is None:
        return None

    wav_path = _record_to_wav(max_secs=timeout_secs)
    if not wav_path:
        return None

    try:
        with wave.open(wav_path, "rb") as wf:
            if wf.getnchannels() != 1 or wf.getsampwidth() != 2 or wf.getframerate() not in (8000, 16000, 32000):
                # VOSK prefers 16k mono; re-recorded audio uses 16000 by default
                pass

            grammar = None
            if phrases:
                # VOSK expects a JSON array of phrases as a grammar
                try:
                    grammar = json.dumps(phrases)
                except Exception:
                    grammar = None

            rec = vosk.KaldiRecognizer(model, wf.getframerate(), grammar) if grammar else vosk.KaldiRecognizer(model, wf.getframerate())
            rec.SetWords(False)

            results: list[str] = []
            while True:
                data = wf.readframes(4000)
                if len(data) == 0:
                    break
                if rec.AcceptWaveform(data):
                    res = rec.Result()
                    try:
                        j = json.loads(res)
                        if "text" in j:
                            results.append(j["text"])
                    except Exception:
                        pass
            # final partial
            final = rec.FinalResult()
            try:
                j = json.loads(final)
                if "text" in j and j["text"]:
                    results.append(j["text"])
            except Exception:
                pass

            text = " ".join(results).strip()
            return text if text else None
    except Exception:
        return None
    finally:
        try:
            os.unlink(wav_path)
        except OSError:
            pass

# ── Whisper model (lazy-loaded) ────────────────────────────────────────────
_WHISPER_MODEL_SIZE = "small"  # much better accent + proper noun handling (~460 MB)
_whisper_model = None

# Prompt that primes Whisper's decoder with Indian place names so it
# outputs "Chennai" instead of "Channel", "Hyderabad" instead of "Higher bad", etc.
_WHISPER_INITIAL_PROMPT = (
    "Chennai, Mumbai, Delhi, Bangalore, Hyderabad, Kolkata, Pune, Ahmedabad, "
    "Jaipur, Lucknow, Coimbatore, Madurai, Trichy, Kochi, Visakhapatnam, "
    "Thiruvananthapuram, Mysore, Mangalore, Salem, Tirupati, Vellore, "
    "Pondicherry, Chandigarh, Indore, Nagpur, Bhopal, Goa, Surat, Vadodara, "
    "Noida, Gurgaon, Faridabad, Ghaziabad, Agra, Varanasi, Amritsar, "
    "Anna Nagar, T Nagar, Adyar, Velachery, Tambaram, Chromepet, Guindy, "
    "Meenambakkam, Pallavaram, Egmore, Nungambakkam, Mylapore, Besant Nagar, "
    "OMR, ECR, GST Road, Mount Road, Marina Beach, Central Station, "
    "airport, railway station, bus stand, hospital, temple, church, mosque, "
    "market, mall, school, college, university, park, beach, bridge, "
    "yes, no, one, two, three, four, five, confirm, cancel, "
    "navigate, directions, walking, driving, route, destination"
)


def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        try:
            from faster_whisper import WhisperModel
            print(f"[WHISPER] Loading '{_WHISPER_MODEL_SIZE}' model (first run downloads ~460 MB)...")
            _whisper_model = WhisperModel(
                _WHISPER_MODEL_SIZE, device="cpu", compute_type="int8"
            )
            print("[WHISPER] Ready.")
        except Exception as exc:
            print(f"[WHISPER] Could not load model: {exc}")
            _whisper_model = "FAILED"
    return None if _whisper_model == "FAILED" else _whisper_model


# Known Whisper hallucinations triggered by background noise / silence
_WHISPER_NOISE_PHRASES = frozenset({
    "thank you", "thank you very much", "thanks for watching",
    "thanks", "thank you so much", "please subscribe", "subscribe",
    "bye", "goodbye", "see you", "see you next time", "that's it",
    "i love you", "oh", "hmm", "um", "uh", "ah",
    "you", "i", "the", "a", "and",
})


def _rms(data: bytes) -> float:
    """Root-mean-square amplitude of a raw PCM int16 chunk."""
    samples = struct.unpack(f"{len(data) // 2}h", data)
    return (sum(s * s for s in samples) / len(samples)) ** 0.5


def _record_to_wav(max_secs: int = 8, sample_rate: int = 16000,
                   silence_secs: float = 0.3) -> str | None:
    """
    Record from the microphone with dynamic noise calibration.
    Always-active listening: cuts speech on short silence (0.3s).

    Step 1 — Calibrate: measure ambient RMS for 0.5 s before recording.
    Step 2 — Record: speech threshold = max(600, ambient_rms * 2.5).
              Only amplitude significantly ABOVE the ambient level is
              treated as the user's voice, so road noise doesn't trigger
              'speech detected' and keeps the recording alive forever.
    Step 3 — Stop when 0.3 s of post-speech silence is detected (quick cutoff),
              or max_secs is reached.

    Returns path to a temporary WAV file, or None if no real speech found.
    """
    CHUNK = 1024
    CALIB_SECS = 0.5     # seconds of ambient sampling before recording

    pa = pyaudio.PyAudio()
    try:
        stream = pa.open(
            format=pyaudio.paInt16, channels=1, rate=sample_rate,
            input=True, frames_per_buffer=CHUNK,
        )
    except Exception:
        pa.terminate()
        return None

    try:
        # ── Step 1: calibrate ambient noise ──────────────────────────────
        calib_chunks = int(sample_rate / CHUNK * CALIB_SECS)
        ambient_rms_vals: list[float] = []
        for _ in range(calib_chunks):
            data = stream.read(CHUNK, exception_on_overflow=False)
            ambient_rms_vals.append(_rms(data))
        ambient_rms = sum(ambient_rms_vals) / len(ambient_rms_vals)

        # Speech threshold: well above ambient but capped at a sane minimum
        SPEECH_THRESHOLD  = max(600, ambient_rms * 2.5)
        SILENCE_THRESHOLD = max(500, ambient_rms * 1.8)
        silence_chunks_needed = int(sample_rate / CHUNK * silence_secs)

        # ── Step 2: record until silence or timeout ───────────────────────
        frames: list[bytes] = []
        silence_chunks = 0
        saw_speech = False
        deadline = time.time() + max_secs

        while time.time() < deadline:
            data = stream.read(CHUNK, exception_on_overflow=False)
            frames.append(data)
            level = _rms(data)
            if level >= SPEECH_THRESHOLD:
                saw_speech = True
                silence_chunks = 0
            elif level < SILENCE_THRESHOLD:
                silence_chunks += 1
                if saw_speech and silence_chunks >= silence_chunks_needed:
                    break  # user finished speaking (quick cutoff)
            # else: between thresholds → keep recording, don't count silence

    finally:
        try:
            if stream.is_active():
                stream.stop_stream()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass
        pa.terminate()

    if not frames or not saw_speech:
        return None

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    with wave.open(tmp.name, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"".join(frames))
    return tmp.name


def _listen_whisper(timeout_secs: int = 6) -> str | None:
    """
    Transcribe with faster-whisper, tuned for always-active listening.
    Returns immediately on brief silence (0.3s) - cuts half-sentences.

    Noise-rejection layers:
      1. Dynamic ambient calibration in _record_to_wav filters mic input.
      2. vad_filter=True  — Whisper's own VAD skips silent segments.
      3. temperature=0    — greedy decode: refuses to hallucinate guesses.
      4. no_speech_prob   — discard any segment where Whisper itself
                            rates the chance of real speech < 50%.
      5. Hallucination blocklist — known Whisper noise-phrases are dropped.
    """
    model = _get_whisper_model()
    if model is None:
        return None

    wav_path = _record_to_wav(max_secs=timeout_secs)
    if not wav_path:
        return None

    try:
        segments, _ = model.transcribe(
            wav_path,
            language="en",
            initial_prompt=_WHISPER_INITIAL_PROMPT,  # prime with Indian names
            vad_filter=True,                  # VAD removes silent parts
            vad_parameters={"threshold": 0.6},# stricter VAD (0.5 default)
            beam_size=5,
            temperature=0,                    # greedy: no hallucination
            condition_on_previous_text=False, # no drift from prior phrases
            no_speech_threshold=0.5,          # discard low-confidence segs
        )
        valid: list[str] = []
        for seg in segments:
            # Reject if Whisper's own confidence in hearing speech is < 50%
            if seg.no_speech_prob >= 0.5:
                continue
            part = seg.text.strip()
            # Strip punctuation for blocklist check only
            part_clean = re.sub(r"[^\w\s]", "", part).strip().lower()
            if part_clean and part_clean not in _WHISPER_NOISE_PHRASES:
                valid.append(part)
        text = " ".join(valid).strip()
        return text if text else None
    except Exception:
        return None
    finally:
        try:
            os.unlink(wav_path)
        except OSError:
            pass


def _listen_google_cloud(timeout_secs: int = 6, phrase_hints: list[str] | None = None) -> str | None:
    """
    Use Google Cloud Speech-to-Text for higher accuracy, with optional phrase hints.

    Requires: Set `GOOGLE_APPLICATION_CREDENTIALS` env var to a valid service account JSON.
    Returns transcribed text or None.
    """
    if not _GCS_OK:
        return None

    wav_path = _record_to_wav(max_secs=timeout_secs)
    if not wav_path:
        return None

    try:
        client = gcs_speech.SpeechClient()
        with open(wav_path, "rb") as f:
            content = f.read()

        audio = {"content": content}
        speech_contexts = []
        if phrase_hints:
            # chunk hints to reasonable size
            hints = [h for h in phrase_hints if h]
            speech_contexts = [{"phrases": hints[:200]}]

        config = {
            "encoding": gcs_speech.RecognitionConfig.AudioEncoding.LINEAR16,
            "sample_rate_hertz": 16000,
            "language_code": "en-IN",
            "max_alternatives": 3,
            "enable_automatic_punctuation": True,
            "speech_contexts": speech_contexts,
            "model": "default",
        }

        request = {"config": config, "audio": audio}
        response = client.recognize(request=request)
        results = []
        for res in response.results:
            if res.alternatives:
                results.append(res.alternatives[0].transcript)

        text = " ".join(results).strip()
        return text if text else None
    except Exception:
        return None
    finally:
        try:
            os.unlink(wav_path)
        except OSError:
            pass



# ── Google STT (fallback only) ──────────────────────────────────────────────
_recogniser = sr.Recognizer()
_recogniser.pause_threshold          = 0.3   # Respond quickly on pause (was 1.2)
_recogniser.dynamic_energy_threshold = True


def _listen_google(timeout_secs: int = 5) -> str | None:
    """Fallback STT via Google Web Speech API (requires internet)."""
    try:
        with sr.Microphone() as mic:
            _recogniser.adjust_for_ambient_noise(mic, duration=0.1)
            audio = _recogniser.listen(mic, timeout=timeout_secs,
                                       phrase_time_limit=8)
        text = _recogniser.recognize_google(audio, language="en-IN")
        return text.strip()
    except Exception:
        return None



def _parse_spoken_choice(text: str) -> str:
    """
    Normalise a spoken response so Y/N confirmations and numbered menu
    selections resolve correctly.

    Strips punctuation Whisper adds to short words (e.g. "Yes." → "yes",
    "No," → "no") before matching.

    Mappings
    --------
    yes / yeah / yep / correct / confirm / ok / okay / sure  →  "Y"
    no  / nope / cancel / stop / back                        →  "N"
    one / first … five / fifth                               →  "1"…"5"
    """
    _yn = {
        "yes": "Y", "yeah": "Y", "yep": "Y", "correct": "Y",
        "confirm": "Y", "ok": "Y", "okay": "Y", "sure": "Y",
        "no": "N", "nope": "N", "cancel": "N", "stop": "N", "back": "N",
    }
    _nums = {
        "one": "1", "first": "1",
        "two": "2", "second": "2",
        "three": "3", "third": "3",
        "four": "4", "fourth": "4",
        "five": "5", "fifth": "5",
    }
    # Strip punctuation Whisper appends to short words ("Yes." "No," "Two!")
    cleaned = re.sub(r"[^\w\s]", "", text).strip()
    lower   = cleaned.lower()
    if lower in ("y", "n", "1", "2", "3", "4", "5"):
        return lower.upper() if lower in ("y", "n") else lower
    for word, val in _yn.items():
        if word == lower or lower.startswith(word + " "):
            return val
    for word, digit in _nums.items():
        if word in lower:
            return digit
    return text   # return original (preserve punctuation in place names)


def _listen_once() -> str | None:
    """
    Wait for TTS to clear, then transcribe one spoken utterance.
    Mic is always-active: responds to brief pauses (0.3s).
    
    Since speak() now runs in background thread, listening starts immediately.
    Priority: Whisper (offline, any vocabulary) → Google STT (online, fallback).
    Returns the normalised text, or None if nothing was understood.
    """
    # Minimal wait since TTS is running in background
    time.sleep(0.1)        # Let TTS engine stabilize audio device
    print("[MIC] Listening (always-active · responds on pause)...")

    # Primary: Whisper — works for ANY words including place names
    text = _listen_whisper(timeout_secs=6)

    # Fallback: Google STT
    if not text:
        text = _listen_google(timeout_secs=5)

    if text:
        text = _parse_spoken_choice(text)
        print(f"[MIC] Heard: {text}")
        return text.strip()
    return None


def _clear_tts_queue() -> None:
    """
    Clear all remaining messages from TTS queue and STOP currently playing audio.
    ⚡ Instant cutoff - skips remaining audio and moves forward immediately.
    """
    global _tts_queue
    
    # Stop the currently playing TTS immediately
    _stop_tts_immediately()
    
    # Clear all queued messages
    try:
        while True:
            _tts_queue.get_nowait()
            _tts_queue.task_done()
    except queue_module.Empty:
        pass


def _wait_for_tts() -> None:
    """Wait for TTS queue to empty (all messages played)."""
    _tts_queue.join()  # Blocks until queue is processed


def listen(prompt: str, max_retries: int = 3) -> str:
    """
    Speak the prompt, then listen for a voice response.
    Mic listens WHILE system is speaking (sequential TTS in background).
    
    ⚡ When user answers, immediately skip remaining audio and move forward.

    Fully voice-driven — no keyboard input at any point.
    Retries up to `max_retries` times with short retry prompts between attempts.
    Returns whatever was recognised, or an empty string if all retries fail.
    """
    speak(prompt)  # Queue TTS (returns immediately)
    
    for attempt in range(max_retries):
        text = _listen_once()
        if text:
            _clear_tts_queue()  # Skip remaining audio, move forward immediately
            return text

        # Tell user to try again (shorter prompts to save time)
        if attempt < max_retries - 1:
            speak("Please try again." if attempt == 0 else "Once more.")

    # All retries exhausted — give up gracefully
    speak("I couldn't hear that. Try in a quieter area.")
    _wait_for_tts()  # Let final message play
    return ""





# ---------------------------------------------------------------------------
# 1. Start Location Input
# ---------------------------------------------------------------------------

def get_start_location() -> str:
    """
    Fallback: ask the user to say their starting point.
    Only called when device GPS is unavailable or rejected by the user.
    """
    location = ""
    while not location:
        location = listen("Where are you starting from?")
        if not location:
            speak("Let's try again.")
    return location


# ---------------------------------------------------------------------------
# 1b. Device GPS
# ---------------------------------------------------------------------------

async def _gps_async() -> tuple[float, float]:
    """Coroutine: request one high-accuracy fix from the Windows Location API."""
    locator = Geolocator()
    locator.desired_accuracy = PositionAccuracy.HIGH
    pos = await locator.get_geoposition_async()
    c = pos.coordinate
    return float(c.latitude), float(c.longitude)


def get_device_gps(timeout_secs: int = 12) -> tuple[float, float] | None:
    """
    Fetch the device's current GPS position via the Windows Location API.

    Requires:
      - ``pip install winsdk``
      - Windows 10/11 with Location Services enabled for this app.

    Returns:
        (latitude, longitude) on success, or None if unavailable.
    """
    if not _WINSDK_OK:
        return None
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(
            asyncio.wait_for(_gps_async(), timeout=timeout_secs)
        )
        loop.close()
        return result
    except Exception:
        return None


def reverse_geocode(lat: float, lng: float, api_key: str) -> str:
    """
    Convert GPS coordinates to a human-readable address via the Geocoding API.
    Returns only the street/area name (not full address) to keep speech brief.

    Returns the address string, or a short fallback.
    """
    try:
        resp = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"latlng": f"{lat},{lng}", "key": api_key},
            timeout=8,
        )
        data = resp.json()
        if data.get("status") == "OK" and data.get("results"):
            full = data["results"][0]["formatted_address"]
            # Extract just first component (street/area name only)
            parts = full.split(",")
            return parts[0].strip()
    except requests.RequestException:
        pass
    return f"Current location"


# ---------------------------------------------------------------------------
# 2. Destination Input
# ---------------------------------------------------------------------------

def get_destination() -> str:
    """
    Ask the user where they want to go.

    Returns:
        A plain-text destination string (e.g., "Connaught Place, New Delhi").
    """
    destination = ""
    while not destination:
        destination = listen("Where do you want to go?")
        if not destination:
            speak("Let's try again.")
    return destination


# ---------------------------------------------------------------------------
# 3. Route Confirmation
# ---------------------------------------------------------------------------

def confirm_route(start: str, destination: str) -> bool:
    """
    Read back the planned trip and ask the user to confirm before fetching.

    Returns:
        True if the user confirms, False otherwise.
    """
    speak(
        f"From {start} to {destination}. Confirm?"
    )
    answer = listen("Say yes or no.").upper()

    if answer in ("Y", "YES"):
        speak("Fetching route...")
        return True

    speak("Cancelled.")
    return False


# ---------------------------------------------------------------------------
# 2b. Location Search & Resolution
# ---------------------------------------------------------------------------

def search_places(query: str, api_key: str) -> list[dict]:
    """
    Search for named places using the Google Places Text Search API.

    Unlike the Geocoding API, this correctly resolves business names,
    schools, restaurants, landmarks etc. — returning up to 5 specific
    candidates rather than guessing or falling back to the city centroid.

    Returns:
        List of place dicts (may be empty on failure):
          {"name", "address", "lat", "lng", "place_id"}
    """
    url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    params = {"query": query, "key": api_key}
    try:
        resp = requests.get(url, params=params, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "OK":
            results = []
            for r in data.get("results", [])[:5]:
                results.append({
                    "name":     r.get("name", ""),
                    "address":  r.get("formatted_address", ""),
                    "lat":      r["geometry"]["location"]["lat"],
                    "lng":      r["geometry"]["location"]["lng"],
                    "place_id": r.get("place_id", ""),
                })
            return results
    except requests.RequestException:
        pass
    return []


def geocode_location(query: str, api_key: str) -> dict | None:
    """
    Resolve a free-text address query using the Google Geocoding API.
    Used as a fallback when Places Text Search finds nothing.

    Returns a dict {address, lat, lng, place_id} or None.
    """
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": query, "key": api_key}
    try:
        resp = requests.get(url, params=params, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "OK" and data.get("results"):
            result = data["results"][0]
            return {
                "address":  result["formatted_address"],
                "lat":      result["geometry"]["location"]["lat"],
                "lng":      result["geometry"]["location"]["lng"],
                "place_id": result.get("place_id", ""),
            }
    except requests.RequestException:
        pass
    return None


def _place_to_resolved(p: dict) -> dict:
    """Package a place result into the {label, coord} dict navigate() expects."""
    # Extract just the place name (before any comma, keep it short)
    name = p['name'].split(",")[0].strip() if p.get("name") else p["address"].split(",")[0].strip()
    return {"label": name, "coord": f"{p['lat']},{p['lng']}"}


def _fuzzy_match_place(spoken: str, places: list[dict]) -> dict | None:
    """
    Fuzzy-match a noisy spoken string to the best place candidate.

    Uses simple normalization + SequenceMatcher ratio and token overlap.
    Returns the best place dict or None if no confident match.
    """
    import difflib

    def normalize(s: str) -> str:
        s2 = re.sub(r"[^\w\s]", "", s).lower().strip()
        return s2

    spoken_n = normalize(spoken)
    best_score = 0.0
    best_place = None

    for p in places:
        name = p.get("name") or p.get("address") or ""
        name_n = normalize(name)

        # direct containment boost
        if spoken_n in name_n or name_n in spoken_n:
            score = 0.95
        else:
            # sequence matcher ratio
            seq = difflib.SequenceMatcher(None, spoken_n, name_n)
            score = seq.ratio()

            # token overlap boost
            s_tokens = set(spoken_n.split())
            n_tokens = set(name_n.split())
            if s_tokens and n_tokens:
                overlap = len(s_tokens & n_tokens) / max(1, len(s_tokens | n_tokens))
                # combine heuristics
                score = max(score, 0.6 * score + 0.4 * overlap)

        if score > best_score:
            best_score = score
            best_place = p

    # require a reasonably high confidence to accept
    if best_score >= 0.55:
        return best_place
    return None


def resolve_location(raw_query: str, role: str, api_key: str) -> dict:
    """
    Resolve a typed location to an exact GPS coordinate, with confirmation.

    Strategy (three tiers):
      1. Places Text Search API: handles business names, schools, landmarks.
         Returns up to 5 candidates; user picks from a numbered list.
      2. Geocoding API fallback: for pure address strings.
      3. Raw-text pass-through: if all APIs fail, uses the string as-is.

    At any confirmation prompt the user can type a new search term and it
    is re-searched immediately without restarting.

    Returns:
        {"label": str, "coord": "lat,lng"}
    """
    query = raw_query

    while True:
        speak(f'Searching: "{query}"...')

        # ── Tier 1: Places Text Search (best for named places) ──────────
        places = search_places(query, api_key)

        if places:
            # If we have candidates, try to match spoken responses robustly.
            if len(places) == 1:
                p = places[0]
                speak(f"Found: {p['name']}. Confirm?")
                # Prefer Google Cloud STT with phrase hints for place confirmations
                answer = None
                if _GCS_OK:
                    hints = [p.get('name', ''), 'yes', 'no', 'y', 'n', 'one', 'two', 'three', '1', '2', '3']
                    answer = _listen_google_cloud(timeout_secs=6, phrase_hints=hints)
                if not answer:
                    answer = _listen_once()
                if answer:
                    answer = _parse_spoken_choice(answer)
                else:
                    answer = listen("Say yes.").strip()

                if answer and answer.upper() in ("Y", "YES", ""):
                    return _place_to_resolved(p)
                elif answer:
                    query = answer
                    continue
                else:
                    # fallback to explicit confirmation prompt
                    answer = listen("Say yes to confirm, or say a different place name.").strip()
                    if answer.upper() in ("Y", "YES", ""):
                        return _place_to_resolved(p)
                    query = answer
                    continue

            else:
                # ── Build ONE announcement with all candidates, then listen ONCE
                lines = [f"{len(places)} places found."]
                for i, p in enumerate(places, start=1):
                    lines.append(f"Option {i}: {p['name']}.")
                lines.append(f"Say one to {len(places)}.")
                speak("\n".join(lines))

                # Listen once quickly (always-active mic).
                # Try Google Cloud STT first with place name hints to improve accuracy.
                answer = None
                if _GCS_OK:
                    hints = [p.get('name', '') for p in places] + ['one','two','three','1','2','3','yes','no','y','n']
                    answer = _listen_google_cloud(timeout_secs=6, phrase_hints=hints)
                if not answer:
                    answer = _listen_once()
                if answer:
                    answer = _parse_spoken_choice(answer).strip()
                else:
                    answer = listen("Which one?").strip()

                # If digit, choose directly
                if answer.isdigit() and 1 <= int(answer) <= len(places):
                    return _place_to_resolved(places[int(answer) - 1])

                # Otherwise try fuzzy-match the spoken phrase to candidate names
                if answer:
                    best = _fuzzy_match_place(answer, places)
                    if best is not None:
                        return _place_to_resolved(best)
                    # If fuzzy-match failed, treat answer as new query
                    query = answer
                    continue
                else:
                    return _place_to_resolved(places[0])

        # ── Tier 2: Geocoding fallback (for address strings) ────────────
        else:
            geo = geocode_location(query, api_key)
            if geo:
                speak(f"Found: {geo['address']}. Confirm?")
                answer = listen("Say yes.").strip()
                if answer.upper() in ("Y", "YES", ""):
                    return {"label": geo["address"], "coord": f"{geo['lat']},{geo['lng']}"}
                else:
                    query = answer
                    continue

            # ── Tier 3: nothing worked — ask again ──────────────────────
            else:
                speak(f"No match for {query}. Be specific.")
                new_query = listen("Try again.").strip()
                if new_query:
                    query = new_query
                else:
                    speak("Using original location.")
                    return {"label": query, "coord": query}





# ---------------------------------------------------------------------------
# 4. Google Maps Routing — helpers
# ---------------------------------------------------------------------------

def _clean_instruction(raw_html: str) -> str:
    """
    Convert a Google Maps html_instructions string to clean plain text.

    Steps:
      1. Replace <div…> tags (used for "Pass by" hints) with " | " so they
         remain readable but distinct from the main manoeuvre.
      2. Strip all remaining HTML tags.
      3. Decode HTML entities (&amp; → &, &nbsp; → space, etc.).
    """
    # Separate sub-instructions that Google wraps in <div …>
    cleaned = re.sub(r"<div[^>]*>", "  ↳ ", raw_html)
    cleaned = cleaned.replace("</div>", "")
    # Strip all remaining tags
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    # Decode HTML entities
    cleaned = html_module.unescape(cleaned)
    return cleaned.strip()


def _decode_polyline(encoded: str) -> list[tuple[float, float]]:
    """
    Decode a Google Maps Encoded Polyline string into (lat, lng) pairs.

    The overview_polyline returned by the Directions API contains hundreds of
    intermediate GPS points, making the map line far smoother and more accurate
    than the handful of step start/end coordinates.
    """
    coords: list[tuple[float, float]] = []
    index, lat, lng = 0, 0, 0

    while index < len(encoded):
        for coord_slot in range(2):          # 0 = lat, 1 = lng
            shift, result = 0, 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if (result & 1) else (result >> 1)
            if coord_slot == 0:
                lat += delta
            else:
                lng += delta
        coords.append((lat / 1e5, lng / 1e5))

    return coords


# ---------------------------------------------------------------------------
# 4b. Blind-Accessibility Speech Formatters
# ---------------------------------------------------------------------------

# Mapping of Google's compass headings to plain language a blind user can act on.
_HEADING_MAP = {
    "north":     "Walk straight ahead",
    "south":     "Walk straight ahead",
    "east":      "Walk straight ahead",
    "west":      "Walk straight ahead",
    "northeast": "Walk diagonally to your right",
    "northwest": "Walk diagonally to your left",
    "southeast": "Walk diagonally to your right",
    "southwest": "Walk diagonally to your left",
}
_HEADING_RE = re.compile(
    r"^Head\s+(north|south|east|west|northeast|northwest|southeast|southwest)\b",
    re.IGNORECASE,
)


def _normalize_heading(text: str) -> str:
    """
    Replace a compass-based 'Head <direction>' prefix with plain language.

    "Head west on Mettu St" → "Walk straight ahead on Mettu St"
    "Head northeast toward …" → "Walk diagonally to your right toward …"
    """
    m = _HEADING_RE.match(text)
    if not m:
        return text
    replacement = _HEADING_MAP.get(m.group(1).lower(), "Walk straight ahead")
    return _HEADING_RE.sub(replacement, text, count=1)


def _dist_to_steps(distance_text: str) -> str:
    """
    Convert a distance string ("17 m" or "0.3 km") to an approximate step count.

    Uses an average stride length of ~0.75 m.  Returns an empty string if the
    input cannot be parsed (so callers can skip the hint safely).
    """
    try:
        text = distance_text.lower().strip()
        if "km" in text:
            metres = float(text.replace("km", "").strip()) * 1000
        else:
            metres = float(text.replace("m", "").strip())
        steps = max(1, round(metres / 0.75))
        return f"~{steps:,} steps"
    except (ValueError, AttributeError):
        return ""


def _format_step_for_speech(step: dict, idx: int, total: int) -> str:
    """
    Format a single navigation step for a blind user.

    Design principles:
      - Lead with the ACTION word (TURN LEFT / TURN RIGHT / WALK STRAIGHT).
      - State the landmark or street name that triggers the action.
      - Give time + step count so the user knows how far to walk.
      - Demote "Pass by" hints to CONFIRMATION lines — they are audio cues
        that tell the user they are on the right path, not new instructions.
      - "Destination will be on the right/left" is promoted as a final landmark.

    Args:
        step:  A step dict from fetch_routes().
        idx:   1-based step index.
        total: Total number of steps in the route.

    Returns:
        A multi-line string ready to be passed to speak().
    """
    instruction = step["instruction"]
    distance    = step["distance"]
    duration    = step["duration"]

    # --- Split main action from ↳ sub-hints ---------------------------------
    parts        = instruction.split("↳")
    main_action  = _normalize_heading(parts[0].strip())
    sub_hints    = [p.strip() for p in parts[1:] if p.strip()]

    # --- Approximate walking steps ------------------------------------------
    step_count = _dist_to_steps(distance)
    walk_line  = f"  Walk for {distance}  ({step_count})".rstrip()

    # --- Bucket sub-hints into confirmation cues or destination notices ------
    confirmations: list[str] = []
    destination_hints: list[str] = []
    for hint in sub_hints:
        lower = hint.lower()
        if "destination" in lower:
            destination_hints.append(hint)
        else:
            # Reword "Pass by X" → "You should pass X" for natural speech
            reworded = re.sub(
                r"(?i)^pass by\s+", "You will pass ", hint
            )
            confirmations.append(reworded)

    # --- Assemble the block (compact format) -------------------------------
    lines = [
        f"Step {idx}: {main_action}",
        walk_line,
    ]

    for cue in confirmations:
        lines.append(f"  {cue}")

    for hint in destination_hints:
        lines.append(f"  {hint}")

    return "  ".join(lines)


# ---------------------------------------------------------------------------
# 5. Google Maps Routing — main fetch
# ---------------------------------------------------------------------------

def fetch_routes(
    start: str,
    destination: str,
    api_key: str,
    max_alternatives: int = 1,
) -> list[dict]:
    """
    Fetch up to `max_alternatives` walking routes from the Directions API.

    Returns:
        A list of route dicts, each containing:
          {
            "summary":          str,              # Google's route label
            "total_distance":   str,              # e.g. "1.2 km"
            "total_duration":   str,              # e.g. "18 mins"
            "steps":            list[dict],       # see below
            "polyline_coords":  list[(lat,lng)],  # decoded overview polyline
          }

        Each step dict:
          {
            "instruction":  str,    # clean plain-text manoeuvre
            "distance":     str,
            "duration":     str,
            "start_lat":    float,
            "start_lng":    float,
            "end_lat":      float,
            "end_lng":      float,
          }

    Raises:
        ConnectionError: On network failure.
        ValueError:      On API error status.
    """
    url = "https://maps.googleapis.com/maps/api/directions/json"
    params = {
        "origin":        start,
        "destination":   destination,
        "mode":         "walking",
        "alternatives": "true" if max_alternatives > 1 else "false",
        "avoid":        "ferries",          # never route a pedestrian on a ferry
        "units":        "metric",
        "key":           api_key,
    }

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ConnectionError(
            f"Failed to reach the Directions API: {exc}"
        ) from exc

    data = response.json()
    status = data.get("status", "UNKNOWN")

    if status != "OK":
        error_msg = data.get("error_message", "No additional info.")
        raise ValueError(
            f"Directions API status '{status}'. Details: {error_msg}"
        )

    raw_routes = data.get("routes", [])[:max_alternatives]
    if not raw_routes:
        raise ValueError("API returned no routes.")

    routes = []
    for raw_route in raw_routes:
        leg = raw_route["legs"][0]          # walking = single leg

        # Decode overview polyline for smooth map line
        encoded = raw_route.get("overview_polyline", {}).get("points", "")
        polyline_coords = _decode_polyline(encoded) if encoded else []

        # Parse steps
        steps = []
        for raw_step in leg["steps"]:
            steps.append({
                "instruction": _clean_instruction(
                    raw_step.get("html_instructions", "")
                ),
                "distance":  raw_step.get("distance", {}).get("text", "?"),
                "duration":  raw_step.get("duration", {}).get("text", "?"),
                "start_lat": raw_step["start_location"]["lat"],
                "start_lng": raw_step["start_location"]["lng"],
                "end_lat":   raw_step["end_location"]["lat"],
                "end_lng":   raw_step["end_location"]["lng"],
            })

        routes.append({
            "summary":         raw_route.get("summary", "Route"),
            "total_distance":  leg.get("distance", {}).get("text", "?"),
            "total_duration":  leg.get("duration", {}).get("text", "?"),
            "steps":           steps,
            "polyline_coords": polyline_coords,
        })

    return routes


# ---------------------------------------------------------------------------
# 6. Route Selection
# ---------------------------------------------------------------------------

def select_route(routes: list[dict]) -> dict:
    """
    If multiple alternatives exist, present them and let the user pick one.

    Returns:
        The selected route dict.
    """
    if len(routes) == 1:
        r = routes[0]
        speak(
            f"Route: {r['total_distance']}, {r['total_duration']}."
        )
        return r

    # Multiple alternatives — batch all into one announcement then listen once
    lines = [f"{len(routes)} routes found."]
    for i, r in enumerate(routes, start=1):
        lines.append(
            f"Option {i}: {r['total_distance']}, {r['total_duration']}."
        )
    lines.append("Say one, two, or three.")
    speak("\n".join(lines))

    while True:
        choice = listen("Which route?")
        if not choice:
            return routes[0]
        if choice.isdigit() and 1 <= int(choice) <= len(routes):
            return routes[int(choice) - 1]
        speak("Try again.")


# ---------------------------------------------------------------------------
# 7. Navigation Loop
# ---------------------------------------------------------------------------

# Metres from a step's trigger point before we announce that step
_STEP_TRIGGER_M = 20
# Metres from the final destination before we announce arrival
_ARRIVAL_M = 15
# GPS poll interval in seconds
_GPS_POLL_S = 2


def _haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres between two GPS points."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi   = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def navigate(route: dict) -> None:
    """
    Real-time GPS-driven navigation loop.

    Behaviour
    ---------
    1. Announce step 1 immediately (user is already at the start).
    2. Poll device GPS every _GPS_POLL_S seconds.
    3. When the user is within _STEP_TRIGGER_M metres of a step's start
       point, announce that step (turn instruction + distance).
    4. When within _ARRIVAL_M metres of the final waypoint, announce arrival.

    If GPS is unavailable (no winsdk / location services off), falls back
    to a simple time-based loop as a degraded mode.
    """
    steps = route["steps"]
    total = len(steps)

    # ── Journey summary ──────────────────────────────────────────────────
    speak(f"Navigation started. {route['total_distance']}, {route['total_duration']}.")

    # Announce step 1 immediately
    speak(_format_step_for_speech(steps[0], 1, total))

    # ── Degraded mode: no GPS ─────────────────────────────────────────────
    if not _WINSDK_OK:
        speak("GPS unavailable. Steps advance every 10 seconds.")
        for idx in range(1, total):   # steps[1] … steps[total-1]
            time.sleep(10)
            speak(_format_step_for_speech(steps[idx], idx + 1, total))
        speak("Arrived. Navigation complete.")
        return

    # ── Live GPS loop ─────────────────────────────────────────────────────
    next_idx = 1      # index (0-based) of the NEXT step yet to be announced
    announced_near = False   # avoid re-announcing while still close to a point

    while True:
        time.sleep(_GPS_POLL_S)
        gps = get_device_gps(timeout_secs=4)
        if not gps:
            continue
        lat, lng = gps

        if next_idx < total:
            # ── Check proximity to next turn point ──────────────────────
            tgt = steps[next_idx]
            dist = _haversine(lat, lng, tgt["start_lat"], tgt["start_lng"])
            if dist <= _STEP_TRIGGER_M:
                if not announced_near:
                    speak(_format_step_for_speech(tgt, next_idx + 1, total))
                    next_idx += 1
                    announced_near = True
            else:
                announced_near = False   # moved away — reset for next step
        else:
            # ── All turns announced — check final arrival ────────────────
            last = steps[-1]
            dist = _haversine(lat, lng, last["end_lat"], last["end_lng"])
            if dist <= _ARRIVAL_M:
                speak("Arrived.")
                return


# ---------------------------------------------------------------------------
# 8. Route Map Viewer
# ---------------------------------------------------------------------------

def open_route_map(route: dict, start_label: str, dest_label: str) -> None:
    """
    Generate a standalone Leaflet.js HTML file from the route data and open it.

    Uses the decoded overview_polyline for a smooth, pixel-accurate route line.
    Each navigation step gets a numbered marker; clicking it shows the full
    instruction so you can verify correctness at a glance.

    Args:
        route:       Selected route dict from fetch_routes().
        start_label: Human-readable start string.
        dest_label:  Human-readable destination string.
    """
    steps           = route["steps"]
    polyline_coords = route["polyline_coords"]
    total_steps     = len(steps)
    total_pts       = len(polyline_coords)

    # ------------------------------------------------------------------
    # Fallback: if polyline decoding failed, fall back to step endpoints
    # ------------------------------------------------------------------
    if not polyline_coords:
        for step in steps:
            pt = (step["start_lat"], step["start_lng"])
            if not polyline_coords or polyline_coords[-1] != pt:
                polyline_coords.append(pt)
        last = (steps[-1]["end_lat"], steps[-1]["end_lng"])
        if polyline_coords[-1] != last:
            polyline_coords.append(last)

    mid_lat = sum(c[0] for c in polyline_coords) / len(polyline_coords)
    mid_lng = sum(c[1] for c in polyline_coords) / len(polyline_coords)

    # ------------------------------------------------------------------
    # Build the smooth polyline JS array
    # ------------------------------------------------------------------
    poly_js = ", ".join(f"[{lat}, {lng}]" for lat, lng in polyline_coords)

    # ------------------------------------------------------------------
    # Build numbered step markers with instruction popups
    # ------------------------------------------------------------------
    marker_js_lines: list[str] = []

    # Start marker (green)
    s0 = steps[0]
    safe_start = start_label.replace("'", "\\'")
    marker_js_lines.append(
        f"L.marker([{s0['start_lat']}, {s0['start_lng']}], "
        f"{{icon: makeIcon('#22c55e', 'S')}}).addTo(map)"
        f".bindPopup('<b>START</b><br/>{safe_start}');"
    )

    # Intermediate step markers (blue, numbered)
    for i, step in enumerate(steps, start=1):
        safe_instr = (
            step["instruction"]
            .replace("\\", "\\\\")
            .replace("'", "\\'")
            .replace("\n", " ")
            .replace("↳", "↳")
        )
        lat, lng = step["start_lat"], step["start_lng"]
        # Pre-compute so we avoid backslash-in-f-string (Python < 3.12)
        step_dist = step["distance"]
        step_dur  = step["duration"]
        marker_js_lines.append(
            f"L.marker([{lat}, {lng}], "
            f"{{icon: makeIcon('#3b82f6', '{i}')}}).addTo(map)"
            f".bindPopup('<b>Step {i}/{total_steps}</b><br/>"
            f"{safe_instr}<br/>"
            f"<small>{step_dist} · ~{step_dur}</small>');"
        )


    # Destination marker (red)
    sl = steps[-1]
    safe_dest = dest_label.replace("'", "\\'")
    marker_js_lines.append(
        f"L.marker([{sl['end_lat']}, {sl['end_lng']}], "
        f"{{icon: makeIcon('#ef4444', 'D')}}).addTo(map)"
        f".bindPopup('<b>DESTINATION</b><br/>{safe_dest}');"
    )

    markers_js = "\n    ".join(marker_js_lines)

    # ------------------------------------------------------------------
    # Render full HTML
    # ------------------------------------------------------------------
    page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>HamLerc Route: {start_label} → {dest_label}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: 'Segoe UI', sans-serif; background: #0f1117; color: #e1e4e8; }}
    #header {{
      padding: 10px 20px; background: #161b22;
      border-bottom: 1px solid #30363d;
      display: flex; align-items: center; justify-content: space-between;
    }}
    #header h1 {{ font-size: 14px; font-weight: 700; letter-spacing: .3px; }}
    #header .meta {{ font-size: 12px; color: #8b949e; display: flex; gap: 18px; }}
    #header .badge {{
      background: #21262d; border: 1px solid #30363d; border-radius: 999px;
      padding: 2px 10px; font-size: 11px; font-weight: 600;
    }}
    #map {{ width: 100%; height: calc(100vh - 44px); }}
    .legend {{
      background: #161b22cc; border: 1px solid #30363d; border-radius: 8px;
      padding: 10px 14px; font-size: 12px; line-height: 2;
      backdrop-filter: blur(4px);
    }}
    .dot {{
      display: inline-flex; align-items: center; justify-content: center;
      width: 14px; height: 14px; border-radius: 50%;
      font-size: 9px; font-weight: 700; color: #fff; margin-right: 6px;
    }}
    .leaflet-popup-content {{ font-size: 13px; line-height: 1.5; min-width: 180px; }}
  </style>
</head>
<body>
  <div id="header">
    <h1>&#128205; HamLerc Route Verification</h1>
    <div class="meta">
      <span>&#128722; {start_label} &rarr; {dest_label}</span>
      <span class="badge">&#128644; {route['total_distance']}</span>
      <span class="badge">&#8987; ~{route['total_duration']}</span>
      <span class="badge">&#128203; {total_steps} steps</span>
      <span class="badge">&#128205; {total_pts} pts</span>
    </div>
  </div>
  <div id="map"></div>
  <script>
    // ── Custom icon factory ───────────────────────────────────────────
    function makeIcon(color, label) {{
      return L.divIcon({{
        className: '',
        html: `<div style="
          background:${{color}};
          width:22px;height:22px;border-radius:50%;
          border:2px solid #fff;
          display:flex;align-items:center;justify-content:center;
          font-size:9px;font-weight:700;color:#fff;
          box-shadow:0 1px 4px #0006;
        ">${{label}}</div>`,
        iconSize: [22, 22],
        iconAnchor: [11, 11],
        popupAnchor: [0, -12],
      }});
    }}

    // ── Map init ─────────────────────────────────────────────────────
    const map = L.map('map').setView([{mid_lat}, {mid_lng}], 15);
    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      maxZoom: 19,
    }}).addTo(map);

    // ── Smooth route polyline (decoded overview_polyline) ────────────
    const routeLine = L.polyline([{poly_js}], {{
      color: '#6366f1', weight: 5, opacity: 0.9,
      lineJoin: 'round', lineCap: 'round',
    }}).addTo(map);

    // Shadow line for depth
    L.polyline([{poly_js}], {{
      color: '#1e1b4b', weight: 9, opacity: 0.35,
      lineJoin: 'round', lineCap: 'round',
    }}).addTo(map).bringToBack();

    map.fitBounds(routeLine.getBounds(), {{padding: [40, 40]}});

    // ── Numbered step markers ────────────────────────────────────────
    {markers_js}

    // ── Legend ───────────────────────────────────────────────────────
    const legend = L.control({{position: 'bottomright'}});
    legend.onAdd = () => {{
      const div = L.DomUtil.create('div', 'legend');
      div.innerHTML = `
        <b style="font-size:13px">Route Legend</b><br/>
        <span class="dot" style="background:#22c55e">S</span>Start<br/>
        <span class="dot" style="background:#3b82f6">1</span>Step (click for instruction)<br/>
        <span class="dot" style="background:#ef4444">D</span>Destination<br/>
        <hr style="border-color:#30363d;margin:6px 0"/>
        <span style="color:#8b949e;font-size:11px">
          Route via {route['summary']}<br/>
          Polyline: {total_pts} GPS points
        </span>
      `;
      return div;
    }};
    legend.addTo(map);
  </script>
</body>
</html>"""

    # ------------------------------------------------------------------
    # Write to temp file and open
    # ------------------------------------------------------------------
    map_path = os.path.join(tempfile.gettempdir(), "hamlerc_route_map.html")
    with open(map_path, "w", encoding="utf-8") as f:
        f.write(page_html)

    print(f"[MAP] Opening map... ({total_pts} points, {total_steps} steps)")
    print(f"      File: {map_path}")
    webbrowser.open(f"file:///{map_path}")


# ---------------------------------------------------------------------------
# 9. Main Entry Point
# ---------------------------------------------------------------------------

def main() -> None:
    """
    Orchestrate the full navigation flow:
      1. Auto-fetch device GPS for start location (Windows Location API).
         Falls back to manual typing if GPS is unavailable or rejected.
      2. Reverse-geocode GPS coords to a human-readable address.
      3. Ask for destination and resolve it via Places API.
      4. Confirm the full trip.
      5. Fetch up to MAX_ALTERNATIVES routes using precise coordinates.
      6. User picks a route.
      7. Open the interactive map for visual verification.
      8. Run the step-by-step navigation loop.
    """
    print("\n=== HamLerc Navigation System ===")
    print("[INIT] Preloading speech model...")
    _get_whisper_model()  # Load at startup, not during first listen
    print("[INIT] Ready to navigate.")
    speak("Hi. Let's navigate.")

    # ── 1+2: Start location — try device GPS first ─────────────────────
    print("\n[INIT] Detecting GPS location...")
    gps = get_device_gps(timeout_secs=10)

    if gps:
        lat, lng = gps
        print(f"[GPS] Fix acquired: {lat:.5f}, {lng:.5f}")
        address = reverse_geocode(lat, lng, GOOGLE_MAPS_API_KEY)
        speak(f"Starting from {address}. Correct?")
        answer = listen("Say yes or say a new starting location.").strip()

        if answer.upper() in ("Y", "YES", ""):
            start = {"label": address, "coord": f"{lat},{lng}"}
        else:
            raw = answer if answer else get_start_location()
            start = resolve_location(raw, "starting location", GOOGLE_MAPS_API_KEY)
    else:
        print("[GPS] No fix available. Using manual input.")
        if not _WINSDK_OK:
            speak("GPS unavailable. Please say your location.")
        raw_start = get_start_location()
        start = resolve_location(raw_start, "starting location", GOOGLE_MAPS_API_KEY)

    # ── 3: Destination ─────────────────────────────────────────────────
    raw_dest    = get_destination()
    destination = resolve_location(raw_dest, "destination", GOOGLE_MAPS_API_KEY)

    # ── 4: Confirm trip ────────────────────────────────────────────────
    if not confirm_route(start["label"], destination["label"]):
        sys.exit(0)

    # ── 5: Fetch routes (precise coordinates) ──────────────────────────
    try:
        print("[FETCH] Getting routes...")
        routes = fetch_routes(
            start["coord"], destination["coord"], GOOGLE_MAPS_API_KEY,
            max_alternatives=MAX_ALTERNATIVES,
        )
    except (ConnectionError, ValueError) as exc:
        speak(f"Error: {str(exc)[:30]}")
        sys.exit(1)

    if not routes:
        speak("No route found.")
        sys.exit(1)

    # ── 6: Route selection ─────────────────────────────────────────────
    selected = select_route(routes)

    # ── 7: Map ─────────────────────────────────────────────────────────
    open_route_map(selected, start["label"], destination["label"])

    # ── 8: Navigate ────────────────────────────────────────────────────
    navigate(selected)


# ---------------------------------------------------------------------------
# Entry guard
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
