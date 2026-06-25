"""Downloads media from telegram."""
import asyncio
import logging
import os
import shutil
import time
from typing import List, Optional, Tuple, Union

import pyrogram
from loguru import logger
from pyrogram.types import Audio, Document, Photo, Video, VideoNote, Voice
from rich.logging import RichHandler

from module.app import Application, ChatDownloadConfig, DownloadStatus, TaskNode
from module.bot import start_download_bot, stop_download_bot
from module.download_stat import update_download_status
from module.get_chat_history_v2 import get_chat_history_v2
from module.language import _t
from module.pyrogram_extension import (
    HookClient,
    fetch_message,
    get_extension,
    record_download_status,
    report_bot_download_status,
    set_max_concurrent_transmissions,
    set_meta_data,
    update_cloud_upload_stat,
    upload_telegram_chat,
)
from module.web import init_web
from utils.format import truncate_filename, validate_title
from utils.log import LogFilter
from utils.meta import print_meta
from utils.meta_data import MetaData

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler()],
)

CONFIG_NAME = "config.yaml"
DATA_FILE_NAME = "data.yaml"
APPLICATION_NAME = "media_downloader"
app = Application(CONFIG_NAME, DATA_FILE_NAME, APPLICATION_NAME)


class DownloadSizeMismatch(Exception):
    """Raised when downloaded file size doesn't match expected media size."""


async def _download_media_with_timeout(
    client: pyrogram.client.Client,
    message: pyrogram.types.Message,
    file_name: str,
    message_id: int,
    ui_file_name: str,
    task_start_time: float,
    node: TaskNode,
) -> Optional[str]:
    """Download media and optionally cap how long a single task can run."""

    download_coro = client.download_media(
        message,
        file_name=file_name,
        progress=update_download_status,
        progress_args=(
            message_id,
            ui_file_name,
            task_start_time,
            node,
            client,
        ),
    )

    if app.download_media_timeout <= 0:
        return await download_coro

    return await asyncio.wait_for(download_coro, timeout=app.download_media_timeout)


queue: asyncio.Queue = asyncio.Queue()
RETRY_TIME_OUT = 3

logging.getLogger("pyrogram.session.session").addFilter(LogFilter())
logging.getLogger("pyrogram.client").addFilter(LogFilter())

logging.getLogger("pyrogram").setLevel(logging.WARNING)


def _check_download_finish(media_size: int, download_path: str, ui_file_name: str):
    """Check download task if finish

    Parameters
    ----------
    media_size: int
        The size of the downloaded resource
    download_path: str
        Resource download hold path
    ui_file_name: str
        Really show file name

    """
    download_size = os.path.getsize(download_path)
    if media_size == download_size:
        logger.success(f"{_t('Successfully downloaded')} - {ui_file_name}")
    else:
        logger.warning(
            f"{_t('Media downloaded with wrong size')}: "
            f"{download_size}, {_t('actual')}: "
            f"{media_size}, {_t('file name')}: {ui_file_name}"
        )
        os.remove(download_path)
        raise DownloadSizeMismatch()


def _move_to_download_path(temp_download_path: str, download_path: str):
    """Move file to download path

    Parameters
    ----------
    temp_download_path: str
        Temporary download path

    download_path: str
        Download path

    """

    directory, _ = os.path.split(download_path)
    os.makedirs(directory, exist_ok=True)
    shutil.move(temp_download_path, download_path)


def _check_timeout(retry: int, _: int):
    """Check if message download timeout, then add message id into failed_ids

    Parameters
    ----------
    retry: int
        Retry download message times

    message_id: int
        Try to download message 's id

    """
    if retry == 2:
        return True
    return False


def _format_size(file_size: int) -> str:
    """Format file size for concise log output."""

    size = float(file_size or 0)
    units = ("B", "KB", "MB", "GB", "TB")
    unit_index = 0
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1
    if unit_index == 0:
        return f"{int(size)} {units[unit_index]}"
    return f"{size:.1f} {units[unit_index]}"


