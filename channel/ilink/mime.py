# -*- coding: utf-8 -*-
"""扩展名 → MIME 映射（官方 src/media/mime.ts 移植）。
注意：MIME 只用于日志/信息展示；P1 上传类型恒定 UploadMediaType.FILE，
不因扩展名是 image/video 而改变上传路径（png/jpg 也强制 FILE，见 task 书 23/42 Case7）。
"""
import os

_EXT_TO_MIME = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".json": "application/json",
    ".csv": "text/csv",
    ".zip": "application/zip",
    ".tar": "application/x-tar",
    ".gz": "application/gzip",
    ".7z": "application/x-7z-compressed",
    ".rar": "application/vnd.rar",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def mime_from_filename(filename: str) -> str:
    """按扩展名推断 MIME；未知一律 application/octet-stream（官方同款）。"""
    ext = os.path.splitext(filename)[1].lower()
    return _EXT_TO_MIME.get(ext, "application/octet-stream")


if __name__ == "__main__":
    for f in ("a.txt", "中文名.pdf", "x.png", "y.zip", "z.unknown", "noext"):
        print(f, "→", mime_from_filename(f))
