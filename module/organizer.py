"""Post-download media organizer.

The organizer builds a reviewable plan that moves downloaded audio/video files
into a synchronized library::

    <sync_root>/mp3/<author>/<file>
    <sync_root>/mp4/<author>/<file>
    <sync_root>/未整理/<file>          # author cannot be confirmed

Nothing is moved until the plan is executed. Execution always keeps a backup,
never overwrites an existing target and verifies the result before reporting
success.
"""
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import unicodedata
from typing import Dict, List, Optional, Tuple

try:
    from mutagen import File as MutagenFile
except Exception:  # pragma: no cover - mutagen is an optional runtime dependency
    MutagenFile = None

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {"mp3", "m4a", "flac", "aac", "mp4a"}
VIDEO_EXTENSIONS = {"mp4"}
# AAC is intentionally excluded: only move it, never rewrite its tags.
TAG_WRITABLE_EXTENSIONS = {"mp3", "m4a", "flac", "mp4a"}
DEFAULT_SOURCE_MARKERS = ["TG@Jingluoasmr", "ASMR圖書館", "ASMR图书馆"]
MAX_LEARN_FILES_PER_AUTHOR = 20
MAX_SCAN_FILES = 5000

_INVALID_AUTHORS = {
    "",
    "unknown",
    "va",
    "various",
    "various artists",
    "n/a",
    "none",
    "null",
}
_NUMBER_RE = re.compile(r"^\d+$")
_DATE_RE = re.compile(r"^\d{4}[-_.年/]?\d{1,2}([-_.月/]?\d{1,2})?日?$")
_URL_RE = re.compile(r"^(https?://|www\.)", re.I)
_EPISODE_RE = re.compile(r"^(ep|e|p|part|vol|第)\s*\d+", re.I)
_RECORD_RE = re.compile(r"^(record|video|video[_-]?\d+)$", re.I)
_AUTHOR_SPLIT_RE = re.compile(r"\s*(?:/|&|,|，|、|;|；|\bfeat\.?\b|\bft\.?\b)\s*", re.I)


class OrganizeConfig:
    """Configuration for the post-download organizer."""

    def __init__(self):
        self.enable = False
        self.sync_root = ""
        self.audio_dir = "mp3"
        self.video_dir = "mp4"
        self.pending_dir = "未整理"
        self.backup_dir = ".organize_backup"
        self.plan_file = os.path.join("log", "organize_plan.json")
        self.download_root = ""
        self.source_markers = list(DEFAULT_SOURCE_MARKERS)
        self.author_aliases: Dict[str, str] = {}
        self.write_audio_tags = True
        self.auto_learn_aliases = True
        self.scan_library_roots = True
        self.scan_download_root = True


def assign_organize_config(config: OrganizeConfig, raw: dict) -> OrganizeConfig:
    """Apply a raw ``organize`` config section to ``config``."""
    if not isinstance(raw, dict):
        return config

    config.enable = bool(raw.get("enable", config.enable))
    if raw.get("sync_root") is not None:
        config.sync_root = str(raw["sync_root"])
    if raw.get("audio_dir"):
        config.audio_dir = str(raw["audio_dir"])
    if raw.get("video_dir"):
        config.video_dir = str(raw["video_dir"])
    if raw.get("pending_dir"):
        config.pending_dir = str(raw["pending_dir"])
    if raw.get("backup_dir"):
        config.backup_dir = str(raw["backup_dir"])
    if raw.get("plan_file"):
        config.plan_file = str(raw["plan_file"])
    if raw.get("download_root") is not None:
        config.download_root = str(raw["download_root"])
    if isinstance(raw.get("source_markers"), list):
        config.source_markers = [str(item) for item in raw["source_markers"] if item]
    if isinstance(raw.get("author_aliases"), dict):
        config.author_aliases = {
            str(alias): str(canonical)
            for alias, canonical in raw["author_aliases"].items()
            if alias and canonical
        }
    config.write_audio_tags = bool(raw.get("write_audio_tags", config.write_audio_tags))
    config.auto_learn_aliases = bool(
        raw.get("auto_learn_aliases", config.auto_learn_aliases)
    )
    config.scan_library_roots = bool(
        raw.get("scan_library_roots", config.scan_library_roots)
    )
    config.scan_download_root = bool(
        raw.get("scan_download_root", config.scan_download_root)
    )
    return config


