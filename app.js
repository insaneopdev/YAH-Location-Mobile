/**
 * HamLerc Mobile Navigation — app.js
 * ====================================
 * Core application logic for the mobile PWA.
 * Handles: audio recording, server communication, map rendering,
 *          navigation state machine, and GPS tracking.
 */

// ── API base URL ──────────────────────────────────────────────────────────
// If hosted on GitHub Pages, user must set their PC's local IP.
// If served from the FastAPI server itself, same-origin works automatically.
function getApiBase() {
    // If served from the backend directly (port 8000), use same origin
    if (window.location.port === '8000') {
        return window.location.origin;
    }
    // Otherwise check localStorage for saved backend IP
    const saved = localStorage.getItem('hamlerc_backend');
    if (saved) return saved;
    return null;
}

let API = getApiBase();

// ── DOM Elements ──────────────────────────────────────────────────────────
const $map = document.getElementById('map');
const $loadingOverlay = document.getElementById('loading-overlay');
const $loadingStatus = document.getElementById('loading-status');
const $statusBar = document.getElementById('status-bar');
const $dotServer = document.getElementById('dot-server');
const $dotGps = document.getElementById('dot-gps');
const $statusText = document.getElementById('status-text');
const $voiceBtn = document.getElementById('voice-btn');
const $voiceHint = document.getElementById('voice-hint');
const $promptCard = document.getElementById('prompt-card');
const $promptText = document.getElementById('prompt-text');
const $promptSubtext = document.getElementById('prompt-subtext');
const $transcriptBubble = document.getElementById('transcript-bubble');
const $infoPanel = document.getElementById('info-panel');
const $panelTitle = document.getElementById('panel-title');
const $panelSubtitle = document.getElementById('panel-subtitle');
const $panelBody = document.getElementById('panel-body');
const $navSummary = document.getElementById('nav-summary');
const $navEta = document.getElementById('nav-eta');
const $navDist = document.getElementById('nav-dist');
const $navCurrentStep = document.getElementById('nav-current-step');
const $arrivedOverlay = document.getElementById('arrived-overlay');
const $arrivedDest = document.getElementById('arrived-dest');
const $arrivedBtn = document.getElementById('arrived-btn');

// ── State ─────────────────────────────────────────────────────────────────
let map = null;
let routePolyline = null;
let stepMarkers = [];
let userMarker = null;
let userPos = null;
let mediaRecorder = null;
let audioChunks = [];
let isRecording = false;
let currentAudio = null;

// Navigation state
let navState = 'INIT'; // INIT → GET_DEST → CONFIRM_DEST → SELECT_ROUTE → NAVIGATING → ARRIVED
let startLocation = null;  // {label, coord, lat, lng}
let destination = null;    // {label, coord, lat, lng}
let routes = [];
let selectedRoute = null;
let currentStepIdx = 0;
const STEP_TRIGGER_M = 25;
const ARRIVAL_M = 20;

// ── Leaflet Map Init ──────────────────────────────────────────────────────
function initMap() {
    map = L.map('map', {
        zoomControl: true,
        attributionControl: false,
    }).setView([20.5937, 78.9629], 5); // Default: center of India

    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 19,
    }).addTo(map);

    // Move zoom control to top right
    map.zoomControl.setPosition('topright');
}

// ── Leaflet Helpers ───────────────────────────────────────────────────────
function makeIcon(color, label) {
    return L.divIcon({
        className: '',
        html: `<div style="
            background:${color};
            width:22px;height:22px;border-radius:50%;
            border:2px solid #fff;
            display:flex;align-items:center;justify-content:center;
            font-size:9px;font-weight:700;color:#fff;
            box-shadow:0 1px 4px #0006;
            font-family:Inter,sans-serif;
        ">${label}</div>`,
        iconSize: [22, 22],
        iconAnchor: [11, 11],
        popupAnchor: [0, -14],
    });
}

function clearRoute() {
    if (routePolyline) { map.removeLayer(routePolyline); routePolyline = null; }
    stepMarkers.forEach(m => map.removeLayer(m));
    stepMarkers = [];
}

