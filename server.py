"""
server.py
=========
FastAPI backend for the HamLerc Mobile Navigation App.

Runs on the PC and exposes the core navigation functions as REST endpoints.
The mobile PWA (served as static files) connects to this server over local WiFi.

Usage:
    python server.py
"""

import io
import os
import re
import socket
import struct
import tempfile
import wave
import math
import json
import threading

from fastapi import FastAPI, UploadFile, File, Request
from fastapi.responses import FileResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# ---------------------------------------------------------------------------
# Import core navigation functions
# ---------------------------------------------------------------------------
import core_navigation as nav

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = FastAPI(title="HamLerc Navigation API", version="1.0")

# Allow all origins for local network access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Preload Whisper model at startup
# ---------------------------------------------------------------------------
_whisper_ready = threading.Event()

def _preload_whisper():
    """Load the Whisper model in a background thread so server starts fast."""
    print("[SERVER] Preloading Whisper model...")
    nav._get_whisper_model()
    _whisper_ready.set()
    print("[SERVER] Whisper model ready.")

threading.Thread(target=_preload_whisper, daemon=True).start()

# ---------------------------------------------------------------------------
# Serve mobile PWA
# ---------------------------------------------------------------------------
MOBILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mobile")
os.makedirs(MOBILE_DIR, exist_ok=True)

@app.get("/")
async def serve_index():
    """Serve the mobile web app."""
    index_path = os.path.join(MOBILE_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path, media_type="text/html")
    return JSONResponse({"error": "Mobile app not found. Create mobile/index.html"}, 404)

# Mount static files for CSS, JS, manifest etc.
# Mounted at /mobile for legacy and also individual catch-all routes below.
app.mount("/mobile", StaticFiles(directory=MOBILE_DIR), name="mobile")

@app.get("/style.css")
async def serve_css():
    return FileResponse(os.path.join(MOBILE_DIR, "style.css"), media_type="text/css")

@app.get("/app.js")
async def serve_js():
    return FileResponse(os.path.join(MOBILE_DIR, "app.js"), media_type="application/javascript")

@app.get("/manifest.json")
async def serve_manifest():
    return FileResponse(os.path.join(MOBILE_DIR, "manifest.json"), media_type="application/json")

# ---------------------------------------------------------------------------
# API: Speech-to-Text
# ---------------------------------------------------------------------------
@app.post("/api/stt")
async def speech_to_text(audio: UploadFile = File(...)):
    """
    Receive a WAV audio file from the phone, transcribe it using Whisper.
    Returns: {"text": "transcribed text"} or {"text": null}
    """
    _whisper_ready.wait(timeout=120)  # wait for model to load

    model = nav._get_whisper_model()
    if model is None:
        return JSONResponse({"text": None, "error": "Whisper model not available"}, 500)

    # Save uploaded audio to temp file
    tmp_path = None
    try:
        audio_bytes = await audio.read()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        # Transcribe
        segments, _ = model.transcribe(
            tmp_path,
            language="en",
            initial_prompt=nav._WHISPER_INITIAL_PROMPT,
            vad_filter=True,
            vad_parameters={"threshold": 0.6},
            beam_size=5,
            temperature=0,
            condition_on_previous_text=False,
            no_speech_threshold=0.5,
        )

        valid = []
        for seg in segments:
            if seg.no_speech_prob >= 0.5:
                continue
            part = seg.text.strip()
            part_clean = re.sub(r"[^\w\s]", "", part).strip().lower()
            if part_clean and part_clean not in nav._WHISPER_NOISE_PHRASES:
                valid.append(part)

        text = " ".join(valid).strip()
        return {"text": text if text else None}
    except Exception as exc:
        return JSONResponse({"text": None, "error": str(exc)}, 500)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# API: Text-to-Speech