class _AuthorIndex:
    """Canonical author names and learned alias mappings."""

    def __init__(self):
        self.canonical: Dict[str, str] = {}
        self.aliases: Dict[str, str] = {}


_config = OrganizeConfig()
_plan_lock = threading.RLock()
_pending_sources: Dict[str, dict] = {}
_plan_entries: List[dict] = []
_plan_generated_at = 0.0
_plan_dirty = True


def configure(config: OrganizeConfig) -> None:
    """Bind the organizer to the application's organize config."""
    global _config, _plan_dirty
    _config = config or OrganizeConfig()
    with _plan_lock:
        _plan_dirty = True


def get_config() -> OrganizeConfig:
    """Return the currently bound organize config."""
    return _config


def reset() -> None:
    """Clear in-memory plan state (used by tests and manual refreshes)."""
    global _plan_entries, _plan_generated_at, _plan_dirty
    with _plan_lock:
        _pending_sources.clear()
        _plan_entries = []
        _plan_generated_at = 0.0
        _plan_dirty = True


def enqueue_downloaded_file(file_path: str, chat_title: str = "") -> None:
    """Remember a freshly downloaded file so it is included in the next plan."""
    if not file_path:
        return
    source = os.path.abspath(file_path)
    with _plan_lock:
        _pending_sources[source] = {
            "chat_title": chat_title,
            "added_at": time.time(),
        }
        _mark_dirty_locked()


def _mark_dirty_locked() -> None:
    global _plan_dirty
    _plan_dirty = True


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("　", " ")
    return re.sub(r"\s+", " ", text).strip()


def _strip_source_markers(value: str, markers: List[str]) -> str:
    value = _normalize(value)
    for marker in markers or []:
        if marker:
            value = re.sub(re.escape(marker), " ", value, flags=re.I)
    value = re.sub(r"tg@\S+", " ", value, flags=re.I)
    return re.sub(r"\s+", " ", value).strip(" -_·.|")


def clean_author(value: str, markers: List[str]) -> str:
    """Remove source markers and normalize an author candidate."""
    value = _strip_source_markers(value, markers)
    value = re.sub(r"[\[\]【】()（）]", " ", value)
    return re.sub(r"\s+", " ", value).strip(" -_·.|")


def _safe_path_component(value: str) -> str:
    """Make an author safe to use as a single directory name."""
    value = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", value or "")
    value = value.strip(" .")
    return value or "未知作者"


def _split_authors(value: str) -> List[str]:
    return [
        part.strip() for part in _AUTHOR_SPLIT_RE.split(value or "") if part.strip()
    ]


def _is_valid_author(value: str) -> bool:
    if not value:
        return False
    lowered = value.casefold()
    if lowered in _INVALID_AUTHORS:
        return False
    if _NUMBER_RE.match(value) or _DATE_RE.match(value):
        return False
    if _URL_RE.match(value) or _EPISODE_RE.match(value) or _RECORD_RE.match(value):
        return False
    return len(value) <= 80


def _file_extension(path: str) -> str:
    return os.path.splitext(path)[1].lower().lstrip(".")


def _file_kind(path: str) -> Optional[str]:
    extension = _file_extension(path)
    if extension in AUDIO_EXTENSIONS:
        return "audio"
    if extension in VIDEO_EXTENSIONS:
        return "video"
    return None