function drawRoute(route, startLabel, destLabel) {
    clearRoute();

    const coords = route.polyline_coords.map(c => [c[0], c[1]]);
    if (coords.length === 0) return;

    // Shadow line
    L.polyline(coords, {
        color: '#1e1b4b', weight: 9, opacity: 0.35,
        lineJoin: 'round', lineCap: 'round',
    }).addTo(map);

    // Main route line
    routePolyline = L.polyline(coords, {
        color: '#6366f1', weight: 5, opacity: 0.9,
        lineJoin: 'round', lineCap: 'round',
    }).addTo(map);

    // Start marker
    const s0 = route.steps[0];
    const startM = L.marker([s0.start_lat, s0.start_lng], {
        icon: makeIcon('#22c55e', 'S')
    }).addTo(map).bindPopup(`<b>START</b><br/>${startLabel}`);
    stepMarkers.push(startM);

    // Step markers
    route.steps.forEach((step, i) => {
        const m = L.marker([step.start_lat, step.start_lng], {
            icon: makeIcon('#3b82f6', `${i + 1}`)
        }).addTo(map).bindPopup(
            `<b>Step ${i + 1}/${route.steps.length}</b><br/>` +
            `${step.instruction}<br/>` +
            `<small>${step.distance} · ~${step.duration}</small>`
        );
        stepMarkers.push(m);
    });

    // Destination marker
    const sl = route.steps[route.steps.length - 1];
    const destM = L.marker([sl.end_lat, sl.end_lng], {
        icon: makeIcon('#ef4444', 'D')
    }).addTo(map).bindPopup(`<b>DESTINATION</b><br/>${destLabel}`);
    stepMarkers.push(destM);

    // Fit bounds
    map.fitBounds(routePolyline.getBounds(), { padding: [60, 60] });
}

function updateUserMarker(lat, lng) {
    if (!userMarker) {
        userMarker = L.marker([lat, lng], {
            icon: L.divIcon({
                className: '',
                html: '<div class="user-marker"></div>',
                iconSize: [18, 18],
                iconAnchor: [9, 9],
            })
        }).addTo(map);
    } else {
        userMarker.setLatLng([lat, lng]);
    }
}

// ── Audio Recording ───────────────────────────────────────────────────────
async function startRecording() {
    try {
        const stream = await navigator.mediaDevices.getUserMedia({
            audio: {
                sampleRate: 16000,
                channelCount: 1,
                echoCancellation: true,
                noiseSuppression: true,
            }
        });

        mediaRecorder = new MediaRecorder(stream, {
            mimeType: MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
                ? 'audio/webm;codecs=opus'
                : 'audio/webm'
        });

        audioChunks = [];
        mediaRecorder.ondataavailable = (e) => {
            if (e.data.size > 0) audioChunks.push(e.data);
        };

        mediaRecorder.onstop = async () => {
            stream.getTracks().forEach(t => t.stop());
            const blob = new Blob(audioChunks, { type: 'audio/webm' });

            // Convert webm to wav using AudioContext
            const wavBlob = await webmToWav(blob);
            if (wavBlob) {
                await handleRecordedAudio(wavBlob);
            }
        };

        mediaRecorder.start();
        isRecording = true;
        $voiceBtn.classList.add('recording');
        $voiceBtn.innerHTML = '⏹';
        $voiceHint.textContent = 'Tap to stop';
    } catch (err) {
        console.error('Mic error:', err);
        showPrompt('Microphone access denied', 'Please allow microphone access in browser settings');
    }
}

function stopRecording() {
    if (mediaRecorder && mediaRecorder.state === 'recording') {
        mediaRecorder.stop();
        isRecording = false;
        $voiceBtn.classList.remove('recording');
        $voiceBtn.classList.add('processing');
        $voiceBtn.innerHTML = '⏳';
        $voiceHint.textContent = 'Processing...';
    }
}

