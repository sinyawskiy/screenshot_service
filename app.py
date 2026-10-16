"""HTTP screenshot service backed by Selenium and headless Chromium."""

import base64
import logging
import math
import os
import queue
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.service import Service


logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
DEFAULT_WIDTH = 400
DEFAULT_HEIGHT = 300
MAX_DIMENSION = 10000
MAX_DELAY_MS = 60000
NAVIGATION_TIMEOUT_MS = int(os.environ.get("NAVIGATION_TIMEOUT_MS", "30000"))
MAX_NAVIGATION_TIMEOUT_MS = 120000
MAX_QUEUE_SIZE = int(os.environ.get("MAX_QUEUE_SIZE", "100"))
BROWSER_IDLE_TIMEOUT_SECONDS = int(os.environ.get("BROWSER_IDLE_TIMEOUT_SECONDS", "300"))


def parse_integer(name: str, value: str | None, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    try:
        result = int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if not minimum <= result <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


def parse_bool(value: str | None) -> bool:
    if value is None:
        return False
    if value.lower() in {"1", "true", "yes", "on"}:
        return True
    if value.lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError("full_page must be a boolean")


def validate_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
        # Accessing port also rejects malformed values such as :abc or :99999.
        _ = parsed.port
    except ValueError as error:
        raise ValueError("url must be a valid http or https URL") from error
    if not valid:
        raise ValueError("url must be a valid http or https URL")


class PageUnavailableError(Exception):
    """The target page could not be opened by Chromium."""


def create_driver() -> webdriver.Chrome:
    options = webdriver.ChromeOptions()
    options.binary_location = os.environ.get("CHROMIUM_EXECUTABLE_PATH", "/usr/bin/chromium")
    options.page_load_strategy = "eager"  # Return after DOMContentLoaded.
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    service = Service(executable_path=os.environ.get("CHROMEDRIVER_PATH", "/usr/bin/chromedriver"))
    return webdriver.Chrome(service=service, options=options)


def capture_screenshot(
    driver: webdriver.Chrome,
    url: str,
    width: int,
    height: int,
    delay_ms: int,
    navigation_timeout_ms: int,
    user_agent: str,
    full_page: bool,
) -> bytes:
    driver.set_page_load_timeout(navigation_timeout_ms / 1000)
    driver.execute_cdp_cmd("Network.setUserAgentOverride", {"userAgent": user_agent})
    driver.execute_cdp_cmd("Network.clearBrowserCookies", {})
    driver.execute_cdp_cmd("Network.clearBrowserCache", {})
    driver.execute_cdp_cmd(
        "Emulation.setDeviceMetricsOverride",
        {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
    )
    try:
        driver.get(url)
    except TimeoutException:
        raise
    except WebDriverException as error:
        raise PageUnavailableError("Target site is unavailable") from error
    if delay_ms:
        time.sleep(delay_ms / 1000)

    capture_height = height
    if full_page:
        metrics = driver.execute_cdp_cmd("Page.getLayoutMetrics", {})
        capture_height = math.ceil(max(height, metrics["cssContentSize"]["height"]))
    result = driver.execute_cdp_cmd(
        "Page.captureScreenshot",
        {
            "format": "png",
            "fromSurface": True,
            "captureBeyondViewport": full_page,
            "clip": {"x": 0, "y": 0, "width": width, "height": capture_height, "scale": 1},
        },
    )
    return base64.b64decode(result["data"])


@dataclass
class CaptureJob:
    kwargs: dict
    done: threading.Event = field(default_factory=threading.Event)
    result: bytes | None = None
    error: Exception | None = None


class ScreenshotEngine:
    def __init__(self, max_queue_size: int, idle_timeout_seconds: int):
        if max_queue_size < 1 or idle_timeout_seconds < 0:
            raise ValueError("Queue size must be positive and idle timeout non-negative")
        self._queue: queue.Queue[CaptureJob | None] = queue.Queue(maxsize=max_queue_size)
        self._idle_timeout = idle_timeout_seconds
        self._state_lock = threading.Lock()
        self._closed = False
        self._driver = None
        self._worker = threading.Thread(target=self._run, name="screenshot-browser", daemon=True)
        self._worker.start()

    def capture(self, **kwargs) -> bytes:
        job = CaptureJob(kwargs=kwargs)
        with self._state_lock:
            if self._closed:
                raise RuntimeError("Service is stopping")
            try:
                self._queue.put_nowait(job)
            except queue.Full as error:
                raise RuntimeError("Server is busy. Please retry shortly.") from error
        job.done.wait()
        if job.error is not None:
            raise job.error
        if job.result is None:
            raise ValueError("Screenshot worker returned no image")
        return job.result

    def _run(self) -> None:
        while True:
            try:
                timeout = self._idle_timeout if self._driver is not None and self._idle_timeout else None
                job = self._queue.get(timeout=timeout)
            except queue.Empty:
                self._close_driver()
                continue

            if job is None:
                self._close_driver()
                return
            try:
                if self._driver is None:
                    self._driver = create_driver()
                job.result = capture_screenshot(self._driver, **job.kwargs)
            except Exception as error:
                job.error = error
                self._close_driver()
            finally:
                job.done.set()

    def _close_driver(self) -> None:
        if self._driver is not None:
            driver, self._driver = self._driver, None
            try:
                driver.quit()
            except Exception:
                logger.warning("Browser did not shut down cleanly", exc_info=True)

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self._queue.put(None)
        self._worker.join(timeout=15)


screenshot_engine = ScreenshotEngine(MAX_QUEUE_SIZE, BROWSER_IDLE_TIMEOUT_SECONDS)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        yield
    finally:
        screenshot_engine.close()


app = FastAPI(title="Screenshot service", lifespan=lifespan)


@app.get("/")
def status() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/screenshot", responses={200: {"content": {"image/png": {}}}})
def take_screenshot(request: Request) -> Response:
    params = request.query_params
    url = params.get("url")
    if not url:
        return PlainTextResponse("URL parameter is required", status_code=400)

    try:
        validate_url(url)
        width = parse_integer("width", params.get("width"), DEFAULT_WIDTH, 1, MAX_DIMENSION)
        height = parse_integer("height", params.get("height"), DEFAULT_HEIGHT, 1, MAX_DIMENSION)
        delay_ms = parse_integer("delay_ms", params.get("delay_ms"), 1000, 0, MAX_DELAY_MS)
        timeout_ms = parse_integer(
            "timeout_ms", params.get("timeout_ms"), NAVIGATION_TIMEOUT_MS, 1, MAX_NAVIGATION_TIMEOUT_MS
        )
        full_page = parse_bool(params.get("full_page"))
        user_agent = params.get("user_agent") or DEFAULT_USER_AGENT
    except ValueError as error:
        return PlainTextResponse(str(error), status_code=400)

    try:
        png = screenshot_engine.capture(
            url=url,
            width=width,
            height=height,
            delay_ms=delay_ms,
            navigation_timeout_ms=timeout_ms,
            user_agent=user_agent,
            full_page=full_page,
        )
    except RuntimeError as error:
        return PlainTextResponse(str(error), status_code=503)
    except TimeoutException:
        return PlainTextResponse("Timed out while loading the page", status_code=504)
    except PageUnavailableError:
        logger.warning("Target site is unavailable: %s", url)
        return PlainTextResponse("Target site is unavailable", status_code=502)
    except WebDriverException:
        logger.exception("Browser failed to capture %s", url)
        return PlainTextResponse("Failed to take screenshot", status_code=500)
    except Exception:
        logger.exception("Unexpected screenshot failure for %s", url)
        return PlainTextResponse("Failed to take screenshot", status_code=500)
    return Response(content=png, media_type="image/png")