def _read_audio_tags(path: str) -> Dict[str, str]:
    """Read artist/albumartist/album tags without failing on broken files."""
    if MutagenFile is None:
        return {}
    try:
        audio = MutagenFile(path, easy=True)
    except Exception:
        return {}
    if audio is None:
        return {}

    tags: Dict[str, str] = {}
    for key in ("artist", "albumartist", "album"):
        try:
            values = audio.get(key)
        except Exception:
            values = None
        if not values:
            continue
        if isinstance(values, (list, tuple)):
            tags[key] = " / ".join(str(value) for value in values if value)
        else:
            tags[key] = str(values)
    return tags


def _audio_author_candidates(tags: Dict[str, str], markers: List[str]) -> List[str]:
    candidates: List[str] = []
    for key in ("artist", "albumartist", "album"):
        cleaned = clean_author(tags.get(key, ""), markers)
        for part in _split_authors(cleaned):
            part = clean_author(part, markers)
            if _is_valid_author(part):
                candidates.append(part)
    return candidates


def _build_author_index(config: OrganizeConfig) -> _AuthorIndex:
    index = _AuthorIndex()
    roots = [
        os.path.join(config.sync_root, config.audio_dir),
        os.path.join(config.sync_root, config.video_dir),
    ]

    for root in roots:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            if name.startswith("."):
                continue
            if os.path.isdir(os.path.join(root, name)):
                index.canonical.setdefault(_normalize(name).casefold(), name)

    for alias, canonical in (config.author_aliases or {}).items():
        alias_key = _normalize(alias).casefold()
        canonical_name = _normalize(canonical)
        if alias_key and canonical_name:
            index.aliases[alias_key] = canonical_name

    if config.auto_learn_aliases and MutagenFile is not None:
        _learn_aliases_from_directories(config, index)
    return index


def _learn_aliases_from_directories(
    config: OrganizeConfig, index: _AuthorIndex
) -> None:
    """Learn alias -> canonical pairs from already classified author folders."""
    roots = [
        os.path.join(config.sync_root, config.audio_dir),
        os.path.join(config.sync_root, config.video_dir),
    ]
    for root in roots:
        if not os.path.isdir(root):
            continue
        for canonical in list(index.canonical.values()):
            author_dir = os.path.join(root, canonical)
            if not os.path.isdir(author_dir):
                continue
            learned = 0
            for filename in sorted(os.listdir(author_dir)):
                if learned >= MAX_LEARN_FILES_PER_AUTHOR:
                    break
                path = os.path.join(author_dir, filename)
                if not os.path.isfile(path) or _file_kind(path) != "audio":
                    continue
                learned += 1
                tags = _read_audio_tags(path)
                for candidate in _audio_author_candidates(tags, config.source_markers):
                    if candidate.casefold() == canonical.casefold():
                        continue
                    if not _is_valid_author(candidate):
                        continue
                    index.aliases.setdefault(candidate.casefold(), canonical)


def _resolve_audio_author(
    tags: Dict[str, str], index: _AuthorIndex, markers: List[str]
) -> Tuple[str, float, str]:
    candidates = _audio_author_candidates(tags, markers)
    for candidate in candidates:
        key = candidate.casefold()
        if key in index.canonical:
            return index.canonical[key], 1.0, "匹配已有作者目录"
        if key in index.aliases:
            return index.aliases[key], 0.95, "命中作者别名"
    if candidates:
        return candidates[0], 0.8, "使用标签作者"
    return "", 0.0, "标签中没有可用作者"


def _match_existing_author(stem: str, index: _AuthorIndex) -> Optional[str]:
    for canonical in sorted(index.canonical.values(), key=len, reverse=True):
        name = re.escape(canonical)
        if re.match(name + r"[\s\-–—_]", stem, re.I):
            return canonical
        if re.search(r"[\[【]\s*" + name + r"\s*[\]】]", stem, re.I):
            return canonical
    return None


