# Render Deployment

This project is a full Flask video-analysis agent. Use Render Web Service with
Docker so FFmpeg and Chinese PDF fonts are installed consistently.

## Recommended Render Settings

- Service type: Web Service
- Runtime: Docker
- Dockerfile path: `./Dockerfile`
- Branch: your deployment branch, usually `main`

The Dockerfile already starts the app with:

```bash
gunicorn app:app --bind 0.0.0.0:${PORT:-5001} --workers 1 --threads 8 --timeout 300
```

## Environment Variables

Add these in Render > Environment:

- `DEEPSEEK_API_KEY`: your DeepSeek API key
- `QWEN_API_KEY`: your DashScope / Qwen API key
- `DATA_DIR`: `/var/data`

## Persistent Disk

Add a Persistent Disk if you want to keep uploaded videos, analysis history, and
review-rule documents after redeploys or restarts.

- Mount path: `/var/data`
- Size: 10 GB is a reasonable starting point

Without a disk, the app can still start, but runtime files may be lost after a
redeploy or instance restart.

## Files That Must Not Be Uploaded

These are ignored by `.gitignore` and `.dockerignore`:

- `config.json`
- `.env`
- `uploads/`
- `output/`
- `review_rules/`
- uploaded videos, audio files, generated reports

## Security Note

Before sharing the public Render URL with other people, add login protection or
a simple access gate. This app handles API keys, uploaded videos, reports, and
client review rules, so it should not be left fully public for production use.
