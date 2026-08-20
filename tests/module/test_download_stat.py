"""Tests for bounded download progress state and bot status updates."""
import asyncio
import time
import unittest
from unittest.mock import AsyncMock

from module.app import TaskNode
from module import download_stat
from module.pyrogram_extension import (
    MAX_BOT_STATUS_MESSAGE_LENGTH,
    _truncate_bot_status_message,
    report_bot_status,
)
from module.web import get_flask_app


class DownloadStatTestCase(unittest.TestCase):
    @staticmethod
    def run_async(coroutine):
        """Run coroutines without clearing the suite's current event loop."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(coroutine)

    def setUp(self):
        with download_stat._download_result_lock:
            download_stat._download_result.clear()

    def tearDown(self):
        with download_stat._download_result_lock:
            download_stat._download_result.clear()

    def test_finished_download_is_not_returned_as_active(self):
        node = TaskNode(chat_id=101, task_id=7)

        self.run_async(
            download_stat.update_download_status(
                50, 100, 11, "video.mp4", time.time() - 1, node, AsyncMock()
            )
        )

        self.assertIn(11, download_stat.get_active_download_result(101, 7))

        download_stat.finish_download_status(101, 11, True)

        self.assertEqual({}, download_stat.get_active_download_result(101, 7))
        result = download_stat.get_download_result()[101][11]
        self.assertEqual(result["down_byte"], 100)
        self.assertFalse(result["is_active"])

    def test_completed_history_has_a_fixed_limit(self):
        original_limit = download_stat.MAX_COMPLETED_DOWNLOAD_RESULTS_PER_CHAT
        download_stat.MAX_COMPLETED_DOWNLOAD_RESULTS_PER_CHAT = 2
        node = TaskNode(chat_id=101, task_id=7)
        try:
            for message_id in range(1, 4):
                self.run_async(
                    download_stat.update_download_status(
                        10,
                        10,
                        message_id,
                        f"{message_id}.mp4",
                        time.time() - 1,
                        node,
                        AsyncMock(),
                    )
                )
                download_stat.finish_download_status(101, message_id, True)

            self.assertEqual(2, len(download_stat.get_download_result()[101]))
        finally:
            download_stat.MAX_COMPLETED_DOWNLOAD_RESULTS_PER_CHAT = original_limit

    def test_web_lists_active_and_finished_downloads_separately(self):
        node = TaskNode(chat_id=101, task_id=7)
        self.run_async(
            download_stat.update_download_status(
                50, 100, 11, "video.mp4", time.time() - 1, node, AsyncMock()
            )
        )
        flask_app = get_flask_app()
        original_login_disabled = flask_app.config.get("LOGIN_DISABLED")
        flask_app.config["LOGIN_DISABLED"] = True
        try:
            with flask_app.test_client() as client:
                active_response = client.get("/get_download_list?already_down=false")
                self.assertEqual(1, len(active_response.get_json()))

                download_stat.finish_download_status(101, 11, True)

                active_response = client.get("/get_download_list?already_down=false")
                finished_response = client.get("/get_download_list?already_down=true")
                self.assertEqual([], active_response.get_json())
                self.assertEqual(1, len(finished_response.get_json()))
        finally:
            flask_app.config["LOGIN_DISABLED"] = original_login_disabled

    def test_failed_download_is_visible_with_reason_in_completed_list(self):
        node = TaskNode(chat_id=101, task_id=7)
        download_stat.finish_download_status(
            101,
            12,
            False,
            state="failed",
            reason="ConnectionError: temporary API failure",
            file_name="broken-video.mp4",
            total_size=100,
            node=node,
            attempt=3,
            retry_count=2,
        )

        flask_app = get_flask_app()
        original_login_disabled = flask_app.config.get("LOGIN_DISABLED")
        flask_app.config["LOGIN_DISABLED"] = True
        try:
            with flask_app.test_client() as client:
                active_response = client.get("/get_download_list?already_down=false")
                finished_response = client.get("/get_download_list?already_down=true")
                self.assertEqual([], active_response.get_json())
                item = finished_response.get_json()[0]
                self.assertEqual("failed", item["status"])
                self.assertEqual("ConnectionError: temporary API failure", item["reason"])
                self.assertEqual(3, item["attempt"])
        finally:
            flask_app.config["LOGIN_DISABLED"] = original_login_disabled

    def test_retrying_task_is_visible_before_the_next_attempt(self):
        node = TaskNode(chat_id=101, task_id=7)
        download_stat.mark_download_retrying(
            101, 13, node, 1, "TimeoutError: request timed out"
        )

        item = download_stat.get_active_download_result(101, 7)[13]
        self.assertEqual("retrying", item["state"])
        self.assertEqual(1, item["retry_count"])
        self.assertEqual("TimeoutError: request timed out", item["reason"])

    def test_bot_status_retries_after_an_edit_error(self):
        node = TaskNode(
            chat_id=101,
            from_user_id=1,
            reply_message_id=2,
            bot=object(),
            task_id=7,
        )
        client = AsyncMock()
        client.edit_message_text.side_effect = RuntimeError("temporary error")

        self.run_async(report_bot_status(client, node, immediate_reply=True))

        self.assertEqual(node.last_edit_msg, "")

        client.edit_message_text.side_effect = None
        self.run_async(report_bot_status(client, node, immediate_reply=True))

        self.assertNotEqual(node.last_edit_msg, "")
        self.assertEqual(client.edit_message_text.await_count, 2)

    def test_bot_status_message_is_truncated(self):
        message = "`" + ("a" * MAX_BOT_STATUS_MESSAGE_LENGTH)

        result = _truncate_bot_status_message(message)

        self.assertLessEqual(len(result), MAX_BOT_STATUS_MESSAGE_LENGTH)
        self.assertTrue(result.endswith("\n...\n`"))

    def test_bot_status_includes_failed_task_details(self):
        node = TaskNode(
            chat_id=101,
            from_user_id=1,
            reply_message_id=2,
            bot=object(),
            task_id=7,
        )
        node.download_history[12] = {
            "status": "failed",
            "file_name": "broken-video.mp4",
            "reason": "ConnectionError: temporary API failure",
        }
        client = AsyncMock()

        self.run_async(report_bot_status(client, node, immediate_reply=True))

        message = client.edit_message_text.await_args.args[2]
        self.assertIn("Failed details", message)
        self.assertIn("broken-video.mp4", message)

    def test_collecting_task_cannot_be_finished_early(self):
        node = TaskNode(chat_id=101)
        node.is_running = True
        node.is_collecting_tasks = True

        self.assertFalse(node.is_finish())

        node.is_collecting_tasks = False
        self.assertTrue(node.is_finish())