def _resolve_video_author(
    path: str, index: _AuthorIndex, markers: List[str]
) -> Tuple[str, float, str]:
    # Keep brackets while parsing: they carry the author marker structure.
    stem = _strip_source_markers(os.path.splitext(os.path.basename(path))[0], markers)
    matched = _match_existing_author(stem, index)
    if matched:
        return matched, 1.0, "匹配已有作者目录"

    bracket = re.match(r"^[\[【]\s*([^\]】]{1,60})\s*[\]】]\s*\S", stem)
    if bracket:
        candidate = clean_author(bracket.group(1), markers)
        if _is_valid_author(candidate):
            return candidate, 0.8, "文件名作者标记"

    dashed = re.match(r"^(.{1,60}?)\s*[-–—]\s+\S", stem)
    if dashed:
        candidate = clean_author(dashed.group(1), markers)
        if _is_valid_author(candidate):
            return candidate, 0.7, "文件名作者前缀"

    return "", 0.0, "文件名中无法确认作者"


def _target_path(config: OrganizeConfig, kind: str, author: str, filename: str) -> str:
    if not author:
        return os.path.join(config.sync_root, config.pending_dir, filename)

    sub_dir = config.audio_dir if kind == "audio" else config.video_dir
    target = os.path.join(
        config.sync_root, sub_dir, _safe_path_component(author), filename
    )
    # Never let a crafted tag escape the configured sync root.
    sync_root = os.path.abspath(config.sync_root)
    if os.path.commonpath([sync_root, os.path.abspath(target)]) != sync_root:
        return os.path.join(config.sync_root, config.pending_dir, filename)
    return target


def _same_file(left: str, right: str) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _entry_id(source: str, target: str) -> str:
    return hashlib.sha1(f"{source}|{target}".encode("utf-8")).hexdigest()[:12]


def _channel_folder(config: OrganizeConfig, path: str, chat_title: str = "") -> str:
    """Return the channel folder that owns a downloaded file."""
    download_root = config.download_root
    if download_root:
        try:
            relative = os.path.relpath(path, os.path.abspath(download_root))
        except ValueError:
            relative = ""
        if relative and not relative.startswith(".."):
            parts = relative.split(os.sep)
            # downloads/<频道>/<日期>/... -> 频道
            if len(parts) > 1:
                return parts[0]
    return chat_title or ""


def _apply_author(
    config: OrganizeConfig,
    entry: dict,
    author: str,
    confidence: float,
    reason: str,
) -> None:
    """Apply an author decision and recompute target-dependent fields."""
    entry["author"] = author
    entry["confidence"] = round(confidence, 2)
    entry["target"] = _target_path(config, entry["kind"], author, entry["filename"])
    entry["conflict"] = os.path.exists(entry["target"]) and not _same_file(
        entry["source"], entry["target"]
    )
    if entry["conflict"]:
        entry["action"] = "skip"
        entry["reason"] = f"{reason}；目标文件已存在，默认不覆盖"
    else:
        entry["action"] = "move"
        entry["reason"] = reason
    entry["id"] = _entry_id(entry["source"], entry["target"])


def _shared_author_prefix(stems: List[str]) -> str:
    """Find an author prefix shared by several files in one channel folder.

    Only a prefix that ends on a separator boundary is accepted; prefixes such
    as ``Alice01`` / ``Alice02`` are not treated as a reliable author signal.
    """
    if len(stems) < 2:
        return ""
    common = os.path.commonprefix(stems).strip()
    if len(common) < 2:
        return ""
    boundary = max(common.rfind(char) for char in " -–—_·.|")
    if boundary < 2:
        return ""
    prefix = clean_author(common[:boundary], [])
    if _is_valid_author(prefix):
        return prefix
    return ""


