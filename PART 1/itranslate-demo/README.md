# iTranslate Demo

A demo showcasing how an iTranslate handheld translation device integrates with AssemblyAI's **Universal-3 Pro Streaming API**.

## Overview

This demo simulates a cloud-connected translation device that:
- Captures speech in real-time from a microphone
- Streams audio to AssemblyAI for transcription using the **V3 API** (`u3-rt-pro` model)
- Handles partial and final transcripts with turn detection (`end_of_turn`)
- Supports custom vocabulary boosting via `keyterms_prompt`
- Implements secure device authentication via temporary tokens
- Supports speaker diarization with named speaker mapping

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        User Speech                               │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Device Simulator (Python)                      │
│  ┌──────────────┐  ┌───────────────┐  ┌──────────────────────┐  │
│  │ Audio Capture│─▶│ WebSocket     │─▶│ Transcript Handler   │  │
│  │ (microphone) │  │ Streaming     │  │ + Turn Manager       │  │
│  └──────────────┘  └───────┬───────┘  └──────────────────────┘  │
└────────────────────────────┼────────────────────────────────────┘
                             │
            1. Request Token │ (device_id, api_key)   [token service mode]
                 OR direct   │ Authorization header    [--no-token mode]
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│               AssemblyAI V3 Streaming API                        │
│  ┌──────────────┐  ┌───────────────┐  ┌──────────────────────┐  │
│  │  u3-rt-pro   │─▶│ Turn Detection│─▶│ Keyterms Prompt      │  │
│  │  Model       │  │ (min/max)     │  │ (custom terms)       │  │
│  └──────────────┘  └───────────────┘  └──────────────────────┘  │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            │ Turn Messages (end_of_turn: true/false)
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Output Pipeline                             │
│  ┌──────────────┐  ┌───────────────┐  ┌──────────────────────┐  │
│  │ Real-time    │  │ Turn Complete │  │ Ready for            │  │
│  │ Display      │  │ Detection     │  │ Translation/TTS      │  │
│  └──────────────┘  └───────────────┘  └──────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

## Prerequisites