def _format_exception_reason(error: Exception) -> str:
    """Format exception details into a short one-line reason."""

    text = str(error).strip()
    error_name = type(error).__name__
    if not text or text == error_name:
        return error_name
    return f"{error_name}: {text}"


def _get_message_log_meta(
    message: pyrogram.types.Message,
) -> Tuple[str, str, int]:
    """Extract a stable media summary for task logging."""

    media_type = "message"
    display_name = f"message_{message.id}"
    file_size = 0

    for current_type in (
        "audio",
        "document",
        "photo",
        "video",
        "voice",
        "animation",
        "video_note",
    ):
        media = getattr(message, current_type, None)
        if media is None:
            continue
        media_type = current_type
        raw_name = getattr(media, "file_name", None) or f"{current_type}_{message.id}"
        display_name = os.path.basename(raw_name)
        file_size = getattr(media, "file_size", 0) or 0
        break

    if app.hide_file_name:
        _, ext = os.path.splitext(display_name)
        display_name = f"****{ext}" if ext else "****"

    return media_type, display_name, file_size


def _get_display_file_name(
    message: pyrogram.types.Message,
    file_name: Optional[str],
) -> str:
    """Normalize the file name displayed in queue/start/finish logs."""

    media_type, fallback_name, _ = _get_message_log_meta(message)
    if not file_name:
        return fallback_name

    base_name = os.path.basename(file_name)
    if app.hide_file_name:
        _, ext = os.path.splitext(base_name)
        return f"****{ext}" if ext else "****"
    if base_name:
        return base_name
    return fallback_name or f"{media_type}_{message.id}"


def _log_download_task_event(
    event: str,
    message: pyrogram.types.Message,
    node: TaskNode,
    file_name: Optional[str] = None,
    file_size: Optional[int] = None,
    download_status: Optional[DownloadStatus] = None,
    elapsed_seconds: Optional[float] = None,
    detail: Optional[str] = None,
):
    """Write a concise task lifecycle log line."""

    media_type, default_display_name, default_file_size = _get_message_log_meta(message)
    display_name = _get_display_file_name(message, file_name) or default_display_name
    display_size = file_size if file_size is not None else default_file_size
    attempt = node.failed_download_retry_count.get(message.id, 0) + 1

    parts = [
        f"{event} Message[{message.id}]",
        f"chat={node.chat_id}",
        f"type={media_type}",
        f"size={_format_size(display_size)}",
        f"file={display_name}",
        f"attempt={attempt}",
    ]

    if download_status is not None:
        status_name = {
            DownloadStatus.SuccessDownload: "success",
            DownloadStatus.FailedDownload: "failed",
            DownloadStatus.SkipDownload: "skip",
            DownloadStatus.Downloading: "downloading",
        }.get(download_status, str(download_status))
        parts.append(f"status={status_name}")

    if elapsed_seconds is not None:
        parts.append(f"elapsed={elapsed_seconds:.1f}s")

    if detail:
        parts.append(f"reason={detail}")

    text = " ".join(parts)
    if download_status is DownloadStatus.FailedDownload:
        logger.warning(text)
    else:
        logger.info(text)


def _can_download(_type: str, file_formats: dict, file_format: Optional[str]) -> bool:
    """
    Check if the given file format can be downloaded.

    Parameters
    ----------
    _type: str
        Type of media object.
    file_formats: dict
        Dictionary containing the list of file_formats
        to be downloaded for `audio`, `document` & `video`
        media types
    file_format: str
        Format of the current file to be downloaded.

    Returns
    -------
    bool
        True if the file format can be downloaded else False.
    """
    if _type in ["audio", "document", "video"]:
        allowed_formats: list = file_formats[_type]
        if file_format not in allowed_formats and allowed_formats and allowed_formats[0] != "all":
            return False
    return True


def _is_exist(file_path: str) -> bool:
    """
    Check if a file exists and it is not a directory.

    Parameters
    ----------
    file_path: str
        Absolute path of the file to be checked.

    Returns
    -------
    bool
        True if the file exists else False.
    """
    return not os.path.isdir(file_path) and os.path.exists(file_path)