def _build_entry(
    config: OrganizeConfig,
    index: _AuthorIndex,
    source: str,
    meta: dict,
) -> Optional[dict]:
    kind = _file_kind(source)
    if kind is None:
        return None

    filename = os.path.basename(source)
    tag_changes: Dict[str, str] = {}
    if kind == "audio":
        tags = _read_audio_tags(source)
        author, confidence, reason = _resolve_audio_author(
            tags, index, config.source_markers
        )
        if author and _file_extension(source) in TAG_WRITABLE_EXTENSIONS:
            tag_changes = {
                "artist": author,
                "albumartist": author,
                "album": author,
            }
    else:
        author, confidence, reason = _resolve_video_author(
            source, index, config.source_markers
        )

    entry = {
        "id": "",
        "source": source,
        "filename": filename,
        "kind": kind,
        "author": "",
        "confidence": 0,
        "target": "",
        "action": "move",
        "status": "planned",
        "reason": reason,
        "conflict": False,
        "tag_changes": tag_changes,
        "chat_title": meta.get("chat_title", ""),
        "channel_folder": _channel_folder(config, source, meta.get("chat_title", "")),
        "backup": "",
        "message": "",
    }
    _apply_author(config, entry, author, confidence, reason)
    return entry


def _build_group_entries(
    config: OrganizeConfig,
    index: _AuthorIndex,
    items: List[Tuple[str, dict]],
) -> List[dict]:
    """Apply organizing rules inside a single channel folder."""
    entries: List[dict] = []
    for source, meta in items:
        entry = _build_entry(config, index, source, meta)
        if entry is not None:
            entries.append(entry)

    unresolved = [
        entry for entry in entries if entry["kind"] == "video" and not entry["author"]
    ]
    if not unresolved:
        return entries

    stems = [
        _strip_source_markers(
            os.path.splitext(entry["filename"])[0], config.source_markers
        )
        for entry in entries
        if entry["kind"] == "video"
    ]
    shared_author = _shared_author_prefix(stems)
    if shared_author:
        for entry in unresolved:
            _apply_author(
                config,
                entry,
                shared_author,
                0.6,
                "同频道文件夹内文件名共享前缀",
            )
    return entries


def _iter_plan_sources(config: OrganizeConfig) -> List[Tuple[str, dict]]:
    sources: Dict[str, dict] = {}
    with _plan_lock:
        for path, meta in _pending_sources.items():
            if os.path.isfile(path):
                sources[path] = meta

    if config.scan_library_roots:
        roots = [
            os.path.join(config.sync_root, config.audio_dir),
            os.path.join(config.sync_root, config.video_dir),
            os.path.join(config.sync_root, config.pending_dir),
        ]
        for root in roots:
            if not os.path.isdir(root):
                continue
            for name in sorted(os.listdir(root)):
                path = os.path.join(root, name)
                # Only scan files sitting directly in the library roots. Author
                # folders are considered already classified and are left alone.
                if os.path.isfile(path) and _file_kind(path):
                    sources.setdefault(path, {"chat_title": "", "added_at": 0})

    if config.scan_download_root and config.download_root:
        download_root = os.path.abspath(config.download_root)
        found = 0
        if os.path.isdir(download_root):
            for current, dirs, files in os.walk(download_root):
                dirs[:] = sorted(name for name in dirs if not name.startswith("."))
                for name in sorted(files):
                    if found >= MAX_SCAN_FILES:
                        break
                    path = os.path.join(current, name)
                    if not _file_kind(path):
                        continue
                    found += 1
                    sources.setdefault(path, {"chat_title": "", "added_at": 0})
                if found >= MAX_SCAN_FILES:
                    break
    return list(sources.items())


def build_plan(config: Optional[OrganizeConfig] = None) -> dict:
    """Rebuild the organize plan from pending downloads and library roots."""
    global _plan_entries, _plan_generated_at, _plan_dirty
    config = config or _config
    if not config.enable or not config.sync_root:
        with _plan_lock:
            _plan_entries = []
            _plan_generated_at = time.time()
            _plan_dirty = False
        return get_plan(config)

    index = _build_author_index(config)
    groups: Dict[str, List[Tuple[str, dict]]] = {}
    for source, meta in _iter_plan_sources(config):
        folder = _channel_folder(config, source, meta.get("chat_title", ""))
        groups.setdefault(folder, []).append((source, meta))

    entries: List[dict] = []
    seen = set()
    for folder in sorted(groups):
        for entry in _build_group_entries(config, index, groups[folder]):
            if entry["source"] in seen:
                continue
            seen.add(entry["source"])
            entries.append(entry)

    entries.sort(
        key=lambda item: (
            item["kind"],
            item["channel_folder"].casefold(),
            item["filename"].casefold(),
        )
    )
    with _plan_lock:
        _plan_entries = entries
        _plan_generated_at = time.time()
        _plan_dirty = False
    _persist_plan(config)
    return get_plan(config)


