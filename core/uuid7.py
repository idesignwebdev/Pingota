"""UUIDv7 helper.

MongoDB has no auto-increment, and random UUIDv4 keys scatter inserts across
the index. UUIDv7 embeds a millisecond timestamp in the high bits, so keys sort
by creation time and inserts stay roughly sequential — without a sequence.
"""
import os
import time
import uuid


def uuid7() -> uuid.UUID:
    ms = int(time.time() * 1000)
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76          # version 7
    value |= (rand >> 62) & 0xFFF << 64
    value |= 0b10 << 62         # RFC 4122 variant
    value |= rand & ((1 << 62) - 1)
    return uuid.UUID(int=value)
