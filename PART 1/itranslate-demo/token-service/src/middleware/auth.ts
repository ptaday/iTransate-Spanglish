// Device authentication middleware — validates that each request to /api comes
// from a known device before allowing it through to the token route.
//
// Required headers:
//   Authorization: Bearer <device-key>   — the device's shared secret
//   X-Device-ID: <device-id>             — unique identifier for logging
//
// If ALLOWED_DEVICE_KEYS is set in .env, only those keys are accepted.
// If the env var is empty, all keys are allowed (open mode — development only).
// Authenticated device IDs are attached to req.deviceId for downstream use.

import { Request, Response, NextFunction } from 'express';
import { config } from '../config';

// Extends Express Request so downstream handlers can read req.deviceId
export interface AuthenticatedRequest extends Request {
  deviceId?: string;
}

export function deviceAuth(
  req: AuthenticatedRequest,
  res: Response,
  next: NextFunction
): void {
  const authHeader = req.headers.authorization;
  const deviceId = req.headers['x-device-id'] as string | undefined;

  if (!authHeader || !authHeader.startsWith('Bearer ')) {
    res.status(401).json({
      error: 'Unauthorized',
      message: 'Missing or invalid Authorization header',
    });
    return;
  }

  const deviceKey = authHeader.slice(7);

  if (!deviceId) {
    res.status(400).json({
      error: 'Bad Request',
      message: 'Missing X-Device-ID header',
    });
    return;
  }

  if (config.allowedDeviceKeys.size > 0 && !config.allowedDeviceKeys.has(deviceKey)) {
    console.warn(`[Auth] Rejected device key attempt for device: ${deviceId}`);
    res.status(403).json({
      error: 'Forbidden',
      message: 'Invalid device key',
    });
    return;
  }

  req.deviceId = deviceId;
  console.log(`[Auth] Device authenticated: ${deviceId}`);
  next();
}