def get_plan(config: Optional[OrganizeConfig] = None, rebuild: bool = True) -> dict:
    """Return the current plan, rebuilding it when pending files changed."""
    config = config or _config
    if rebuild and _plan_dirty and config.enable and config.sync_root:
        return build_plan(config)

    with _plan_lock:
        entries = [dict(entry) for entry in _plan_entries]
        generated_at = _plan_generated_at

    summary = {
        "total": len(entries),
        "move": sum(
            1
            for entry in entries
            if entry["action"] == "move" and entry["status"] == "planned"
        ),
        "conflict": sum(
            1 for entry in entries if entry["conflict"] and entry["status"] == "planned"
        ),
        "pending": sum(
            1
            for entry in entries
            if not entry["author"] and entry["status"] == "planned"
        ),
        "done": sum(1 for entry in entries if entry["status"] == "done"),
        "failed": sum(1 for entry in entries if entry["status"] == "failed"),
    }
    warning = ""
    if config.write_audio_tags and MutagenFile is None:
        warning = "mutagen 未安装，音频标签不会写入；请执行 pip install mutagen"

    return {
        "enabled": bool(config.enable and config.sync_root),
        "sync_root": config.sync_root,
        "generated_at": generated_at,
        "warning": warning,
        "summary": summary,
        "entries": entries,
    }


def _plan_file_path(config: OrganizeConfig) -> str:
    path = config.plan_file or os.path.join("log", "organize_plan.json")
    if not os.path.isabs(path):
        path = os.path.join(os.path.abspath("."), path)
    return path