// Convert WebM audio to WAV (16kHz mono) for Whisper compatibility
async function webmToWav(webmBlob) {
    try {
        const audioCtx = new (window.AudioContext || window.webkitAudioContext)({
            sampleRate: 16000
        });
        const arrayBuffer = await webmBlob.arrayBuffer();
        const audioBuffer = await audioCtx.decodeAudioData(arrayBuffer);

        // Get mono channel data
        const channelData = audioBuffer.getChannelData(0);
        const sampleRate = audioBuffer.sampleRate;

        // Resample to 16kHz if needed
        let samples = channelData;
        if (sampleRate !== 16000) {
            const ratio = 16000 / sampleRate;
            const newLength = Math.round(channelData.length * ratio);
            samples = new Float32Array(newLength);
            for (let i = 0; i < newLength; i++) {
                const srcIdx = i / ratio;
                const low = Math.floor(srcIdx);
                const high = Math.min(low + 1, channelData.length - 1);
                const frac = srcIdx - low;
                samples[i] = channelData[low] * (1 - frac) + channelData[high] * frac;
            }
        }

        // Create WAV
        const wavBuffer = new ArrayBuffer(44 + samples.length * 2);
        const view = new DataView(wavBuffer);

        // WAV header
        writeString(view, 0, 'RIFF');
        view.setUint32(4, 36 + samples.length * 2, true);
        writeString(view, 8, 'WAVE');
        writeString(view, 12, 'fmt ');
        view.setUint32(16, 16, true); // chunk size
        view.setUint16(20, 1, true);  // PCM
        view.setUint16(22, 1, true);  // mono
        view.setUint32(24, 16000, true); // sample rate
        view.setUint32(28, 32000, true); // byte rate
        view.setUint16(32, 2, true);  // block align
        view.setUint16(34, 16, true); // bits per sample
        writeString(view, 36, 'data');
        view.setUint32(40, samples.length * 2, true);

        // Write samples as int16
        let offset = 44;
        for (let i = 0; i < samples.length; i++) {
            const s = Math.max(-1, Math.min(1, samples[i]));
            view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
            offset += 2;
        }

        await audioCtx.close();
        return new Blob([wavBuffer], { type: 'audio/wav' });
    } catch (err) {
        console.error('WebM→WAV conversion error:', err);
        return null;
    }
}

function writeString(view, offset, str) {
    for (let i = 0; i < str.length; i++) {
        view.setUint8(offset + i, str.charCodeAt(i));
    }
}

// ── Audio Playback (TTS) ──────────────────────────────────────────────────
async function playTTS(text) {
    try {
        const res = await fetch(`${API}/api/tts`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text }),
        });

        if (!res.ok) throw new Error('TTS failed');

        const blob = await res.blob();
        const url = URL.createObjectURL(blob);

        return new Promise((resolve) => {
            if (currentAudio) {
                currentAudio.pause();
                currentAudio = null;
            }
            currentAudio = new Audio(url);
            currentAudio.onended = () => {
                URL.revokeObjectURL(url);
                currentAudio = null;
                resolve();
            };
            currentAudio.onerror = () => {
                URL.revokeObjectURL(url);
                currentAudio = null;
                resolve();
            };
            currentAudio.play().catch(() => resolve());
        });
    } catch (err) {
        console.error('TTS playback error:', err);
    }
}

function stopTTS() {
    if (currentAudio) {
        currentAudio.pause();
        currentAudio.currentTime = 0;
        currentAudio = null;
    }
}

// ── Server Communication ──────────────────────────────────────────────────
async function sendAudioForSTT(wavBlob) {
    const formData = new FormData();
    formData.append('audio', wavBlob, 'recording.wav');

    try {
        const res = await fetch(`${API}/api/stt`, {
            method: 'POST',
            body: formData,
        });
        const data = await res.json();
        return data.text;
    } catch (err) {
        console.error('STT error:', err);
        return null;
    }
}

async function apiSearchPlaces(query) {
    const res = await fetch(`${API}/api/search-places`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query }),
    });
    const data = await res.json();
    return data.places || [];
}

async function apiGeocode(query) {
    const res = await fetch(`${API}/api/geocode`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query }),
    });
    const data = await res.json();
    return data.result;
}

async function apiReverseGeocode(lat, lng) {
    const res = await fetch(`${API}/api/reverse-geocode`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lat, lng }),
    });
    const data = await res.json();
    return data.address;
}

async function apiFetchRoutes(start, dest, maxAlt = 3) {
    const res = await fetch(`${API}/api/fetch-routes`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ start, destination: dest, max_alternatives: maxAlt }),
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    return data.routes || [];
}

// ── GPS ───────────────────────────────────────────────────────────────────
let gpsWatchId = null;