# pylint: disable = R0912


async def _get_media_meta(
    chat_id: Union[int, str],
    message: pyrogram.types.Message,
    media_obj: Union[Audio, Document, Photo, Video, VideoNote, Voice],
    _type: str,
) -> Tuple[str, str, Optional[str]]:
    """Extract file name and file id from media object.

    Parameters
    ----------
    media_obj: Union[Audio, Document, Photo, Video, VideoNote, Voice]
        Media object to be extracted.
    _type: str
        Type of media object.

    Returns
    -------
    Tuple[str, str, Optional[str]]
        file_name, file_format
    """
    if _type in ["audio", "document", "video"]:
        # pylint: disable = C0301
        file_format: Optional[str] = media_obj.mime_type.split("/")[-1]  # type: ignore
    else:
        file_format = None

    file_name = None
    temp_file_name = None
    dirname = validate_title(f"{chat_id}")
    if message.chat and message.chat.title:
        dirname = validate_title(f"{message.chat.title}")

    if message.date:
        datetime_dir_name = message.date.strftime(app.date_format)
    else:
        datetime_dir_name = "0"

    if _type in ["voice", "video_note"]:
        # pylint: disable = C0209
        file_format = media_obj.mime_type.split("/")[-1]  # type: ignore
        file_save_path = app.get_file_save_path(_type, dirname, datetime_dir_name)
        file_name = "{} - {}_{}.{}".format(
            message.id,
            _type,
            media_obj.date.isoformat(),  # type: ignore
            file_format,
        )
        file_name = validate_title(file_name)
        temp_file_name = os.path.join(app.temp_save_path, dirname, file_name)

        file_name = os.path.join(file_save_path, file_name)
    else:
        file_name = getattr(media_obj, "file_name", None)
        caption = getattr(message, "caption", None)

        file_name_suffix = ".unknown"
        if not file_name:
            file_name_suffix = get_extension(
                media_obj.file_id, getattr(media_obj, "mime_type", "")
            )
        else:
            # file_name = file_name.split(".")[0]
            _, file_name_without_suffix = os.path.split(os.path.normpath(file_name))
            file_name, file_name_suffix = os.path.splitext(file_name_without_suffix)
            if not file_name_suffix:
                file_name_suffix = get_extension(
                    media_obj.file_id, getattr(media_obj, "mime_type", "")
                )

        if caption:
            caption = validate_title(caption)
            app.set_caption_name(chat_id, message.media_group_id, caption)
            app.set_caption_entities(
                chat_id, message.media_group_id, message.caption_entities
            )
        else:
            caption = app.get_caption_name(chat_id, message.media_group_id)

        if not file_name and message.photo:
            file_name = f"{message.photo.file_unique_id}"

        gen_file_name = (
            app.get_file_name(message.id, file_name, caption) + file_name_suffix
        )

        file_save_path = app.get_file_save_path(_type, dirname, datetime_dir_name)

        temp_file_name = os.path.join(app.temp_save_path, dirname, gen_file_name)

        file_name = os.path.join(file_save_path, gen_file_name)
    return truncate_filename(file_name), truncate_filename(temp_file_name), file_format


async def add_download_task(
    message: pyrogram.types.Message,
    node: TaskNode,
    is_retry: bool = False,
):
    """Add Download task"""
    if message.empty:
        return False
    node.download_result_detail.pop(message.id, None)
    node.download_status[message.id] = DownloadStatus.Downloading
    await queue.put((message, node))
    if not is_retry:
        node.total_task += 1
    _log_download_task_event("Queued", message, node)
    return True


async def save_msg_to_file(
    app, chat_id: Union[int, str], message: pyrogram.types.Message
):
    """Write message text into file"""
    dirname = validate_title(
        message.chat.title if message.chat and message.chat.title else str(chat_id)
    )
    datetime_dir_name = message.date.strftime(app.date_format) if message.date else "0"

    file_save_path = app.get_file_save_path("msg", dirname, datetime_dir_name)
    file_name = os.path.join(
        app.temp_save_path,
        file_save_path,
        f"{app.get_file_name(message.id, None, None)}.txt",
    )

    os.makedirs(os.path.dirname(file_name), exist_ok=True)

    if _is_exist(file_name):
        return DownloadStatus.SkipDownload, None

    with open(file_name, "w", encoding="utf-8") as f:
        f.write(message.text or "")

    return DownloadStatus.SuccessDownload, file_name