def _persist_plan(config: OrganizeConfig) -> None:
    path = _plan_file_path(config)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = get_plan(config, rebuild=False)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as plan_file:
            json.dump(data, plan_file, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception as exc:  # pragma: no cover - best effort audit trail
        logger.warning("Could not persist organize plan: %s", exc)


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _backup_root(config: OrganizeConfig) -> str:
    if os.path.isabs(config.backup_dir):
        root = config.backup_dir
    else:
        root = os.path.join(config.sync_root, config.backup_dir)
    return os.path.join(root, time.strftime("%Y%m%d-%H%M%S"))


def _backup_file(source: str, backup_root: str) -> str:
    os.makedirs(backup_root, exist_ok=True)
    target = os.path.join(backup_root, os.path.basename(source))
    counter = 1
    while os.path.exists(target):
        name, extension = os.path.splitext(os.path.basename(source))
        target = os.path.join(backup_root, f"{name}-{counter}{extension}")
        counter += 1
    shutil.copy2(source, target)
    return target


def _write_audio_tags(path: str, author: str) -> Tuple[bool, str]:
    if MutagenFile is None:
        return False, "mutagen 未安装，无法写入音频标签"
    try:
        audio = MutagenFile(path, easy=True)
        if audio is None:
            return False, "无法识别音频格式"
        audio["artist"] = [author]
        audio["albumartist"] = [author]
        audio["album"] = [author]
        audio.save()
        return True, ""
    except Exception as exc:
        return False, f"写入标签失败: {exc}"


def _verify_audio(path: str) -> Tuple[bool, str]:
    if MutagenFile is None:
        return True, ""
    try:
        audio = MutagenFile(path, easy=True)
        if audio is None:
            return False, "写入后无法读取音频"
        if getattr(audio.info, "length", 0) <= 0:
            return False, "写入后音频时长异常"
        return True, ""
    except Exception as exc:
        return False, f"写入后校验失败: {exc}"


def _restore_backup(backup: str, source: str) -> None:
    try:
        os.makedirs(os.path.dirname(source), exist_ok=True)
        shutil.copy2(backup, source)
    except Exception:
        logger.exception("Could not restore %s from %s", source, backup)


def _execute_entry(config: OrganizeConfig, entry: dict, backup_root: str) -> dict:
    source = entry["source"]
    target = entry["target"]

    if not os.path.isfile(source):
        entry["status"] = "failed"
        entry["message"] = "源文件不存在"
        return entry
    if os.path.exists(target):
        if _same_file(source, target):
            entry["status"] = "skipped"
            entry["message"] = "文件已在目标位置"
            return entry
        entry["status"] = "skipped"
        entry["message"] = "目标文件已存在，未覆盖"
        return entry

    os.makedirs(os.path.dirname(target), exist_ok=True)
    try:
        backup = _backup_file(source, backup_root)
    except Exception as exc:
        entry["status"] = "failed"
        entry["message"] = f"备份失败: {exc}"
        return entry
    entry["backup"] = backup

    entry["checksum_before"] = _sha256(source)
    note = ""
    if (
        entry["kind"] == "audio"
        and config.write_audio_tags
        and entry["author"]
        and _file_extension(source) in TAG_WRITABLE_EXTENSIONS
    ):
        if MutagenFile is None:
            # Missing optional dependency must not block the move itself.
            note = "mutagen 未安装，已跳过标签写入"
        else:
            written, message = _write_audio_tags(source, entry["author"])
            if not written:
                _restore_backup(backup, source)
                entry["status"] = "failed"
                entry["message"] = message
                return entry
            verified, message = _verify_audio(source)
            if not verified:
                _restore_backup(backup, source)
                entry["status"] = "failed"
                entry["message"] = message
                return entry

    hash_after = _sha256(source)
    entry["checksum_after"] = hash_after
    try:
        shutil.move(source, target)
    except Exception as exc:
        _restore_backup(backup, source)
        entry["status"] = "failed"
        entry["message"] = f"移动失败: {exc}"
        return entry

    if not os.path.isfile(target) or _sha256(target) != hash_after:
        try:
            if os.path.isfile(target):
                os.remove(target)
        except OSError:
            pass
        _restore_backup(backup, source)
        entry["status"] = "failed"
        entry["message"] = "移动后校验失败，已恢复备份"
        return entry

    entry["status"] = "done"
    entry["message"] = f"已移动到 {target}" + (f"（{note}）" if note else "")
    with _plan_lock:
        _pending_sources.pop(source, None)
    return entry


def execute_plan(
    config: Optional[OrganizeConfig] = None, entry_ids: Optional[List[str]] = None
) -> dict:
    """Execute planned entries and return per-entry results."""
    config = config or _config
    if not config.enable or not config.sync_root:
        return {
            "ok": False,
            "message": "整理功能未启用或未配置 sync_root",
            "results": [],
            "plan": get_plan(config, rebuild=False),
        }

    with _plan_lock:
        selected = [
            dict(entry)
            for entry in _plan_entries
            if entry["status"] == "planned"
            and (not entry_ids or entry["id"] in entry_ids)
        ]
    if not selected:
        return {
            "ok": True,
            "message": "没有待执行的整理项",
            "results": [],
            "plan": get_plan(config, rebuild=False),
        }

    backup_root = _backup_root(config)
    results = [_execute_entry(config, entry, backup_root) for entry in selected]

    with _plan_lock:
        result_map = {entry["id"]: entry for entry in results}
        for entry in _plan_entries:
            if entry["id"] in result_map:
                entry.update(result_map[entry["id"]])
        _plan_dirty = False
    _persist_plan(config)

    executed = sum(1 for entry in results if entry["status"] == "done")
    failed = sum(1 for entry in results if entry["status"] == "failed")
    skipped = sum(1 for entry in results if entry["status"] == "skipped")
    return {
        "ok": failed == 0,
        "message": f"执行完成：成功 {executed}，失败 {failed}，跳过 {skipped}",
        "results": results,
        "plan": get_plan(config, rebuild=False),
    }
