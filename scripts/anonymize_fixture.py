"""Replace the text in a captured `related_sheet` payload with made-up text, keeping its structure.

    python scripts/anonymize_fixture.py captured.b64 tests/fixtures/new.b64

Strings in the value tables (plain strings, rich-text runs, formula text pieces) and picture URLs are
replaced; numbers, styles, merges and sizes are kept, so the decoder sees the same layout. Each distinct
string maps to the same replacement everywhere, and the replacement keeps the original's script (Han,
Arabic, Japanese kana, Latin, ...) so right-to-left and CJK handling is still exercised. Strings listed in
KEEP (column headers, function names) stay as they are. Check the output before committing it.
"""

from __future__ import annotations

import base64
import re
import sys
import zlib

# field paths (see the decoder docstring) whose length-delimited values are sub-messages to walk into
DESCEND = {
    (1,), (1, 5), (1, 5, 19), (1, 5, 19, 5), (1, 5, 19, 5, 1), (1, 5, 19, 5, 2), (1, 5, 19, 5, 2, 3),
    (1, 5, 19, 5, 2, 3, 3), (1, 5, 19, 5, 5), (1, 5, 19, 5, 5, 1), (1, 5, 19, 5, 5, 1, 1),
    (1, 5, 19, 5, 5, 1, 1, 1), (1, 5, 19, 6), (1, 5, 19, 6, 3), (1, 5, 19, 6, 3, 17),
}
TEXT = {(1, 5, 19, 5, 1, 1), (1, 5, 19, 5, 2, 3, 3, 1), (1, 5, 19, 5, 5, 1, 1, 1, 1)}
PICTURE = (1, 5, 19, 6, 3, 17, 1)
KEEP = {"key", "简体", "繁体", "中东", "印尼", "越南", "日本", "类别", "来源", "单价(元)", " 数量", "小计(元)",
        "退款(元)", "总计(元)", "备注"}

SAMPLES = [
    (re.compile(r"[؀-ۿ]"), "نص تجريبي"),
    (re.compile(r"[぀-ヿ]"), "サンプルテキスト"),
    (re.compile(r"[一-鿿]"), "示例文本"),
    (re.compile(r"[฀-๿]"), "ข้อความตัวอย่าง"),
    (re.compile(r"[À-ɏḀ-ỿ]"), "Văn bản mẫu"),
]


def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


class Faker:
    def __init__(self) -> None:
        self.seen: dict[str, str] = {}

    def text(self, s: str) -> str:
        if s in KEEP or not s.strip() or re.fullmatch(r"[\s\d.,:;%()+\-*/=<>!&$\"'A-Z]*", s):
            return s  # headers, numbers, operators and formula function names carry no content
        if s not in self.seen:
            base = next((sample for pattern, sample in SAMPLES if pattern.search(s)), "Sample text")
            self.seen[s] = f"{base} {len(self.seen) + 1}"
        return self.seen[s]

    def picture(self, s: str) -> str:
        n = len(self.seen) + 1
        self.seen.setdefault(s, f"pic{n}")
        return re.sub(r'^\["[^"]*"', f'["https://wdcdn.qpic.cn/MTY4ODg1/example-{self.seen[s]}/0"', s)


def rewrite(buf: bytes, path: tuple[int, ...], fake: Faker) -> bytes:
    out = bytearray()
    pos = 0
    while pos < len(buf):
        start = pos
        key, pos = read_varint(buf, pos)
        fno, wt = key >> 3, key & 7
        if wt == 0:
            _, pos = read_varint(buf, pos)
        elif wt == 1:
            pos += 8
        elif wt == 5:
            pos += 4
        elif wt == 2:
            n, body = read_varint(buf, pos)
            pos = body + n
            value, sub = buf[body:pos], path + (fno,)
            if sub in DESCEND:
                value = rewrite(value, sub, fake)
            elif sub in TEXT:
                value = fake.text(value.decode()).encode()
            elif sub == PICTURE:
                value = fake.picture(value.decode()).encode()
            else:
                out += buf[start:pos]
                continue
            out += varint(key) + varint(len(value)) + value
            continue
        else:
            raise ValueError(f"wire type {wt} at {start}")
        out += buf[start:pos]
    return bytes(out)


def main() -> None:
    src, dst = sys.argv[1:3]
    raw = zlib.decompress(base64.b64decode(open(src).read()))
    with open(dst, "w") as f:
        f.write(base64.b64encode(zlib.compress(rewrite(raw, (), Faker()), 9)).decode())


if __name__ == "__main__":
    main()
