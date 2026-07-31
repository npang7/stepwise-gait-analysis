# StepWise WeChat Cloud Hosting Deployment

Build from the repository root so the image always receives the canonical analysis
sources:

```bash
docker build -f stepwise_cloudrun_flask/Dockerfile -t stepwise-api .
docker run --rm -p 8080:80 stepwise-api
```

## Service

- Framework: Flask
- Port: 80
- API: `POST /api/analyze-text`
- Body:

```json
{
  "walkingText": "TXT file content",
  "standingText": "optional standing calibration TXT content"
}
```

## Configuration

The service accepts these optional environment variables:

- `STEPWISE_DATA_DIR`: writable root for uploads and generated reports.
- `STEPWISE_MAX_INPUT_CHARS`: maximum characters allowed per uploaded TXT payload
  (default: 2,000,000).

Set `apiBase`, `cloudEnv`, and `cloudService` in
`stepwise_miniprogram/config.js` for the target environment. No public service URL
is checked into the repository.

## Note

Generated report files are stored in the container's temporary filesystem. This
is appropriate for the prototype only. A production service would require durable
object storage, authentication, monitoring, and a data-retention policy.