function startGPS() {
    if (!navigator.geolocation) {
        $dotGps.className = 'status-dot disconnected';
        return;
    }

    gpsWatchId = navigator.geolocation.watchPosition(
        (pos) => {
            userPos = {
                lat: pos.coords.latitude,
                lng: pos.coords.longitude,
                accuracy: pos.coords.accuracy,
            };
            $dotGps.className = 'status-dot gps active';
            updateUserMarker(userPos.lat, userPos.lng);

            // During navigation, check step proximity
            if (navState === 'NAVIGATING') {
                checkNavProgress();
            }
        },
        (err) => {
            console.warn('GPS error:', err);
            $dotGps.className = 'status-dot disconnected';
        },
        {
            enableHighAccuracy: true,
            maximumAge: 2000,
            timeout: 10000,
        }
    );
}

function stopGPS() {
    if (gpsWatchId !== null) {
        navigator.geolocation.clearWatch(gpsWatchId);
        gpsWatchId = null;
    }
}

// ── Haversine (client-side) ───────────────────────────────────────────────
function haversine(lat1, lng1, lat2, lng2) {
    const R = 6371000;
    const p1 = lat1 * Math.PI / 180;
    const p2 = lat2 * Math.PI / 180;
    const dp = (lat2 - lat1) * Math.PI / 180;
    const dl = (lng2 - lng1) * Math.PI / 180;
    const a = Math.sin(dp / 2) ** 2 +
        Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
    return R * 2 * Math.asin(Math.sqrt(a));
}

// ── UI Helpers ────────────────────────────────────────────────────────────
function showPrompt(text, subtext) {
    $promptText.textContent = text;
    $promptSubtext.textContent = subtext || '';
    $promptCard.classList.add('visible');
}

function hidePrompt() {
    $promptCard.classList.remove('visible');
}

function showTranscript(text) {
    $transcriptBubble.textContent = `🎤 "${text}"`;
    $transcriptBubble.classList.add('visible');
    setTimeout(() => $transcriptBubble.classList.remove('visible'), 4000);
}

function showPanel(title, subtitle, bodyHTML) {
    $panelTitle.innerHTML = title;
    $panelSubtitle.textContent = subtitle || '';
    $panelBody.innerHTML = bodyHTML;
    $infoPanel.classList.add('visible');
}

function hidePanel() {
    $infoPanel.classList.remove('visible');
}

function showNavSummary(eta, dist, stepText) {
    $navEta.textContent = eta;
    $navDist.textContent = dist;
    $navCurrentStep.textContent = stepText;
    $navSummary.classList.add('visible');
    $statusBar.style.display = 'none';
}

function hideNavSummary() {
    $navSummary.classList.remove('visible');
    $statusBar.style.display = '';
}

function resetVoiceBtn() {
    $voiceBtn.classList.remove('recording', 'processing');
    $voiceBtn.innerHTML = '🎤';
    $voiceHint.textContent = 'Tap to speak';
}

// ── Server Health Check ───────────────────────────────────────────────────
async function checkServer() {
    try {
        const res = await fetch(`${API}/api/reverse-geocode`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ lat: 0, lng: 0 }),
        });
        // If we get any response, server is up
        $dotServer.className = 'status-dot connected active';
        $statusText.textContent = 'Connected';
        return true;
    } catch {
        $dotServer.className = 'status-dot disconnected';
        $statusText.textContent = 'Offline';
        return false;
    }
}

// ── Navigation State Machine ──────────────────────────────────────────────

async function handleRecordedAudio(wavBlob) {
    // Send to server for STT
    const text = await sendAudioForSTT(wavBlob);
    resetVoiceBtn();

    if (!text) {
        showPrompt("I didn't catch that", 'Try again — tap the mic and speak clearly');
        await playTTS("I didn't catch that. Try again.");
        return;
    }

    showTranscript(text);

    // Route based on current state
    switch (navState) {
        case 'GET_DEST':
            await handleDestinationInput(text);
            break;
        case 'CONFIRM_DEST':
            await handleDestConfirmation(text);
            break;
        case 'SELECT_ROUTE':
            await handleRouteSelection(text);
            break;
        case 'NAVIGATING':
            // During navigation, voice can be used for queries
            await handleNavVoice(text);
            break;
        default:
            await handleDestinationInput(text);
            break;
    }
}