# ---------------------------------------------------------------------------
@app.post("/api/tts")
async def text_to_speech(request: Request):
    """
    Receive text, generate speech audio using pyttsx3, return WAV bytes.
    Body: {"text": "Hello navigation"}
    Returns: audio/wav binary
    """
    body = await request.json()
    text = body.get("text", "")
    if not text:
        return JSONResponse({"error": "No text provided"}, 400)

    # Sanitize for speech
    spoken = nav._sanitize_for_speech(text)
    if not spoken:
        return JSONResponse({"error": "Nothing to speak after sanitization"}, 400)

    tmp_path = None
    try:
        import pyttsx3
        tmp_path = tempfile.mktemp(suffix=".wav")

        engine = pyttsx3.init()
        engine.setProperty("rate", 180)  # slightly slower for phone speaker clarity
        engine.setProperty("volume", 1.0)
        engine.save_to_file(spoken, tmp_path)
        engine.runAndWait()
        engine.stop()

        if os.path.exists(tmp_path):
            return FileResponse(
                tmp_path,
                media_type="audio/wav",
                filename="speech.wav",
                background=None,
            )
        return JSONResponse({"error": "TTS failed to generate audio"}, 500)
    except Exception as exc:
        return JSONResponse({"error": f"TTS error: {exc}"}, 500)


# ---------------------------------------------------------------------------
# API: Search Places
# ---------------------------------------------------------------------------
@app.post("/api/search-places")
async def search_places(request: Request):
    """
    Search for named places using the Google Places Text Search API.
    Body: {"query": "Central Park"}
    Returns: {"places": [...]}
    """
    body = await request.json()
    query = body.get("query", "")
    if not query:
        return JSONResponse({"error": "No query provided"}, 400)

    places = nav.search_places(query, nav.GOOGLE_MAPS_API_KEY)
    return {"places": places or []}


# ---------------------------------------------------------------------------
# API: Geocode Location
# ---------------------------------------------------------------------------
@app.post("/api/geocode")
async def geocode(request: Request):
    """
    Resolve a free-text address query using the Google Geocoding API.
    Body: {"query": "1600 Amphitheatre Parkway"}
    Returns: {"result": {address, lat, lng, place_id}} or {"result": null}
    """
    body = await request.json()
    query = body.get("query", "")
    if not query:
        return JSONResponse({"error": "No query provided"}, 400)

    result = nav.geocode_location(query, nav.GOOGLE_MAPS_API_KEY)
    return {"result": result}


# ---------------------------------------------------------------------------
# API: Reverse Geocode
# ---------------------------------------------------------------------------
@app.post("/api/reverse-geocode")
async def reverse_geocode(request: Request):
    """
    Convert GPS coordinates to a human-readable address.
    Body: {"lat": 28.6139, "lng": 77.2090}
    Returns: {"address": "Connaught Place, New Delhi"}
    """
    body = await request.json()
    lat = body.get("lat")
    lng = body.get("lng")
    if lat is None or lng is None:
        return JSONResponse({"error": "lat and lng required"}, 400)

    address = nav.reverse_geocode(float(lat), float(lng), nav.GOOGLE_MAPS_API_KEY)
    return {"address": address}


# ---------------------------------------------------------------------------
# API: Fetch Routes
# ---------------------------------------------------------------------------
@app.post("/api/fetch-routes")
async def fetch_routes(request: Request):
    """
    Fetch walking routes from Google Directions API.
    Body: {"start": "lat,lng or address", "destination": "lat,lng or address", "max_alternatives": 3}
    Returns: {"routes": [...]}
    """
    body = await request.json()
    start = body.get("start", "")
    destination = body.get("destination", "")
    max_alt = body.get("max_alternatives", 3)

    if not start or not destination:
        return JSONResponse({"error": "start and destination required"}, 400)

    try:
        routes = nav.fetch_routes(start, destination, nav.GOOGLE_MAPS_API_KEY, max_alt)

        # Format steps for speech (pre-compute for mobile display)
        for route in routes:
            total = len(route["steps"])
            for i, step in enumerate(route["steps"]):
                step["speech"] = nav._format_step_for_speech(step, i + 1, total)

        return {"routes": routes}
    except (ConnectionError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, 500)