async def download_task(
    client: pyrogram.Client, message: pyrogram.types.Message, node: TaskNode
):
    """Download media and return its final local status."""

    download_status, file_name = await download_media(
        client, message, app.media_types, app.file_formats, node
    )

    if app.enable_download_txt and message.text and not message.media:
        download_status, file_name = await save_msg_to_file(app, node.chat_id, message)

    try:
        file_size = os.path.getsize(file_name) if file_name else 0
    except FileNotFoundError:
        file_size = 0

    return download_status, file_name, file_size


async def finalize_download_task(
    client: pyrogram.Client,
    message: pyrogram.types.Message,
    node: TaskNode,
    download_status: DownloadStatus,
    file_name: Optional[str],
    file_size: int = 0,
    elapsed_seconds: float = 0,
):
    """Persist task state and run post-download hooks."""

    result_detail = node.download_result_detail.pop(message.id, None)
    if not node.bot:
        app.set_download_id(node, message.id, download_status)
    node.download_status[message.id] = download_status

    try:
        await upload_telegram_chat(
            client,
            node.upload_user if node.upload_user else client,
            app,
            node,
            message,
            download_status,
            file_name,
        )

        # rclone upload
        if (
            not node.upload_telegram_chat_id
            and download_status is DownloadStatus.SuccessDownload
            and file_name
        ):
            ui_file_name = file_name
            if app.hide_file_name:
                ui_file_name = f"****{os.path.splitext(file_name)[-1]}"
            if await app.upload_file(
                file_name, update_cloud_upload_stat, (node, message.id, ui_file_name)
            ):
                node.upload_success_count += 1
    except Exception as e:
        logger.exception(f"Message[{message.id}]: post-download hook failed: {e}")

    await report_bot_download_status(
        node.bot,
        node,
        download_status,
        file_size,
    )
    _log_download_task_event(
        "Finished",
        message,
        node,
        file_name=file_name,
        file_size=file_size,
        download_status=download_status,
        elapsed_seconds=elapsed_seconds,
        detail=result_detail,
    )
    node.failed_download_retry_count.pop(message.id, None)


def _should_retry_failed_download(
    node: TaskNode,
    message_id: int,
    download_status: DownloadStatus,
) -> bool:
    """Whether a failed task should be retried again in this run."""

    if download_status is not DownloadStatus.FailedDownload:
        return False

    if app.failed_download_retry_count <= 0:
        return False

    current_retry = node.failed_download_retry_count.get(message_id, 0)
    return current_retry < app.failed_download_retry_count


async def _retry_failed_download_later(
    message: pyrogram.types.Message,
    node: TaskNode,
    retry_index: int,
):
    """Requeue a failed task after a short cooldown."""

    wait_seconds = max(app.failed_download_retry_interval, 0)
    logger.warning(
        f"Message[{message.id}]: download failed, scheduling retry "
        f"{retry_index}/{app.failed_download_retry_count} in {wait_seconds} seconds."
    )
    await asyncio.sleep(wait_seconds)

    if node.is_stop_transmission:
        if node.chat_id in app.chat_download_config:
            app.chat_download_config[node.chat_id].finish_task += 1
        return

    if app.is_running:
        await add_download_task(message, node, is_retry=True)


def _schedule_failed_download_retry(
    message: pyrogram.types.Message,
    node: TaskNode,
    download_status: DownloadStatus,
) -> bool:
    """Schedule a same-run retry for a failed download."""

    if not _should_retry_failed_download(node, message.id, download_status):
        return False

    retry_index = node.failed_download_retry_count.get(message.id, 0) + 1
    node.failed_download_retry_count[message.id] = retry_index
    app.loop.create_task(_retry_failed_download_later(message, node, retry_index))
    return True