// ── Step 1: Get phone GPS → reverse geocode ───────────────────────────────
async function initNavigation() {
    navState = 'INIT';
    $loadingStatus.textContent = 'Getting your location...';

    // Wait for GPS
    await new Promise((resolve) => {
        if (userPos) return resolve();
        const check = setInterval(() => {
            if (userPos) { clearInterval(check); resolve(); }
        }, 500);
        // Timeout after 15s
        setTimeout(() => { clearInterval(check); resolve(); }, 15000);
    });

    if (userPos) {
        $loadingStatus.textContent = 'Identifying location...';
        try {
            const address = await apiReverseGeocode(userPos.lat, userPos.lng);
            startLocation = {
                label: address,
                coord: `${userPos.lat},${userPos.lng}`,
                lat: userPos.lat,
                lng: userPos.lng,
            };
            map.setView([userPos.lat, userPos.lng], 16);
        } catch {
            startLocation = {
                label: 'Current Location',
                coord: `${userPos.lat},${userPos.lng}`,
                lat: userPos.lat,
                lng: userPos.lng,
            };
        }
    } else {
        // No GPS — use a default
        startLocation = { label: 'Unknown', coord: '0,0', lat: 0, lng: 0 };
    }

    // Hide loading, show UI
    $loadingOverlay.classList.add('hidden');

    // Announce start
    navState = 'GET_DEST';
    const greeting = startLocation.label !== 'Unknown'
        ? `Starting from ${startLocation.label}. Where would you like to go?`
        : 'Where would you like to go?';

    showPrompt('Where would you like to go?', `📍 Starting from: ${startLocation.label}`);
    await playTTS(greeting);
}

// ── Step 2: Handle destination input ──────────────────────────────────────
async function handleDestinationInput(text) {
    showPrompt('Searching...', `Looking for "${text}"`);
    await playTTS(`Searching for ${text}`);

    try {
        const places = await apiSearchPlaces(text);

        if (places.length === 0) {
            // Try geocode fallback
            const geo = await apiGeocode(text);
            if (geo) {
                destination = {
                    label: geo.address,
                    coord: `${geo.lat},${geo.lng}`,
                    lat: geo.lat,
                    lng: geo.lng,
                };
                navState = 'CONFIRM_DEST';
                showPrompt(`Found: ${geo.address}`, 'Say "yes" to confirm or say a different place');
                await playTTS(`Found ${geo.address}. Is this correct?`);
                return;
            }

            showPrompt('No results found', 'Try saying the destination more specifically');
            await playTTS('No results found. Try again with a more specific name.');
            navState = 'GET_DEST';
            return;
        }

        if (places.length === 1) {
            // Single result
            destination = {
                label: places[0].name,
                coord: `${places[0].lat},${places[0].lng}`,
                lat: places[0].lat,
                lng: places[0].lng,
            };
            navState = 'CONFIRM_DEST';
            showPrompt(`Found: ${places[0].name}`, 'Say "yes" to confirm');
            await playTTS(`Found ${places[0].name}. Is this correct?`);
        } else {
            // Multiple results — show options
            destination = null;
            navState = 'CONFIRM_DEST';

            let html = '';
            places.forEach((p, i) => {
                html += `
                    <button class="option-btn" onclick="selectPlace(${i})">
                        <div class="option-name">${i + 1}. ${p.name}</div>
                        <div class="option-address">${p.address || ''}</div>
                    </button>`;
            });

            showPanel('🔍 Select Destination', `${places.length} places found`, html);

            // Store places for selection
            window._pendingPlaces = places;

            let announcement = `${places.length} places found. `;
            places.forEach((p, i) => {
                announcement += `Option ${i + 1}: ${p.name}. `;
            });
            announcement += `Say the number to select.`;

            showPrompt('Select a destination', 'Tap an option or say the number');
            await playTTS(announcement);
        }
    } catch (err) {
        console.error('Place search error:', err);
        showPrompt('Search failed', 'Please try again');
        await playTTS('Search failed. Try again.');
        navState = 'GET_DEST';
    }
}

// Place selection from UI tap
window.selectPlace = function (idx) {
    const places = window._pendingPlaces;
    if (!places || !places[idx]) return;

    const p = places[idx];
    destination = {
        label: p.name,
        coord: `${p.lat},${p.lng}`,
        lat: p.lat,
        lng: p.lng,
    };

    hidePanel();
    confirmAndFetchRoutes();
};

