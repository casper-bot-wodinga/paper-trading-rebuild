# Architecture — Hardware, Data Flow & Agent Communication

**Parent**: [SPEC.md](../SPEC.md)
**Updated**: 2026-07-09

---

## Hardware

| Machine | IP | Role | Resources |
|---------|----|------|-----------|
| **Hermes** | .131 | Orchestrator, spec-keeper, PR reviewer | Coordinates, fixes breakages |
| **OpenClaw** | .41 | Agent host — traders live here | DeepSeek/Gemini via API |
| **Docker** | .179 | Backtest workers, replay harness | 20 parallel containers |

Data flow: Postgres on Docker (.179:5433), data bus on OpenClaw (.41:5000), TrueNAS (.96) for historical archives.

## Agent Communication

### Primary: Native Webhooks

| Endpoint | Host | Purpose | Auth |
|----------|------|---------|------|
| `/hooks/wake` | OpenClaw .41:18789 | Inbound messages to Casper | Bearer: `hermes-hook-2026` |
| `/hooks/agent` | OpenClaw .41:18789 | Agent-to-agent dispatch | Shared token |
| `POST /webhooks/main` | Hermes .131:8644 | Casper → Hermes outbound | HMAC-SHA256 |

### Fallback: Chat Bridge

`OpenClaw .41:8644/send` with Bearer token for Hermes → Casper messages when webhooks are unavailable. Used for initial setup and recovery.

### Bidirectional Flow

```
Casper → Hermes:  POST hermes:8644/webhooks/main (HMAC signed)
Hermes → Casper:  POST openclaw:18789/hooks/wake (Bearer token)
```

## Message Persistence

All agent-to-agent messages stored in `hermes_inbox` table in the shared database for auditing and replay.