# pylint: disable = R0915,R0914


@record_download_status
async def download_media(
    client: pyrogram.client.Client,
    message: pyrogram.types.Message,
    media_types: List[str],
    file_formats: dict,
    node: TaskNode,
):
    """
    Download media from Telegram.

    Each of the files to download are retried 3 times with a
    delay of 5 seconds each.

    Parameters
    ----------
    client: pyrogram.client.Client
        Client to interact with Telegram APIs.
    message: pyrogram.types.Message
        Message object retrieved from telegram.
    media_types: list
        List of strings of media types to be downloaded.
        Ex : `["audio", "photo"]`
        Supported formats:
            * audio
            * document
            * photo
            * video
            * voice
    file_formats: dict
        Dictionary containing the list of file_formats
        to be downloaded for `audio`, `document` & `video`
        media types.

    Returns
    -------
    int
        Current message id.
    """

    # pylint: disable = R0912

    file_name: str = ""
    ui_file_name: str = ""
    task_start_time: float = time.time()
    media_size = 0
    _media = None
    message = await fetch_message(client, message)
    node.download_result_detail.pop(message.id, None)
    try:
        for _type in media_types:
            _media = getattr(message, _type, None)
            if _media is None:
                continue
            file_name, temp_file_name, file_format = await _get_media_meta(
                node.chat_id, message, _media, _type
            )
            media_size = getattr(_media, "file_size", 0)

            ui_file_name = file_name
            if app.hide_file_name:
                ui_file_name = f"****{os.path.splitext(file_name)[-1]}"

            if _can_download(_type, file_formats, file_format):
                if _is_exist(file_name):
                    file_size = os.path.getsize(file_name)
                    if file_size == media_size:
                        node.download_result_detail[message.id] = "already exists"
                        logger.info(
                            f"id={message.id} {ui_file_name} "
                            f"{_t('already download,download skipped')}.\n"
                        )

                        return DownloadStatus.SkipDownload, None
            else:
                node.download_result_detail[message.id] = "filtered by file_formats"
                return DownloadStatus.SkipDownload, None

            break
    except Exception as e:
        node.download_result_detail[message.id] = _format_exception_reason(e)
        logger.error(
            f"Message[{message.id}]: "
            f"{_t('could not be downloaded due to following exception')}:\n[{e}].",
            exc_info=True,
        )
        return DownloadStatus.FailedDownload, None
    if _media is None:
        node.download_result_detail[message.id] = "no matched media"
        return DownloadStatus.SkipDownload, None

    failure_reason = "download failed"
    message_id = message.id

    for retry in range(3):
        try:
            temp_download_path = await _download_media_with_timeout(
                client,
                message,
                temp_file_name,
                message_id,
                ui_file_name,
                task_start_time,
                node,
            )

            if temp_download_path and isinstance(temp_download_path, str):
                _check_download_finish(media_size, temp_download_path, ui_file_name)
                await asyncio.sleep(0.5)
                _move_to_download_path(temp_download_path, file_name)
                # TODO: if not exist file size or media
                node.download_result_detail.pop(message.id, None)
                return DownloadStatus.SuccessDownload, file_name
        except DownloadSizeMismatch:
            failure_reason = "downloaded file size mismatch"
            logger.warning(
                f"Message[{message.id}]: downloaded file size mismatch, retrying..."
            )
            await asyncio.sleep(RETRY_TIME_OUT)
            if _check_timeout(retry, message.id):
                logger.error(
                    f"Message[{message.id}]: size mismatch after 3 retries, download skipped."
                )
                break
        except pyrogram.errors.exceptions.bad_request_400.BadRequest:
            failure_reason = "file reference expired"
            logger.warning(
                f"Message[{message.id}]: {_t('file reference expired, refetching')}..."
            )
            await asyncio.sleep(RETRY_TIME_OUT)
            message = await fetch_message(client, message)
            if _check_timeout(retry, message.id):
                # pylint: disable = C0301
                logger.error(
                    f"Message[{message.id}]: "
                    f"{_t('file reference expired for 3 retries, download skipped.')}"
                )
                break
        except pyrogram.errors.exceptions.flood_420.FloodWait as wait_err:
            failure_reason = f"FloodWait: {wait_err.value}s"
            await asyncio.sleep(wait_err.value)
            logger.warning("Message[{}]: FlowWait {}", message.id, wait_err.value)
            if _check_timeout(retry, message.id):
                break
        except (TimeoutError, TypeError, ConnectionError, OSError) as e:
            failure_reason = _format_exception_reason(e)
            # pylint: disable = C0301
            logger.warning(
                f"{_t('Timeout Error occurred when downloading Message')}[{message.id}], "
                f"{_t('retrying after')} {RETRY_TIME_OUT} {_t('seconds')}"
            )
            await asyncio.sleep(RETRY_TIME_OUT)
            if _check_timeout(retry, message.id):
                logger.error(
                    f"Message[{message.id}]: {_t('Timing out after 3 reties, download skipped.')}"
                )
                break
        except Exception as e:
            failure_reason = _format_exception_reason(e)
            # pylint: disable = C0301
            logger.error(
                f"Message[{message.id}]: "
                f"{_t('could not be downloaded due to following exception')}:\n[{e}].",
                exc_info=True,
            )
            break

    node.download_result_detail[message.id] = failure_reason
    return DownloadStatus.FailedDownload, None


