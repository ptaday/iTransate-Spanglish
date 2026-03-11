// Token route — generates short-lived AssemblyAI V3 tokens for authenticated devices.
//
// POST /api/token
//   Body (optional):
//     expires_in_seconds          — how long the token is valid (1–600s, default from config)
//     max_session_duration_seconds — how long the WebSocket session can stay open (60–10800s)
//   Response:
//     token          — the AssemblyAI temporary token (embed in WebSocket URL as ?token=...)
//     websocket_url  — the V3 WebSocket endpoint to connect to
//     device_id      — echoed back for confirmation
//
// The AssemblyAI V3 token endpoint is a GET request (not POST) that returns
// { token: string }. The token is then passed back to the device, which embeds
// it in the WebSocket URL query string. The master API key stays server-side.

import { Router, Response } from 'express';
import { config } from '../config';
import { AuthenticatedRequest } from '../middleware/auth';

const router = Router();

// AssemblyAI V3 endpoints
const ASSEMBLYAI_TOKEN_URL = 'https://streaming.assemblyai.com/v3/token';
const ASSEMBLYAI_WS_URL = 'wss://streaming.assemblyai.com/v3/ws';

interface AssemblyAITokenResponse {
  token: string;
}

interface AssemblyAIErrorResponse {
  error: string;
}

interface TokenRequestBody {
  expires_in_seconds?: number;
  max_session_duration_seconds?: number;
}

async function generateAssemblyAIToken(
  expiresInSeconds: number,
  maxSessionDurationSeconds?: number
): Promise<string> {
  // Build query parameters for V3 GET request
  const params = new URLSearchParams({
    expires_in_seconds: expiresInSeconds.toString(),
  });

  if (maxSessionDurationSeconds) {
    params.append('max_session_duration_seconds', maxSessionDurationSeconds.toString());
  }

  const response = await fetch(`${ASSEMBLYAI_TOKEN_URL}?${params.toString()}`, {
    method: 'GET',
    headers: {
      'Authorization': config.assemblyAiApiKey,
    },
  });

  if (!response.ok) {
    const errorData = (await response.json()) as AssemblyAIErrorResponse;
    throw new Error(`AssemblyAI token generation failed: ${errorData.error || response.statusText}`);
  }

  const data = (await response.json()) as AssemblyAITokenResponse;
  return data.token;
}

router.post('/token', async (req: AuthenticatedRequest, res: Response): Promise<void> => {
  const deviceId = req.deviceId;
  const body = req.body as TokenRequestBody;

  // V3 uses expires_in_seconds (1-600 seconds)
  const expiresInSeconds = body.expires_in_seconds || config.tokenExpiresIn;
  const clampedExpiresIn = Math.min(Math.max(expiresInSeconds, 1), 600);

  // V3 optional: max_session_duration_seconds (60-10800 seconds).
  // This controls how long the streaming session can run AFTER the WebSocket is established —
  // independent of the token's expires_in_seconds (which only gates the initial connection).
  // Default to 3600s (1 hour) so long sessions are not silently capped by AssemblyAI's own default.
  const maxSessionDuration = body.max_session_duration_seconds
    ? Math.min(Math.max(body.max_session_duration_seconds, 60), 10800)
    : 3600;

  console.log(`[Token] Generating V3 token for device: ${deviceId}, expires_in: ${clampedExpiresIn}s`);

  try {
    const token = await generateAssemblyAIToken(clampedExpiresIn, maxSessionDuration);

    console.log(`[Token] V3 token generated successfully for device: ${deviceId}`);

    res.json({
      token,
      expires_in_seconds: clampedExpiresIn,
      max_session_duration_seconds: maxSessionDuration,
      device_id: deviceId,
      websocket_url: ASSEMBLYAI_WS_URL,
    });
  } catch (error) {
    console.error(`[Token] Failed to generate token for device ${deviceId}:`, error);

    res.status(502).json({
      error: 'Token Generation Failed',
      message: error instanceof Error ? error.message : 'Unknown error',
    });
  }
});

router.get('/health', (_req, res) => {
  res.json({
    status: 'healthy',
    service: 'itranslate-token-service',
    timestamp: new Date().toISOString(),
  });
});

export default router;