# ---------------------------------------------------------------------------
# API: Navigation helpers
# ---------------------------------------------------------------------------
@app.post("/api/haversine")
async def haversine(request: Request):
    """
    Compute distance between two GPS points.
    Body: {"lat1": ..., "lng1": ..., "lat2": ..., "lng2": ...}
    Returns: {"distance_m": float}
    """
    body = await request.json()
    dist = nav._haversine(
        float(body["lat1"]), float(body["lng1"]),
        float(body["lat2"]), float(body["lng2"]),
    )
    return {"distance_m": dist}


# ---------------------------------------------------------------------------
# API: Parse spoken choice (normalise Y/N, numbers)
# ---------------------------------------------------------------------------
@app.post("/api/parse-choice")
async def parse_choice(request: Request):
    """
    Normalise a spoken response.
    Body: {"text": "yes"}
    Returns: {"parsed": "Y"}
    """
    body = await request.json()
    text = body.get("text", "")
    parsed = nav._parse_spoken_choice(text)
    return {"parsed": parsed}


# ---------------------------------------------------------------------------
# Utility: Get local IP
# ---------------------------------------------------------------------------
def get_local_ip() -> str:
    """Get the PC's local network IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ---------------------------------------------------------------------------
# Tunnel: Cloudflare Quick Tunnel (free, no signup)
# ---------------------------------------------------------------------------
import subprocess
import sys
import zipfile
import urllib.request
import time as _time

CLOUDFLARED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cloudflared")
CLOUDFLARED_EXE = os.path.join(CLOUDFLARED_DIR, "cloudflared.exe")

def _download_cloudflared():
    """Download cloudflared binary for Windows if not present."""
    if os.path.exists(CLOUDFLARED_EXE):
        return True
    os.makedirs(CLOUDFLARED_DIR, exist_ok=True)
    url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
    print("[TUNNEL] Downloading cloudflared (one-time, ~25 MB)...")
    try:
        urllib.request.urlretrieve(url, CLOUDFLARED_EXE)
        print("[TUNNEL] Download complete.")
        return True
    except Exception as exc:
        print(f"[TUNNEL] Download failed: {exc}")
        return False

def _start_tunnel(port: int) -> str | None:
    """Start a Cloudflare Quick Tunnel and return the public URL."""
    if not _download_cloudflared():
        return None
    try:
        proc = subprocess.Popen(
            [CLOUDFLARED_EXE, "tunnel", "--url", f"http://localhost:{port}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # Cloudflare prints the URL to stderr
        # Wait up to 15 seconds for the URL
        import re as _re
        deadline = _time.time() + 20
        url = None
        while _time.time() < deadline:
            line = proc.stderr.readline()
            if not line:
                _time.sleep(0.2)
                continue
            # Look for the trycloudflare.com URL
            match = _re.search(r'(https://[a-zA-Z0-9\-]+\.trycloudflare\.com)', line)
            if match:
                url = match.group(1)
                break
        return url
    except Exception as exc:
        print(f"[TUNNEL] Failed to start: {exc}")
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    local_ip = get_local_ip()
    port = 8000

    print("\n" + "=" * 60)
    print("  🚀 HamLerc Mobile Navigation Server")
    print("=" * 60)

    # Start tunnel in background
    print("\n  ⏳ Creating public tunnel (no signup needed)...")
    tunnel_url = _start_tunnel(port)

    if tunnel_url:
        print(f"\n  📱 Open this URL on your phone:")
        print(f"     {tunnel_url}")
        print(f"\n     ✅ Works from ANY network — no WiFi/firewall setup needed!")
    else:
        print(f"\n  ⚠️  Tunnel failed. Use local network instead:")
        print(f"     http://{local_ip}:{port}")
        print(f"     (Both devices must be on same WiFi/hotspot)")
        print(f"     Run open_firewall.bat as Admin if phone can't connect")

    print(f"\n  💻 Local access:")
    print(f"     http://localhost:{port}")
    print("=" * 60 + "\n")

    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