def _load_config():
    """Load config"""
    app.load_config()


def _check_config() -> bool:
    """Check config"""
    print_meta(logger)
    try:
        _load_config()
        logger.add(
            os.path.join(app.log_file_path, "tdl.log"),
            rotation="10 MB",
            retention="10 days",
            level=app.log_level,
        )
    except Exception as e:
        logger.exception(f"load config error: {e}")
        return False

    return True


async def worker(client: pyrogram.client.Client):
    """Work for download task"""
    while app.is_running:
        message = None
        node = None
        started_at = None
        try:
            item = await queue.get()
            message = item[0]
            node: TaskNode = item[1]

            if node.is_stop_transmission:
                if node.chat_id in app.chat_download_config:
                    app.chat_download_config[node.chat_id].finish_task += 1
                continue

            _log_download_task_event("Starting", message, node)
            started_at = time.time()
            download_status = DownloadStatus.FailedDownload
            file_name = None
            file_size = 0
            if node.client:
                download_status, file_name, file_size = await download_task(
                    node.client, message, node
                )
            else:
                download_status, file_name, file_size = await download_task(
                    client, message, node
                )

            if _schedule_failed_download_retry(message, node, download_status):
                continue

            await finalize_download_task(
                client,
                message,
                node,
                download_status,
                file_name,
                file_size,
                time.time() - started_at,
            )
        except Exception as e:
            logger.exception(f"{e}")
            if node and message:
                try:
                    if _schedule_failed_download_retry(
                        message, node, DownloadStatus.FailedDownload
                    ):
                        continue
                    node.download_result_detail[message.id] = _format_exception_reason(e)
                    node.download_status[message.id] = DownloadStatus.FailedDownload
                    await finalize_download_task(
                        client,
                        message,
                        node,
                        DownloadStatus.FailedDownload,
                        None,
                        0,
                        time.time() - started_at if started_at else 0,
                    )
                except Exception:
                    pass