- **Node.js** >= 18.0.0
- **Python** >= 3.10
- **AssemblyAI API Key** ([Get one here](https://www.assemblyai.com/dashboard/signup))

## Quick Start

### 1. Configure

```bash
cd itranslate-demo
cp .env.example .env
```

Edit `.env` with your credentials:

```bash
# Required
ASSEMBLYAI_API_KEY=your_assemblyai_api_key_here

# Device authentication (token service mode)
ALLOWED_DEVICE_KEYS=device-key-001,device-key-002
DEVICE_API_KEY=device-key-001
DEVICE_ID=itranslate-device-001
```

### 2. Run — Direct Mode (default)

Connects directly to AssemblyAI using your API key. No extra service needed.

```bash
cd device-simulator
pip install -r requirements.txt
python3 -m src.main
```

### 3. Run — Token Service Mode (production auth pattern)

Start the token service first:

```bash
cd token-service
npm install
npm run dev
```

Then in a new terminal:

```bash
cd device-simulator
python3 -m src.main --token-service
```

Speak into your microphone and watch real-time transcription.

## Device Simulator Usage

### Basic Usage

```bash
# Direct connection (default)
python3 -m src.main

# Via token service
python3 -m src.main --token-service
```

### Speaker Diarization

Automatically detects and labels different speakers. Optionally map labels to real names in order of first appearance.

```bash
# Auto-label speakers (Speaker A, Speaker B, ...)
python3 -m src.main --diarization

# Map speakers to real names
python3 -m src.main --diarization --speakers Alice Bob
```

### Keyterm Boosting

Improves recognition accuracy for specific words and phrases such as product names, proper nouns, and technical terms.

```bash
python3 -m src.main --keyterms "iTranslate" "AssemblyAI" "Pocketalk"
```

### Save Transcript

Saves the full session transcript to a `.txt` file on exit, including timestamps, speaker labels, and word-level detail.

```bash
python3 -m src.main --output ./transcripts/session.txt

# Combined with diarization
python3 -m src.main --diarization --speakers Alice Bob --output ./transcripts/session.txt
```

### Turn Detection Tuning

```bash
# More responsive — finalizes faster
python3 -m src.main --min-turn-silence 50 --max-turn-silence 500

# More patient — waits longer before finalizing
python3 -m src.main --min-turn-silence 200 --max-turn-silence 2000
```

### Reliability Configuration

```bash
python3 -m src.main \
  --max-retries 10 \
  --initial-delay 500 \
  --max-delay 60000 \
  --no-jitter
```


## Token Service API

### Generate Token (V3)

**POST** `/api/token`

```bash
curl -X POST http://localhost:3001/api/token \
  -H "Authorization: Bearer device-key-001" \
  -H "X-Device-ID: itranslate-device-001" \
  -H "Content-Type: application/json" \
  -d '{"expires_in_seconds": 120, "max_session_duration_seconds": 3600}'
```

Response:
```json
{
  "token": "eyJ...",
  "expires_in_seconds": 120,
  "max_session_duration_seconds": 3600,
  "device_id": "itranslate-device-001",
  "websocket_url": "wss://streaming.assemblyai.com/v3/ws"
}
```

### Health Check

**GET** `/health`

```bash
curl http://localhost:3001/health
```

## Project Structure

```
itranslate-demo/
├── token-service/                 # TypeScript Express server
│   ├── src/
│   │   ├── index.ts              # Server entry point
│   │   ├── config.ts             # Environment configuration
│   │   ├── routes/
│   │   │   └── token.ts          # Token generation endpoint
│   │   └── middleware/
│   │       └── auth.ts           # Device authentication
│   ├── package.json
│   └── tsconfig.json
│
├── device-simulator/              # Python device simulator
│   ├── src/
│   │   ├── main.py               # CLI entry point
│   │   ├── config.py             # Pydantic settings
│   │   ├── audio/
│   │   │   └── capture.py        # Microphone audio capture
│   │   ├── streaming/
│   │   │   ├── client.py         # AssemblyAI WebSocket client
│   │   │   ├── reconnect.py      # Reconnection with backoff
│   │   │   └── errors.py         # Error classification
│   │   ├── transcription/
│   │   │   └── handler.py        # Transcript processing + display
│   │   └── prompts/
│   │       └── modes.py          # Prompting configurations
│   ├── requirements.txt
│   └── pyproject.toml
│
├── .env.example                   # Environment template
└── README.md
```

## Key Features

### Two Connection Modes

- **Direct (default)**: Connects to AssemblyAI using the API key in the `Authorization` header. No extra service needed.
- **`--token-service`**: Device requests a short-lived token from the token service, which proxies to AssemblyAI. Master API key is never exposed to the device. Best for production.

### Real-time Streaming

- WebSocket connection to AssemblyAI `u3-rt-pro` model
- Audio streamed as raw PCM binary in 100ms chunks at 16kHz
- Partial transcripts for immediate feedback
- Final transcripts on turn completion

### Turn Detection (V3)

- `min_turn_silence` (default 100ms) — short pause triggers a partial transcript
- `max_turn_silence` (default 1000ms) — silence threshold that forces a final transcript

**V3 Message Types:**
- `Begin` — Session started
- `Turn` — Transcript with `end_of_turn` flag
- `SpeechStarted` — Speech detected
- `Termination` — Session ended

### Speaker Diarization

- Automatically detects voice differences and labels speakers
- `--speakers` flag maps labels to real names in order of first appearance
- Labels reset each session (no cross-session voice memory)

### Keyterm Boosting

`keyterms_prompt` improves recognition of product names, proper nouns, and domain-specific terminology. Pass terms via `--keyterms`. Cannot be combined with a prompt — keyterms only.

### Reliability

- Automatic reconnection with exponential backoff and jitter
- Audio buffering during disconnection
- Configurable retry limits and delays

## Configuration Reference

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `ASSEMBLYAI_API_KEY` | AssemblyAI API key (direct mode + token service) | Required |
| `PORT` | Token service port | `3001` |
| `TOKEN_EXPIRES_IN` | Default token TTL (seconds) | `60` |
| `ALLOWED_DEVICE_KEYS` | Comma-separated allowed device keys | Empty (all allowed) |
| `TOKEN_SERVICE_URL` | Token service URL | `http://localhost:3001` |
| `DEVICE_API_KEY` | Device's API key for token service auth | Required |
| `DEVICE_ID` | Unique device identifier | `device-001` |
| `SAMPLE_RATE` | Audio sample rate | `16000` |

## Troubleshooting

### Token Service Issues

**"Missing required environment variable: ASSEMBLYAI_API_KEY"**
- Ensure `.env` exists and contains a valid API key

**"Invalid device key"**
- Check `ALLOWED_DEVICE_KEYS` includes your device's key
- Verify `Authorization` header format: `Bearer <key>`

### Device Simulator Issues

**"Cannot connect to token service"**
- Use `--no-token` to bypass the token service for testing
- Or verify the token service is running: `curl http://localhost:3001/health`

**No audio / low confidence scores**
- Check microphone permissions in System Settings → Privacy & Security → Microphone

**Connection keeps dropping**
- Check network stability
- Increase `--max-retries` and `--max-delay`

## License

MIT