// ── Step 3: Handle destination confirmation ───────────────────────────────
async function handleDestConfirmation(text) {
    const normalized = text.toLowerCase().trim();

    // Check for yes/confirm
    if (['yes', 'y', 'yeah', 'yep', 'sure', 'correct', 'confirm', 'ok', 'okay'].some(w => normalized.includes(w))) {
        if (window._pendingPlaces && !destination) {
            // They said yes but haven't selected from multiple — pick first
            const p = window._pendingPlaces[0];
            destination = {
                label: p.name,
                coord: `${p.lat},${p.lng}`,
                lat: p.lat,
                lng: p.lng,
            };
        }

        if (destination) {
            hidePanel();
            await confirmAndFetchRoutes();
        }
        return;
    }

    // Check for number selection from multiple places
    const num = parseInt(normalized.replace(/[^\d]/g, ''));
    if (window._pendingPlaces && num >= 1 && num <= window._pendingPlaces.length) {
        const p = window._pendingPlaces[num - 1];
        destination = {
            label: p.name,
            coord: `${p.lat},${p.lng}`,
            lat: p.lat,
            lng: p.lng,
        };
        hidePanel();
        await confirmAndFetchRoutes();
        return;
    }

    // Check for no/cancel — restart destination input
    if (['no', 'n', 'nope', 'cancel', 'wrong', 'different'].some(w => normalized.includes(w))) {
        hidePanel();
        navState = 'GET_DEST';
        showPrompt('Where would you like to go?', 'Say your destination');
        await playTTS('Where would you like to go?');
        return;
    }

    // Treat as new destination query
    hidePanel();
    navState = 'GET_DEST';
    await handleDestinationInput(text);
}

// ── Step 4: Fetch & display routes ────────────────────────────────────────
async function confirmAndFetchRoutes() {
    showPrompt('Fetching routes...', `${startLocation.label} → ${destination.label}`);
    await playTTS(`Getting routes from ${startLocation.label} to ${destination.label}`);

    try {
        routes = await apiFetchRoutes(startLocation.coord, destination.coord, 3);

        if (routes.length === 0) {
            showPrompt('No routes found', 'Try a different destination');
            await playTTS('No routes found. Try a different destination.');
            navState = 'GET_DEST';
            return;
        }

        // Draw first route on map
        drawRoute(routes[0], startLocation.label, destination.label);

        if (routes.length === 1) {
            selectedRoute = routes[0];
            navState = 'SELECT_ROUTE';

            showPrompt(
                `Route: ${routes[0].total_distance}, ~${routes[0].total_duration}`,
                'Say "go" to start navigation'
            );
            await playTTS(
                `Route found. ${routes[0].total_distance}, approximately ${routes[0].total_duration}. ` +
                `Say go to start navigation.`
            );
        } else {
            // Multiple routes
            navState = 'SELECT_ROUTE';
            let html = '';
            routes.forEach((r, i) => {
                html += `
                    <div class="route-card ${i === 0 ? 'selected' : ''}" onclick="selectRoute(${i})">
                        <div class="route-card-header">
                            <div class="route-card-title">Route ${i + 1}</div>
                            ${i === 0 ? '<div class="route-card-badge">Fastest</div>' : ''}
                        </div>
                        <div class="route-card-meta">
                            <span>📏 ${r.total_distance}</span>
                            <span>⏱ ~${r.total_duration}</span>
                            <span>👣 ${r.steps.length} steps</span>
                        </div>
                    </div>`;
            });

            showPanel('🗺️ Choose a Route', `${routes.length} routes available`, html);

            let announcement = `${routes.length} routes found. `;
            routes.forEach((r, i) => {
                announcement += `Route ${i + 1}: ${r.total_distance}, ${r.total_duration}. `;
            });
            announcement += 'Say the route number or tap to select.';

            showPrompt('Select a route', 'Tap a route or say its number');
            await playTTS(announcement);
        }
    } catch (err) {
        console.error('Route fetch error:', err);
        showPrompt('Route error', err.message);
        await playTTS(`Error getting routes: ${err.message}`);
        navState = 'GET_DEST';
    }
}

// Route selection from UI tap
window.selectRoute = function (idx) {
    if (!routes[idx]) return;
    selectedRoute = routes[idx];
    drawRoute(selectedRoute, startLocation.label, destination.label);
    hidePanel();
    startNavigation();
};