async def download_chat_task(
    client: pyrogram.Client,
    chat_download_config: ChatDownloadConfig,
    node: TaskNode,
):
    """Download all task"""
    messages_iter = get_chat_history_v2(
        client,
        node.chat_id,
        limit=node.limit,
        max_id=node.end_offset_id,
        offset_id=chat_download_config.last_read_message_id,
        reverse=True,
    )

    chat_download_config.node = node

    if chat_download_config.ids_to_retry:
        logger.info(f"{_t('Downloading files failed during last run')}...")
        skipped_messages: list = await client.get_messages(  # type: ignore
            chat_id=node.chat_id, message_ids=chat_download_config.ids_to_retry
        )

        for message in skipped_messages:
            if message is None or message.empty:
                continue
            await add_download_task(message, node)

    async for message in messages_iter:  # type: ignore
        meta_data = MetaData()

        caption = message.caption
        if caption:
            caption = validate_title(caption)
            app.set_caption_name(node.chat_id, message.media_group_id, caption)
            app.set_caption_entities(
                node.chat_id, message.media_group_id, message.caption_entities
            )
        else:
            caption = app.get_caption_name(node.chat_id, message.media_group_id)
        set_meta_data(meta_data, message, caption)

        if app.need_skip_message(chat_download_config, message.id):
            continue

        if app.exec_filter(chat_download_config, meta_data):
            await add_download_task(message, node)
        else:
            node.download_status[message.id] = DownloadStatus.SkipDownload
            if message.media_group_id:
                await upload_telegram_chat(
                    client,
                    node.upload_user,
                    app,
                    node,
                    message,
                    DownloadStatus.SkipDownload,
                )

    chat_download_config.need_check = True
    chat_download_config.total_task = node.total_task
    node.is_running = True


async def download_all_chat(client: pyrogram.Client):
    """Download All chat"""
    for key, value in app.chat_download_config.items():
        value.node = TaskNode(chat_id=key)
        try:
            await download_chat_task(client, value, value.node)
        except Exception as e:
            logger.warning(f"Download {key} error: {e}")
        finally:
            value.need_check = True


async def run_until_all_task_finish():
    """Normal download"""
    max_wait = app.run_until_all_task_finish_timeout
    waited = 0
    while True:
        finish: bool = True
        for _, value in app.chat_download_config.items():
            if not value.need_check or value.total_task != value.finish_task:
                finish = False

        if (not app.bot_token and finish) or app.restart_program:
            break

        if max_wait > 0 and waited >= max_wait:
            logger.warning("Timeout waiting for tasks to finish, forcing exit.")
            break

        await asyncio.sleep(1)
        waited += 1


def _exec_loop():
    """Exec loop"""

    app.loop.run_until_complete(run_until_all_task_finish())


async def start_server(client: pyrogram.Client):
    """
    Start the server using the provided client.
    """
    await client.start()


async def stop_server(client: pyrogram.Client):
    """
    Stop the server using the provided client.
    """
    await client.stop()


def main():
    """Main function of the downloader."""
    tasks = []
    client = HookClient(
        "media_downloader",
        api_id=app.api_id,
        api_hash=app.api_hash,
        proxy=app.proxy,
        workdir=app.session_file_path,
        start_timeout=app.start_timeout,
        no_updates=True,
    )
    try:
        app.pre_run()
        init_web(app)

        set_max_concurrent_transmissions(client, app.max_concurrent_transmissions)

        app.loop.run_until_complete(start_server(client))
        logger.success(_t("Successfully started (Press Ctrl+C to stop)"))

        download_all_task = app.loop.create_task(download_all_chat(client))
        for _ in range(app.max_download_task):
            task = app.loop.create_task(worker(client))
            tasks.append(task)

        if app.bot_token:
            app.loop.run_until_complete(
                start_download_bot(app, client, add_download_task, download_chat_task)
            )
        _exec_loop()
    except KeyboardInterrupt:
        logger.info(_t("KeyboardInterrupt"))
    except Exception as e:
        logger.exception("{}", e)
    finally:
        app.is_running = False
        if app.bot_token:
            app.loop.run_until_complete(stop_download_bot())
        app.loop.run_until_complete(stop_server(client))
        for task in tasks:
            task.cancel()
        if tasks:
            app.loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        logger.info(_t("Stopped!"))
        # check_for_updates(app.proxy)
        logger.info(f"{_t('update config')}......")
        app.update_config()
        logger.success(
            f"{_t('Updated last read message_id to config file')},"
            f"{_t('total download')} {app.total_download_task}, "
            f"{_t('total upload file')} "
            f"{app.cloud_drive_config.total_upload_success_file_count}"
        )


if __name__ == "__main__":
    if _check_config():
        main()
