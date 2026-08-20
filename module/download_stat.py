"""Download Stat"""
import asyncio
import time
from enum import Enum
from threading import RLock
from typing import Dict

from pyrogram import Client

from module.app import TaskNode


class DownloadState(Enum):
    """Download state"""

    Downloading = 1
    StopDownload = 2


_download_result: dict = {}
_download_result_lock = RLock()
_total_download_speed: int = 0
_total_download_size: int = 0
_last_download_time: float = time.time()
_download_state: DownloadState = DownloadState.Downloading
MAX_COMPLETED_DOWNLOAD_RESULTS_PER_CHAT = 1_000


def set_download_task_status(
    chat_id: int,
    message_id: int,
    node: TaskNode,
    state: str,
    *,
    file_name: str = "",
    total_size: int = 0,
    attempt: int = 0,
    retry_count: int = 0,
    reason: str = "",
    is_active: bool = True,
):
    """Create or update a task lifecycle record, even before bytes arrive."""
    now = time.time()
    with _download_result_lock:
        messages = _download_result.setdefault(chat_id, {})
        result = messages.get(message_id)
        if result is None:
            result = {
                "down_byte": 0,
                "total_size": total_size,
                "file_name": file_name or f"message_{message_id}",
                "start_time": now,
                "end_time": now,
                "download_speed": 0,
                "each_second_total_download": 0,
                "task_id": node.task_id,
            }
            messages[message_id] = result

        if file_name:
            result["file_name"] = file_name
        if total_size:
            result["total_size"] = total_size
        result["task_id"] = node.task_id
        result["state"] = state
        result["attempt"] = attempt
        result["retry_count"] = retry_count
        result["reason"] = reason
        result["is_active"] = is_active
        result["updated_at"] = now
        if state in ("queued", "retrying"):
            result["down_byte"] = 0
            result["download_speed"] = 0
            result["each_second_total_download"] = 0
            result["start_time"] = now
            result["end_time"] = now
        if not is_active:
            result["finished_at"] = now


def mark_download_retrying(
    chat_id: int,
    message_id: int,
    node: TaskNode,
    retry_count: int,
    reason: str = "",
):
    """Expose a delayed retry instead of leaving it as a stale download."""
    set_download_task_status(
        chat_id,
        message_id,
        node,
        "retrying",
        attempt=retry_count + 1,
        retry_count=retry_count,
        reason=reason,
        is_active=True,
    )


def get_download_result() -> dict:
    """Return a snapshot of all download results for status consumers."""
    with _download_result_lock:
        return {
            chat_id: {
                message_id: result.copy() for message_id, result in messages.items()
            }
            for chat_id, messages in _download_result.items()
        }


def get_active_download_result(chat_id: int, task_id: int) -> Dict[int, dict]:
    """Return the active progress records belonging to a bot task."""
    with _download_result_lock:
        messages = _download_result.get(chat_id, {})
        return {
            message_id: result.copy()
            for message_id, result in messages.items()
            if result.get("is_active", result["down_byte"] != result["total_size"])
            and result["task_id"] == task_id
        }


def finish_download_status(
    chat_id: int,
    message_id: int,
    is_success: bool,
    *,
    state: str = "failed",
    reason: str = "",
    file_name: str = "",
    total_size: int = 0,
    node: TaskNode = None,
    attempt: int = 0,
    retry_count: int = 0,
):
    """Mark a download record inactive and keep a bounded completed history."""
    with _download_result_lock:
        messages = _download_result.setdefault(chat_id, {})
        if message_id not in messages:
            messages[message_id] = {
                "down_byte": 0,
                "total_size": total_size,
                "file_name": file_name or f"message_{message_id}",
                "start_time": time.time(),
                "end_time": time.time(),
                "download_speed": 0,
                "each_second_total_download": 0,
                "task_id": node.task_id if node else 0,
            }

        result = messages[message_id]
        if file_name:
            result["file_name"] = file_name
        if total_size:
            result["total_size"] = total_size
        if node:
            result["task_id"] = node.task_id
        if is_success:
            result["down_byte"] = result["total_size"]
        result["is_active"] = False
        result["state"] = state
        result["reason"] = reason
        result["attempt"] = attempt
        result["retry_count"] = retry_count
        result["finished_at"] = time.time()
        result["updated_at"] = result["finished_at"]

        completed = sorted(
            (
                (completed_id, completed_result["finished_at"])
                for completed_id, completed_result in messages.items()
                if not completed_result.get(
                    "is_active",
                    completed_result["down_byte"] != completed_result["total_size"],
                )
            ),
            key=lambda item: item[1],
        )
        overflow = len(completed) - MAX_COMPLETED_DOWNLOAD_RESULTS_PER_CHAT
        for completed_id, _ in completed[: max(overflow, 0)]:
            messages.pop(completed_id, None)

        if not messages:
            _download_result.pop(chat_id, None)


def get_total_download_speed() -> int:
    """get total download speed"""
    return _total_download_speed


def get_download_state() -> DownloadState:
    """get download state"""
    return _download_state


# pylint: disable = W0603
def set_download_state(state: DownloadState):
    """set download state"""
    global _download_state
    _download_state = state


async def update_download_status(
    down_byte: int,
    total_size: int,
    message_id: int,
    file_name: str,
    start_time: float,
    node: TaskNode,
    client: Client,
):
    """update_download_status"""
    cur_time = time.time()
    # pylint: disable = W0603
    global _total_download_speed
    global _total_download_size
    global _last_download_time

    if node.is_stop_transmission:
        client.stop_transmission()

    chat_id = node.chat_id

    while get_download_state() == DownloadState.StopDownload:
        if node.is_stop_transmission:
            client.stop_transmission()
        await asyncio.sleep(1)

    with _download_result_lock:
        if not _download_result.get(chat_id):
            _download_result[chat_id] = {}

        messages = _download_result[chat_id]
        result = messages.get(message_id)
        if result and result.get("is_active", True):
            last_download_byte = result["down_byte"]
            last_time = result["end_time"]
            download_speed = result["download_speed"]
            each_second_total_download = result["each_second_total_download"]
            end_time = result["end_time"]

            _total_download_size += down_byte - last_download_byte
            each_second_total_download += down_byte - last_download_byte

            if cur_time - last_time >= 1.0:
                download_speed = int(
                    each_second_total_download / (cur_time - last_time)
                )
                end_time = cur_time
                each_second_total_download = 0

            result["down_byte"] = down_byte
            result["end_time"] = end_time
            result["download_speed"] = max(download_speed, 0)
            result["each_second_total_download"] = each_second_total_download
            result["state"] = "downloading"
            result["updated_at"] = cur_time
        else:
            each_second_total_download = down_byte
            messages[message_id] = {
                "down_byte": down_byte,
                "total_size": total_size,
                "file_name": file_name,
                "start_time": start_time,
                "end_time": cur_time,
                "download_speed": (
                    down_byte / (cur_time - start_time) if cur_time > start_time else 0
                ),
                "each_second_total_download": each_second_total_download,
                "task_id": node.task_id,
                "is_active": True,
                "state": "downloading",
                "attempt": 1,
                "retry_count": 0,
                "reason": "",
                "updated_at": cur_time,
            }
            _total_download_size += down_byte

    elapsed = cur_time - _last_download_time
    if elapsed >= 1.0:
        # update speed
        _total_download_speed = int(_total_download_size / elapsed) if elapsed > 0 else 0
        _total_download_speed = max(_total_download_speed, 0)
        _total_download_size = 0
        _last_download_time = cur_time
