# Screenshot service

FastAPI service that returns PNG screenshots made by Selenium and headless Chromium.

## Run in Docker

Recommended deployment with Compose:

```bash
# Stop the previous manually started container if it still occupies port 5000.
docker stop screenshot-service  # only if it is running
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:5000/
```

The Compose file publishes port 5000 only on the host's loopback interface. To use another host port, set `SCREENSHOT_PORT` in a local `.env` file. It also creates the `screenshot_backend` Docker network. If Django runs in another Compose project on the same server, attach its service to that external network:

```yaml
services:
  django:
    networks:
      - screenshot_backend

networks:
  screenshot_backend:
    external: true
    name: screenshot_backend
```

Then set `SCREENSHOT_SERVICE_URL=http://screenshot-service:5000/screenshot` in Django's configuration. The published localhost port is for checks on the Docker host; containers should use the service name on the shared network. Update the service with `docker compose up -d --build` and inspect logs with `docker compose logs -f screenshot-service`.

Manual Docker commands are also available:

```bash
docker build -t screenshot-service .
docker run --rm -p 127.0.0.1:5000:5000 --name screenshot-service screenshot-service
```

Open `http://localhost:5000/` to check the service; it returns `{"status":"ok"}`. The interactive API documentation is at `http://localhost:5000/docs`. After changing the code, stop the existing container, rebuild the image, and run it again; a container restart alone keeps the old image.

The container includes Chromium and the matching ChromeDriver package. Selenium controls Chromium; using Selenium does not eliminate the browser from the image. Requests enter a FIFO queue. One worker starts Chromium on the first request and reuses it, processing one screenshot at a time. After five minutes without requests, the worker closes Chromium to free memory. The next request starts it again. A browser failure also causes a restart on the next request.

## API

`GET /screenshot` accepts these query parameters:

| Parameter | Default | Meaning |
| --- | --- | --- |
| `url` | required | HTTP or HTTPS page URL |
| `width` | `400` | viewport and PNG width, 1–10000 px |
| `height` | `300` | viewport and normal PNG height, 1–10000 px |
| `full_page` | `false` | capture the full page height (`true`/`false`) |
| `delay_ms` | `1000` | wait after DOMContentLoaded, 0–60000 ms |
| `timeout_ms` | `30000` | page navigation timeout, 1–120000 ms |
| `user_agent` | Chrome-like desktop UA | browser User-Agent |

Successful responses have `Content-Type: image/png`. Errors return plain text: `400` for invalid parameters, `502` when the target site cannot be opened, `503` when the queue is full, `504` for page load timeout, and `500` for browser failures. `full_page=true` uses the requested `height` as the viewport height and captures at least that much of the page. `timeout_ms` limits page navigation only; time in the queue, browser startup, and `delay_ms` are additional.

```bash
curl -G 'http://localhost:5000/screenshot' \
  --data-urlencode 'url=https://example.com/path?a=1&b=2' \
  --data-urlencode 'width=1920' \
  --data-urlencode 'height=1080' \
  --data-urlencode 'timeout_ms=5000' \
  -o screenshot.png
```

For the Django `Screenshoter` client, store the service URL in settings and pass the page URL through `params`, for example:

```python
# settings.py
SCREENSHOT_SERVICE_URL = "http://screenshot-service:5000/screenshot"

# screenshoter.py
resp = requests.get(
    settings.SCREENSHOT_SERVICE_URL,
    params={"url": self.resource.link},
    stream=True,
)
```

The current `screenshoter.py` uses `https://mini.s-shot.ru/?<page URL>`; that request format is not supported by this service.

## Configuration

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `MAX_QUEUE_SIZE` | `100` | maximum number of waiting requests; further requests get `503` |
| `BROWSER_IDLE_TIMEOUT_SECONDS` | `300` | seconds before closing an idle browser; `0` keeps it open |
| `NAVIGATION_TIMEOUT_MS` | `30000` | Selenium page load timeout |
| `CHROMIUM_EXECUTABLE_PATH` | `/usr/bin/chromium-browser` in Docker | browser binary |
| `CHROMEDRIVER_PATH` | `/usr/bin/chromedriver` | driver binary |

Keep one Uvicorn worker to keep one browser process; each additional worker would start its own browser.

## Tests

With the packages in `requirements.txt` installed:

```bash
python -m unittest discover -s tests -v
```