// ── Step 5: Handle route selection by voice ───────────────────────────────
async function handleRouteSelection(text) {
    const normalized = text.toLowerCase().trim();

    // "go" / "start" / "navigate" — use first/selected route
    if (['go', 'start', 'navigate', 'begin', 'yes', 'okay'].some(w => normalized.includes(w))) {
        if (!selectedRoute) selectedRoute = routes[0];
        hidePanel();
        await startNavigation();
        return;
    }

    // Number selection
    const num = parseInt(normalized.replace(/[^\d]/g, ''));
    if (num >= 1 && num <= routes.length) {
        selectedRoute = routes[num - 1];
        drawRoute(selectedRoute, startLocation.label, destination.label);
        hidePanel();
        await startNavigation();
        return;
    }

    showPrompt('Select a route', 'Say "go" or the route number');
    await playTTS('Say go to start, or say a route number.');
}

// ── Step 6: Start navigation ──────────────────────────────────────────────
async function startNavigation() {
    navState = 'NAVIGATING';
    currentStepIdx = 0;
    hidePrompt();
    hidePanel();

    const route = selectedRoute;
    const steps = route.steps;

    // Show navigation summary bar
    showNavSummary(
        `~${route.total_duration}`,
        route.total_distance,
        steps[0].instruction
    );

    // Show steps panel
    renderStepsList(steps, 0);

    // Announce start
    await playTTS(`Navigation started. ${route.total_distance}, approximately ${route.total_duration}.`);

    // Announce first step
    if (steps[0].speech) {
        await playTTS(steps[0].speech);
    } else {
        await playTTS(steps[0].instruction);
    }

    $voiceHint.textContent = 'Navigation active';
}

function renderStepsList(steps, activeIdx) {
    let html = '';
    steps.forEach((step, i) => {
        const cls = i < activeIdx ? 'completed' : (i === activeIdx ? 'active' : '');
        html += `
            <div class="step-card ${cls}">
                <div class="step-header">
                    <div class="step-number">${i + 1}</div>
                    <div class="step-body">
                        <div class="step-instruction">${step.instruction}</div>
                        <div class="step-meta">${step.distance} · ~${step.duration}</div>
                    </div>
                </div>
            </div>`;
    });

    showPanel(
        `🧭 Navigation`,
        `Step ${activeIdx + 1} of ${steps.length}`,
        html
    );
}

// ── Step 7: Check navigation progress (called on GPS update) ──────────────
function checkNavProgress() {
    if (!selectedRoute || !userPos) return;

    const steps = selectedRoute.steps;

    if (currentStepIdx < steps.length) {
        const tgt = steps[currentStepIdx];
        const startDist = haversine(userPos.lat, userPos.lng, tgt.start_lat, tgt.start_lng);

        // Check if user has reached the next step
        if (currentStepIdx > 0 && startDist <= STEP_TRIGGER_M) {
            // Announce this step
            const speech = tgt.speech || tgt.instruction;
            playTTS(speech);

            currentStepIdx++;
            renderStepsList(steps, currentStepIdx);

            // Update summary
            if (currentStepIdx < steps.length) {
                $navCurrentStep.textContent = steps[currentStepIdx].instruction;
            }
        }

        // Also check proximity to next step if we haven't moved to it yet
        if (currentStepIdx + 1 < steps.length) {
            const next = steps[currentStepIdx + 1];
            const nextDist = haversine(userPos.lat, userPos.lng, next.start_lat, next.start_lng);
            if (nextDist <= STEP_TRIGGER_M) {
                currentStepIdx++;
                const speech = next.speech || next.instruction;
                playTTS(speech);
                renderStepsList(steps, currentStepIdx);

                if (currentStepIdx < steps.length) {
                    $navCurrentStep.textContent = steps[currentStepIdx].instruction;
                }
            }
        }
    }

    // Check arrival at final destination
    if (currentStepIdx >= steps.length - 1) {
        const last = steps[steps.length - 1];
        const dist = haversine(userPos.lat, userPos.lng, last.end_lat, last.end_lng);
        if (dist <= ARRIVAL_M) {
            handleArrival();
        }
    }
}

