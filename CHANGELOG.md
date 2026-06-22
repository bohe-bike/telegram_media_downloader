# Changelog

## [2.2.7] - 2026-06-22

### Fixed

#### media_downloader.py

- **[CRITICAL]** Fixed file size comparison logic (`if file_size or file_size == media_size` → `if file_size == media_size`) that caused corrupted/incomplete files to be skipped instead of re-downloaded.
- **[HIGH]** Added custom `DownloadSizeMismatch` exception instead of misusing `BadRequest` for file size mismatches.
- **[HIGH]** Fixed retry loop not breaking when retries are exhausted in `BadRequest`, `FloodWait`, and `TypeError` handlers.
- **[HIGH]** Fixed worker function silently swallowing exceptions, which caused tasks to hang in `Downloading` state forever. Now properly updates task status on failure.
- **[HIGH]** Added timeout (1 hour) to `run_until_all_task_finish` to prevent infinite waiting.
- **[MEDIUM]** Filtered out `None` messages from `get_messages` for deleted message IDs to prevent crashes.
- **[MEDIUM]** Fixed cancelled asyncio tasks not being awaited, which could cause data corruption during shutdown.
- **[LOW]** Fixed `not file_format in allowed_formats` operator precedence and added empty list guard.
- **[LOW]** Added `FileNotFoundError` guard for `os.path.getsize` in `download_task`.

#### bot.py

- **[HIGH]** Fixed `update_config` writing to hardcoded file path `"d"` instead of `self.config_path`.
- **[HIGH]** Fixed `remove_task_node` using `.pop()` without default, causing `KeyError` on missing task IDs.
- **[HIGH]** Fixed missing `await` on `client.send_message()` in `download_from_link` error handler.
- **[HIGH]** Fixed `entity` variable used before assignment when `chat_id` is falsy in `download_from_bot` and `download_from_link`.
- **[HIGH]** Fixed inverted logic in `remove_replace_advertisement_filter` (appending instead of notifying) and empty `send_message()` call.
- **[HIGH]** Added missing `return None` after filter check failure in forward task creation.
- **[MEDIUM]** Added missing newline separator in error message formatting.
- **[LOW]** Replaced silent `except Exception: pass` with logging in bot startup.
- **[LOW]** Added `message.text` None guard in `set_language`.

#### pyrogram_extension.py

- **[HIGH]** Removed unsupported `caption` parameter from `send_video_note` / `reply_video_note` calls.
- **[HIGH]** Fixed `proc_cache_forward` always returning `CacheForward` instead of actual forward status.
- **[MEDIUM]** Added division-by-zero guards for download and upload progress calculations.
- **[MEDIUM]** Fixed `getattr(message, "from_user")` missing default value, causing `AttributeError` on channel messages.
- **[MEDIUM]** Fixed `convect_caption_entities` returning inconsistent types (`dict_values` vs tuple) and replaced `print` with `logger`.

#### web.py

- **[CRITICAL]** Changed hardcoded Flask `secret_key` from `"tdl"` to environment variable `TDL_SECRET_KEY` (falls back to random hex).
- **[CRITICAL]** Changed hardcoded AES key/IV to environment variables `TDL_AES_KEY` / `TDL_AES_IV` (falls back to original defaults for backward compatibility).
- **[HIGH]** Fixed `load_user` ignoring user ID, always returning the same user object.
- **[MEDIUM]** Fixed debug-mode Flask thread not running as daemon, preventing clean process exit.
- **[MEDIUM]** Replaced manual JSON string concatenation with `jsonify()` to prevent malformed JSON from special characters in filenames.
- **[MEDIUM]** Added division-by-zero guard in download progress calculation.

#### filter.py

- **[HIGH]** Added division-by-zero guard for `/` operator in filter expressions.
- **[HIGH]** Fixed `NoneObj` comparisons always returning `True` instead of `False` in all comparison operators (`>`, `<`, `>=`, `<=`, `==`, `!=`).
- **[MEDIUM]** Fixed `p[0] = 0` returning integer instead of `False` in `eq`/`ne` operators when type check fails.
- **[MEDIUM]** Fixed `check_type` using `is NoneObj` (class identity) instead of `isinstance(p, NoneObj)`.
- **[LOW]** Moved `bool` type check before `int` in `check_type` since `bool` is a subclass of `int` in Python.

#### app.py

- **[MEDIUM]** Added None guard for `caption` parameter in `is_match_advertisement` to prevent `TypeError`.
- **[MEDIUM]** Added try/except for config file write operations to handle IO errors gracefully.

#### download_stat.py

- **[MEDIUM]** Added division-by-zero guards for download speed calculations when `cur_time == start_time`.

#### format.py

- **[MEDIUM]** Fixed `replace_date_time` recursive calls not forwarding the `fmt` parameter.
- **[LOW]** Fixed malformed error message string in `format_byte`.
- **[LOW]** Added guard for negative `f_max` in `truncate_filename` when extension exceeds limit.

#### crypto.py

- **[MEDIUM]** Added PKCS#7 unpadding validation to prevent silent garbage data on corrupted input.
- **[LOW]** Fixed `encrypt()` return type from `bytes` to `str` to match docstring.

#### language.py

- **[LOW]** Fixed malformed pylint directive syntax.

### Changed

#### docker-compose.yaml

- Added commented environment variable examples for `TDL_SECRET_KEY`, `TDL_AES_KEY`, `TDL_AES_IV`.

### Documentation

- Updated `README.md` with Environment Variables section.
- Updated `README_CN.md` with Environment Variables section (Chinese).
