import dotenv from 'dotenv';
import path from 'path';

dotenv.config({ path: path.resolve(__dirname, '../../.env') });

interface Config {
  port: number;
  assemblyAiApiKey: string;
  tokenExpiresIn: number;
  allowedDeviceKeys: Set<string>;
  corsOrigins: string[];
}

function getEnvVar(name: string, required = true): string {
  const value = process.env[name];
  if (required && !value) {
    throw new Error(`Missing required environment variable: ${name}`);
  }
  return value || '';
}

function parseDeviceKeys(keysString: string): Set<string> {
  if (!keysString) return new Set();
  return new Set(keysString.split(',').map(key => key.trim()).filter(Boolean));
}

export const config: Config = {
  port: parseInt(process.env.PORT || '3001', 10),
  assemblyAiApiKey: getEnvVar('ASSEMBLYAI_API_KEY'),
  tokenExpiresIn: parseInt(process.env.TOKEN_EXPIRES_IN || '60', 10),
  allowedDeviceKeys: parseDeviceKeys(process.env.ALLOWED_DEVICE_KEYS || ''),
  corsOrigins: (process.env.CORS_ORIGINS || 'http://localhost:3000').split(','),
};