// ── Handle voice during navigation ────────────────────────────────────────
async function handleNavVoice(text) {
    const normalized = text.toLowerCase().trim();

    if (['repeat', 'again', 'what'].some(w => normalized.includes(w))) {
        // Repeat current step
        const step = selectedRoute.steps[currentStepIdx];
        if (step) {
            await playTTS(step.speech || step.instruction);
        }
        return;
    }

    if (['stop', 'cancel', 'exit', 'quit'].some(w => normalized.includes(w))) {
        // Stop navigation
        navState = 'GET_DEST';
        hideNavSummary();
        hidePanel();
        clearRoute();
        showPrompt('Navigation cancelled', 'Say a new destination');
        await playTTS('Navigation cancelled. Where would you like to go?');
        return;
    }

    // Default: acknowledge
    await playTTS("Navigation is active. Say repeat to hear the current step, or stop to cancel.");
}

// ── Arrival ───────────────────────────────────────────────────────────────
async function handleArrival() {
    navState = 'ARRIVED';
    hideNavSummary();
    hidePanel();

    $arrivedDest.textContent = destination.label;
    $arrivedOverlay.classList.add('visible');

    await playTTS(`You have arrived at ${destination.label}. Navigation complete.`);
}

// ── Voice Button Handler ──────────────────────────────────────────────────
$voiceBtn.addEventListener('click', () => {
    stopTTS(); // Stop any playing TTS

    if (isRecording) {
        stopRecording();
    } else {
        startRecording();
    }
});

// ── Arrived Button Handler ────────────────────────────────────────────────
$arrivedBtn.addEventListener('click', () => {
    $arrivedOverlay.classList.remove('visible');
    clearRoute();
    navState = 'GET_DEST';
    showPrompt('Where would you like to go?', `📍 From: ${startLocation.label}`);
    playTTS('Where would you like to go?');
});

// ── Initialization ────────────────────────────────────────────────────────

// Called from the connect overlay when hosted on GitHub Pages
window.connectToServer = async function () {
    const ipInput = document.getElementById('server-ip');
    const errorDiv = document.getElementById('connect-error');
    const btn = document.getElementById('connect-btn');

    const ip = ipInput.value.trim();
    if (!ip) {
        errorDiv.textContent = 'Please enter an IP address';
        errorDiv.style.display = 'block';
        return;
    }

    btn.textContent = 'Connecting...';
    btn.disabled = true;
    errorDiv.style.display = 'none';

    const testUrl = `http://${ip}:8000`;
    try {
        const res = await fetch(`${testUrl}/api/reverse-geocode`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ lat: 0, lng: 0 }),
            signal: AbortSignal.timeout(5000),
        });
        if (!res.ok) throw new Error('Bad response');

        // Save and proceed
        API = testUrl;
        localStorage.setItem('hamlerc_backend', testUrl);
        document.getElementById('connect-overlay').style.display = 'none';
        await startApp();
    } catch (err) {
        errorDiv.textContent = `Cannot reach ${testUrl}. Is the server running?`;
        errorDiv.style.display = 'block';
        btn.textContent = 'Connect';
        btn.disabled = false;
    }
};

async function init() {
    // If no API URL (hosted externally without saved IP), show connect overlay
    if (!API) {
        document.getElementById('loading-overlay').classList.add('hidden');
        document.getElementById('connect-overlay').style.display = 'block';
        initMap(); // init map in background
        return;
    }

    await startApp();
}

async function startApp() {
    $loadingStatus.textContent = 'Initializing map...';
    if (!map) initMap();

    $loadingStatus.textContent = 'Checking server connection...';
    const serverOk = await checkServer();
    if (!serverOk) {
        // If we have a saved IP but it's not reachable, show connect overlay
        if (window.location.port !== '8000') {
            localStorage.removeItem('hamlerc_backend');
            $loadingOverlay.classList.add('hidden');
            document.getElementById('connect-overlay').style.display = 'block';
            return;
        }
        $loadingStatus.textContent = 'Cannot reach server. Check WiFi connection.';
        const retry = setInterval(async () => {
            const ok = await checkServer();
            if (ok) {
                clearInterval(retry);
                await continueInit();
            }
        }, 3000);
        return;
    }

    await continueInit();
}

async function continueInit() {
    $loadingStatus.textContent = 'Getting GPS...';
    startGPS();
    await initNavigation();
}

// Start
init();

