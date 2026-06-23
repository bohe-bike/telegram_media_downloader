"""test app"""

import os
import sys
import unittest
from unittest import mock

import module.app
from module.app import Application, ChatDownloadConfig, DownloadStatus
from module.cloud_drive import CloudDrive

sys.path.append("..")  # Adds higher directory to python modules path.


class AppTestCase(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        config_test = os.path.join(os.path.abspath("."), "config_test.yaml")
        data_test = os.path.join(os.path.abspath("."), "data_test.yaml")
        if os.path.exists(config_test):
            os.remove(config_test)
        if os.path.exists(data_test):
            os.remove(data_test)

    def test_app(self):
        app = Application("", "")
        self.assertEqual(app.save_path, os.path.join(os.path.abspath("."), "downloads"))
        self.assertEqual(app.proxy, {})
        self.assertEqual(app.restart_program, False)
        self.assertEqual(app.cloud_drive_config.after_upload_file_delete, False)

        app.chat_download_config[123] = ChatDownloadConfig()
        app.chat_download_config[123].last_read_message_id = 13
        app.chat_download_config[123].node.download_status[
            6
        ] = DownloadStatus.Downloading
        app.chat_download_config[123].ids_to_retry.append(7)
        # download success
        app.chat_download_config[123].node.download_status[
            8
        ] = DownloadStatus.SuccessDownload
        app.chat_download_config[123].finish_task += 1
        # download success
        app.chat_download_config[123].node.download_status[
            10
        ] = DownloadStatus.SuccessDownload
        app.chat_download_config[123].finish_task += 1
        # not exist message
        app.chat_download_config[123].node.download_status[
            13
        ] = DownloadStatus.SuccessDownload
        app.config["chat"] = [{"chat_id": 123, "last_read_message_id": 5}]

        app.update_config(False)

        self.assertEqual(
            app.chat_download_config[123].last_read_message_id + 1,
            app.config["chat"][0]["last_read_message_id"],
        )
        self.assertEqual(
            [6, 7],
            app.app_data["chat"][0]["ids_to_retry"],
        )

    def test_upload_drive_false_values_are_applied(self):
        app = Application("", "")
        app.cloud_drive_config.enable_upload_file = True
        app.cloud_drive_config.before_upload_file_zip = True
        app.cloud_drive_config.after_upload_file_delete = True

        app.assign_config(
            {
                "api_id": 123,
                "api_hash": "hash",
                "media_types": [],
                "file_formats": {},
                "upload_drive": {
                    "enable_upload_file": False,
                    "before_upload_file_zip": False,
                    "after_upload_file_delete": False,
                },
            }
        )

        self.assertEqual(app.cloud_drive_config.enable_upload_file, False)
        self.assertEqual(app.cloud_drive_config.before_upload_file_zip, False)
        self.assertEqual(app.cloud_drive_config.after_upload_file_delete, False)

    def test_upload_file_uses_executor_for_aligo(self):
        app = Application("", "")
        app.cloud_drive_config.enable_upload_file = True
        app.cloud_drive_config.upload_adapter = "aligo"
        app.loop.run_in_executor = mock.AsyncMock(return_value=True)

        result = app.loop.run_until_complete(app.upload_file("demo.mp4"))

        self.assertEqual(result, True)
        app.loop.run_in_executor.assert_awaited_once_with(
            app.executor,
            CloudDrive.aligo_upload_file,
            app.cloud_drive_config,
            app.save_path,
            "demo.mp4",
        )

    @mock.patch("__main__.__builtins__.open", new_callable=mock.mock_open)
    @mock.patch("module.app._yaml.dump")
    def test_update_config(self, mock_dump, mock_open):
        app = Application("", "")
        app.config_file = "config_test.yaml"
        app.app_data_file = "data_test.yaml"
        app.config["chat"] = [{"chat_id": 123, "last_read_message_id": 0}]
        app.update_config()
        mock_open.assert_any_call("config_test.yaml", "w", encoding="utf-8")
        mock_open.assert_any_call("data_test.yaml", "w", encoding="utf-8")
        self.assertEqual(mock_dump.call_count, 2)
