// Token service entry point — Express server that acts as a secure proxy
// between iTranslate devices and the AssemblyAI V3 API.
//
// Why this exists:
//   Embedding the master AssemblyAI API key in firmware is a security risk.
//   Instead, each device authenticates with its own device key, and this service
//   exchanges that for a short-lived AssemblyAI token (1–600 seconds).
//   The device uses the token to open a WebSocket directly to AssemblyAI.
//
// Route structure:
//   POST /api/token  — generate an AssemblyAI temporary token (auth required)
//   GET  /health     — health check (no auth)
//
// Middleware stack (applied in order):
//   helmet  — sets secure HTTP headers
//   cors    — restricts origins and allowed headers
//   deviceAuth — validates Authorization: Bearer <device-key> and X-Device-ID

import express from 'express';
import cors from 'cors';
import helmet from 'helmet';
import { config } from './config';
import { deviceAuth } from './middleware/auth';
import tokenRouter from './routes/token';

const app = express();

// Security headers (Content-Security-Policy, X-Frame-Options, etc.)
app.use(helmet());
app.use(cors({
  origin: config.corsOrigins,
  methods: ['GET', 'POST'],
  allowedHeaders: ['Content-Type', 'Authorization', 'X-Device-ID'],
}));
app.use(express.json());

// All /api routes require device authentication before reaching the router
app.use('/api', deviceAuth, tokenRouter);

app.get('/health', (_req, res) => {
  res.json({
    status: 'healthy',
    service: 'itranslate-token-service',
    timestamp: new Date().toISOString(),
  });
});

app.use((_req, res) => {
  res.status(404).json({
    error: 'Not Found',
    message: 'The requested endpoint does not exist',
  });
});

app.use((err: Error, _req: express.Request, res: express.Response, _next: express.NextFunction) => {
  console.error('[Error]', err);
  res.status(500).json({
    error: 'Internal Server Error',
    message: process.env.NODE_ENV === 'development' ? err.message : 'An unexpected error occurred',
  });
});

app.listen(config.port, () => {
  console.log(`
╔═══════════════════════════════════════════════════════════╗
║           iTranslate Token Service                        ║
╠═══════════════════════════════════════════════════════════╣
║  Status:    Running                                       ║
║  Port:      ${String(config.port).padEnd(45)}║
║  Endpoints:                                               ║
║    POST /api/token  - Generate AssemblyAI temp token      ║
║    GET  /health     - Service health check                ║
╚═══════════════════════════════════════════════════════════╝
  `);
});

export default app;
