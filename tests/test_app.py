import base64
import threading
import time
import unittest
from urllib.parse import urlencode
from unittest.mock import patch

from fastapi import Request
from selenium.common.exceptions import TimeoutException, WebDriverException

import app


PNG = b"\x89PNG\r\n\x1a\nexample"


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
        )

    def test_custom_parameters(self):
        with patch.object(app.screenshot_engine, "capture", return_value=PNG) as capture:
            response = self.get({"url": "http://example.com", "width": "1920", "height": "1080",
                                 "delay_ms": "0", "timeout_ms": "5000", "full_page": "true",
                                 "user_agent": "Custom browser"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(capture.call_args.kwargs["width"], 1920)
        self.assertEqual(capture.call_args.kwargs["height"], 1080)
        self.assertEqual(capture.call_args.kwargs["navigation_timeout_ms"], 5000)
        self.assertEqual(capture.call_args.kwargs["full_page"], True)
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

    def set_page_load_timeout(self, timeout):
        self.timeout = timeout

    def get(self, url):
        self.url = url
        if self.navigation_error:
            raise self.navigation_error

    def execute_cdp_cmd(self, name, args):
        self.commands.append((name, args))
        if name == "Page.getLayoutMetrics":
            return {"cssContentSize": {"height": 1400.2}}
        if name == "Page.captureScreenshot":
            return {"data": base64.b64encode(PNG).decode("ascii")}
        return {}

    def quit(self):
        self.closed = True


class SeleniumCaptureTests(unittest.TestCase):
    def test_viewport_capture(self):
        driver = FakeDriver()
        result = app.capture_screenshot(driver, "https://example.com", 800, 600, 0, 30000, "UA", False)
        self.assertEqual(result, PNG)
        self.assertEqual(driver.timeout, 30)
        self.assertEqual(driver.url, "https://example.com")
        self.assertEqual(driver.commands[0], ("Network.setUserAgentOverride", {"userAgent": "UA"}))
        self.assertEqual(driver.commands[3][1]["width"], 800)
        self.assertEqual(driver.commands[-1][1]["clip"]["height"], 600)
        self.assertFalse(driver.closed)

    def test_full_page_height(self):
        driver = FakeDriver()
        app.capture_screenshot(driver, "https://example.com", 800, 600, 0, 30000, "UA", True)
        self.assertEqual(driver.commands[-1][1]["clip"]["height"], 1401)
        self.assertTrue(driver.commands[-1][1]["captureBeyondViewport"])

    def test_unavailable_site_raises_page_error(self):
        driver = FakeDriver()
        driver.navigation_error = WebDriverException("net::ERR_NAME_NOT_RESOLVED")
        with self.assertRaises(app.PageUnavailableError):
            app.capture_screenshot(driver, "https://missing.example", 400, 300, 0, 5000, "UA", False)
        self.assertEqual(driver.timeout, 5)

    def test_single_browser_is_reused_and_closed_on_shutdown(self):
        driver = FakeDriver()
        engine = app.ScreenshotEngine(10, 0)
        kwargs = dict(url="https://example.com", width=800, height=600, delay_ms=0,
                      navigation_timeout_ms=30000, user_agent="UA", full_page=False)
        with patch.object(app, "create_driver", return_value=driver) as create:
            self.assertEqual(engine.capture(**kwargs), PNG)
            self.assertEqual(engine.capture(**kwargs), PNG)
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
                      navigation_timeout_ms=30000, user_agent="UA", full_page=False)
        with patch.object(app, "create_driver", side_effect=[driver, replacement]):
            with self.assertRaises(TimeoutException):
                engine.capture(**kwargs)
            self.assertEqual(engine.capture(**kwargs), PNG)
        self.assertTrue(driver.closed)
        engine.close()
        self.assertTrue(replacement.closed)

    def test_idle_browser_closes_and_next_request_restarts_it(self):
        first = FakeDriver()
        second = FakeDriver()
        engine = app.ScreenshotEngine(10, 0.05)
        kwargs = dict(url="https://example.com", width=800, height=600, delay_ms=0,
                      navigation_timeout_ms=30000, user_agent="UA", full_page=False)
        with patch.object(app, "create_driver", side_effect=[first, second]):
            self.assertEqual(engine.capture(**kwargs), PNG)
            self.assertTrue(self.wait_until(lambda: first.closed))
            self.assertEqual(engine.capture(**kwargs), PNG)
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
