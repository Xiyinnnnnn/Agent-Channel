# -*- coding: utf-8 -*-
"""协议枚举常量 —— 严格对齐官方 @tencent-weixin/openclaw-weixin v2.4.8 (70ab695) src/api/types.ts。

两组枚举必须严格分离、禁止混用（task 书 3.x）：
  UploadMediaType  → 用于 getUploadUrl.media_type（上传通道类型）
  MessageItemType  → 用于 sendMessage item_list[].type（消息条目类型）
"""
from enum import IntEnum


class UploadMediaType(IntEnum):
    """proto: UploadMediaType（官方: IMAGE=1 VIDEO=2 FILE=3 VOICE=4）"""
    IMAGE = 1
    VIDEO = 2
    FILE = 3
    VOICE = 4


class MessageItemType(IntEnum):
    """proto: MessageItemType（官方: NONE=0 TEXT=1 IMAGE=2 VOICE=3 FILE=4 VIDEO=5）"""
    NONE = 0
    TEXT = 1
    IMAGE = 2
    VOICE = 3
    FILE = 4
    VIDEO = 5
    TOOL_CALL_START = 11
    TOOL_CALL_RESULT = 12


class MessageType(IntEnum):
    """proto: MessageType（BOT=2 用于外发）"""
    NONE = 0
    USER = 1
    BOT = 2


class MessageState(IntEnum):
    NEW = 0
    GENERATING = 1
    FINISH = 2


# 缩略图：P1 FILE 一律 no_need_thumb=true，不生成任何 thumb 字段（task 书 8.2）
