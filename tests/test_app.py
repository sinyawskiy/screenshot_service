import base64
import threading
import time
import unittest
from io import BytesIO
from urllib.parse import urlencode
from unittest.mock import patch

from fastapi import Request
from PIL import Image
from selenium.common.exceptions import TimeoutException, WebDriverException

import app


def make_png(width=16, height=12):
    output = BytesIO()
    Image.new("RGB", (width, height), color=(20, 100, 180)).save(output, format="PNG")
    return output.getvalue()


def image_size(png):
    with Image.open(BytesIO(png)) as image:
        return image.size


PNG = make_png()


class ScreenshotApiTests(unittest.TestCase):
    def test_root_status(self):
        self.assertEqual(app.status(), {"status": "ok"})

    def get(self, params):
        query_string = urlencode(params).encode("utf-8")
        request = Request({"type": "http", "method": "GET", "path": "/screenshot",
                           "query_string": query_string, "headers": []})
        return app.take_screenshot(request)

    def test_default_parameters_and_png_response(self):
        with patch.object(app.screenshot_engine, "capture", return_value=PNG) as capture:
            response = self.get({"url": "https://example.com/a?x=1&y=2"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.media_type, "image/png")
        self.assertEqual(response.body, PNG)
        capture.assert_called_once_with(
            url="https://example.com/a?x=1&y=2",
            width=400,
            height=300,
            delay_ms=1000,
            navigation_timeout_ms=app.NAVIGATION_TIMEOUT_MS,
            user_agent=app.DEFAULT_USER_AGENT,
            full_page=False,
            redirect_wait_ms=app.REDIRECT_WAIT_MS,
        )

    def test_custom_parameters(self):
        with patch.object(app.screenshot_engine, "capture", return_value=PNG) as capture:
            response = self.get({"url": "http://example.com", "width": "1920", "height": "1080",
                                 "delay_ms": "0", "timeout_ms": "5000", "full_page": "true",
                                 "redirect_wait_ms": "2500", "user_agent": "Custom browser"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(capture.call_args.kwargs["width"], 1920)
        self.assertEqual(capture.call_args.kwargs["height"], 1080)
        self.assertEqual(capture.call_args.kwargs["navigation_timeout_ms"], 5000)
        self.assertEqual(capture.call_args.kwargs["full_page"], True)
        self.assertEqual(capture.call_args.kwargs["redirect_wait_ms"], 2500)
        self.assertEqual(capture.call_args.kwargs["user_agent"], "Custom browser")

    def test_rejects_missing_or_invalid_parameters(self):
        cases = [
            ({}, "URL parameter is required"),
            ({"url": "file:///etc/passwd"}, "url must be"),
            ({"url": "https://example.com", "width": "0"}, "width must be"),
            ({"url": "https://example.com", "height": "abc"}, "height must be"),
            ({"url": "https://example.com", "delay_ms": "-1"}, "delay_ms must be"),
            ({"url": "https://example.com", "timeout_ms": "0"}, "timeout_ms must be"),
            ({"url": "https://example.com", "timeout_ms": "abc"}, "timeout_ms must be"),
            ({"url": "https://example.com", "redirect_wait_ms": "-1"}, "redirect_wait_ms must be"),
            ({"url": "https://example.com", "full_page": "maybe"}, "full_page must be"),
        ]
        with patch.object(app.screenshot_engine, "capture") as capture:
            for params, message in cases:
                with self.subTest(params=params):
                    response = self.get(params)
                    self.assertEqual(response.status_code, 400)
                    self.assertIn(message, response.body.decode())
        capture.assert_not_called()

    def test_capture_errors_have_expected_statuses(self):
        cases = [(RuntimeError("busy"), 503), (TimeoutException(), 504),
                 (app.PageUnavailableError(), 502),
                 (WebDriverException("failed"), 500)]
        for error, status in cases:
            with self.subTest(error=type(error).__name__):
                with patch.object(app.screenshot_engine, "capture", side_effect=error):
                    response = self.get({"url": "https://example.com"})
                self.assertEqual(response.status_code, status)


class FakeDriver:
    def __init__(self):
        self.commands = []
        self.closed = False
        self.timeout = None
        self.url = None
        self.navigation_error = None
        self.ready_state = "complete"
        self.viewport = [0, 0]
        self.current_url_sequence = None
        self.current_url_index = 0
        self.async_scripts = []

    @property
    def current_url(self):
        if self.current_url_sequence:
            index = min(self.current_url_index, len(self.current_url_sequence) - 1)
            self.current_url_index += 1
            return self.current_url_sequence[index]
        return self.url

    def set_page_load_timeout(self, timeout):
        self.timeout = timeout

    def get(self, url):
        self.url = url
        if self.navigation_error:
            raise self.navigation_error

    def execute_cdp_cmd(self, name, args):
        self.commands.append((name, args))
        if name == "Emulation.setDeviceMetricsOverride":
            self.viewport = [args["width"], args["height"]]
        if name == "Page.getLayoutMetrics":
            return {"cssContentSize": {"height": 1400.2}}
        if name == "Page.captureScreenshot":
            return {"data": base64.b64encode(PNG).decode("ascii")}
        return {}

    def execute_script(self, script):
        if "document.readyState" in script:
            return self.ready_state
        if "window.innerWidth" in script:
            return list(self.viewport)
        raise AssertionError(f"Unexpected script: {script}")

    def execute_async_script(self, script):
        self.async_scripts.append(script)

    def quit(self):
        self.closed = True


class SeleniumCaptureTests(unittest.TestCase):
    def test_viewport_capture(self):
        driver = FakeDriver()
        result = app.capture_screenshot(
            driver, "https://example.com", 800, 600, 0, 30000, "UA", False, redirect_wait_ms=0
        )
        self.assertEqual(image_size(result), (800, 600))
        self.assertEqual(driver.timeout, 30)
        self.assertEqual(driver.url, "https://example.com")
        self.assertEqual(driver.commands[0], ("Network.setUserAgentOverride", {"userAgent": "UA"}))
        self.assertEqual(driver.commands[3][1]["width"], app.BROWSER_WIDTH)
        self.assertEqual(driver.commands[3][1]["height"], app.BROWSER_HEIGHT)
        self.assertEqual(driver.viewport, [app.BROWSER_WIDTH, app.BROWSER_HEIGHT])
        self.assertEqual(driver.commands[-2][0], "Emulation.setDeviceMetricsOverride")
        self.assertEqual(driver.commands[-2][1]["width"], app.BROWSER_WIDTH)
        self.assertEqual(driver.commands[-2][1]["height"], app.BROWSER_HEIGHT)
        self.assertEqual(driver.commands[-1][1]["clip"]["width"], app.BROWSER_WIDTH)
        self.assertEqual(driver.commands[-1][1]["clip"]["height"], app.BROWSER_HEIGHT)
        self.assertEqual(len(driver.async_scripts), 1)
        self.assertFalse(driver.closed)

    def test_full_page_height(self):
        driver = FakeDriver()
        result = app.capture_screenshot(
            driver, "https://example.com", 800, 600, 0, 30000, "UA", True, redirect_wait_ms=0
        )
        self.assertEqual(image_size(result), (800, 600))
        self.assertEqual(driver.commands[-1][1]["clip"]["width"], app.BROWSER_WIDTH)
        self.assertEqual(driver.commands[-1][1]["clip"]["height"], 1401)
        self.assertTrue(driver.commands[-1][1]["captureBeyondViewport"])

    def test_resize_png_changes_only_image_dimensions(self):
        source = make_png(1024, 768)

        result = app.resize_png(source, 400, 300)

        self.assertEqual(image_size(source), (1024, 768))
        self.assertEqual(image_size(result), (400, 300))

    def test_unavailable_site_raises_page_error(self):
        driver = FakeDriver()
        driver.navigation_error = WebDriverException("net::ERR_NAME_NOT_RESOLVED")
        with self.assertRaises(app.PageUnavailableError):
            app.capture_screenshot(driver, "https://missing.example", 400, 300, 0, 5000, "UA", False)
        self.assertEqual(driver.timeout, 5)

    def test_waits_for_client_redirect_chain(self):
        driver = FakeDriver()
        driver.current_url_sequence = [
            "https://start.example/",
            "https://start.example/",
            "https://middle.example/",
            "https://final.example/",
        ]

        final_url, redirect_count = app.wait_for_final_document(
            driver,
            quiet_period_ms=1,
            navigation_timeout_ms=100,
            poll_interval_seconds=0,
        )

        self.assertEqual(final_url, "https://final.example/")
        self.assertEqual(redirect_count, 2)

    def test_redirect_loop_times_out(self):
        class RedirectLoopDriver(FakeDriver):
            @property
            def current_url(self):
                self.current_url_index += 1
                return f"https://example.com/{self.current_url_index % 2}"

        with self.assertRaises(TimeoutException):
            app.wait_for_final_document(
                RedirectLoopDriver(),
                quiet_period_ms=10,
                navigation_timeout_ms=2,
                poll_interval_seconds=0,
            )

    def test_single_browser_is_reused_and_closed_on_shutdown(self):
        driver = FakeDriver()
        engine = app.ScreenshotEngine(10, 0)
        kwargs = dict(url="https://example.com", width=800, height=600, delay_ms=0,
                      navigation_timeout_ms=30000, user_agent="UA", full_page=False,
                      redirect_wait_ms=0)
        with patch.object(app, "create_driver", return_value=driver) as create:
            self.assertEqual(image_size(engine.capture(**kwargs)), (800, 600))
            self.assertEqual(image_size(engine.capture(**kwargs)), (800, 600))
        create.assert_called_once_with()
        self.assertFalse(driver.closed)
        engine.close()
        self.assertTrue(driver.closed)

    def test_browser_restarts_after_navigation_failure(self):
        driver = FakeDriver()
        driver.navigation_error = TimeoutException("timed out")
        replacement = FakeDriver()
        engine = app.ScreenshotEngine(10, 0)
        kwargs = dict(url="https://example.com", width=800, height=600, delay_ms=0,
                      navigation_timeout_ms=30000, user_agent="UA", full_page=False,
                      redirect_wait_ms=0)
        with patch.object(app, "create_driver", side_effect=[driver, replacement]):
            with self.assertRaises(TimeoutException):
                engine.capture(**kwargs)
            self.assertEqual(image_size(engine.capture(**kwargs)), (800, 600))
        self.assertTrue(driver.closed)
        engine.close()
        self.assertTrue(replacement.closed)

    def test_idle_browser_closes_and_next_request_restarts_it(self):
        first = FakeDriver()
        second = FakeDriver()
        engine = app.ScreenshotEngine(10, 0.05)
        kwargs = dict(url="https://example.com", width=800, height=600, delay_ms=0,
                      navigation_timeout_ms=30000, user_agent="UA", full_page=False,
                      redirect_wait_ms=0)
        with patch.object(app, "create_driver", side_effect=[first, second]):
            self.assertEqual(image_size(engine.capture(**kwargs)), (800, 600))
            self.assertTrue(self.wait_until(lambda: first.closed))
            self.assertEqual(image_size(engine.capture(**kwargs)), (800, 600))
        engine.close()
        self.assertTrue(second.closed)

    def test_queued_requests_run_in_order(self):
        driver = FakeDriver()
        engine = app.ScreenshotEngine(10, 0)
        first_started = threading.Event()
        release_first = threading.Event()
        order = []

        def slow_capture(_driver, **kwargs):
            order.append(kwargs["url"])
            if kwargs["url"].endswith("first"):
                first_started.set()
                release_first.wait(timeout=2)
            return PNG

        def run(url):
            engine.capture(url=url)

        with patch.object(app, "create_driver", return_value=driver), patch.object(
            app, "capture_screenshot", side_effect=slow_capture
        ):
            first_thread = threading.Thread(target=run, args=("first",))
            second_thread = threading.Thread(target=run, args=("second",))
            first_thread.start()
            self.assertTrue(first_started.wait(timeout=2))
            second_thread.start()
            self.assertTrue(self.wait_until(lambda: engine._queue.qsize() == 1))
            release_first.set()
            first_thread.join(timeout=2)
            second_thread.join(timeout=2)
        engine.close()
        self.assertEqual(order, ["first", "second"])
        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())

    def test_full_queue_rejects_new_requests(self):
        engine = app.ScreenshotEngine(1, 0)
        first_started = threading.Event()
        release_first = threading.Event()

        def slow_capture(_driver, **_kwargs):
            first_started.set()
            release_first.wait(timeout=2)
            return PNG

        with patch.object(app, "create_driver", return_value=FakeDriver()), patch.object(
            app, "capture_screenshot", side_effect=slow_capture
        ):
            first_thread = threading.Thread(target=engine.capture)
            second_thread = threading.Thread(target=engine.capture)
            first_thread.start()
            self.assertTrue(first_started.wait(timeout=2))
            second_thread.start()
            self.assertTrue(self.wait_until(lambda: engine._queue.qsize() == 1))
            with self.assertRaisesRegex(RuntimeError, "Server is busy"):
                engine.capture()
            release_first.set()
            first_thread.join(timeout=2)
            second_thread.join(timeout=2)
        engine.close()
        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())

    @staticmethod
    def wait_until(predicate, timeout=2):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()


if __name__ == "__main__":
    unittest.main()
