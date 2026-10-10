# Speakly Clone Engine (Cloud Run)

Open-source **XTTS-v2** (Coqui) voice-cloning microservice that powers the voice
cloner in the [Speakly](https://github.com/ahmad786-coder/speakly) studio.

Runs as a container on Google Cloud Run. The model is baked into the image so
cold starts don't re-download it.

Endpoints:

- `GET /health` — engine status
- `GET /languages` — supported languages
- `POST /clone` — `multipart/form-data` (`text`, `language`, `speaker_wav`) → `{ jobId }`
- `GET /clone?job=<id>` — job status → `{ status, audio (base64 wav) }`

Cloning runs as a background job so long generations never hold a single HTTP
request open. The server listens on `$PORT` (Cloud Run sets this automatically).

### Deploy

Cloud Run console → Create Service → "Continuously deploy from a repository" →
pick this repo → Dockerfile build → allow unauthenticated → set CPU allocation
to **CPU always allocated**, max instances **1**, memory **4 GiB**, CPU **2**.
