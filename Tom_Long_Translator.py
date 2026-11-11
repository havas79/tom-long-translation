"""
Tom Long Translator — translate "Tom Long: The Time Adventure" (1994) into English.

Open the game folder; everything translatable is listed in the drop-down:
  * Dialogues  (*.DLG)       NPC lines and player choices. Relocatable: any length up to the
                             engine limits (255 bytes, 32-column wrap, 16 lines per node).
  * Items      (ITEMS.ITM)   object names (16-byte field) and the speech bubbles of their
                             look/take/use/walk/talk scripts.
  * Scripts    (PROCS.CMD)   speech bubbles of the global scripts.
  * Room exits (*.ROM)       exit names (16-byte field, 15 characters).
  * Program    (ENGINE.EXE)  verb names, Tom's default replies and the Save/Load/Quit menus,
                             edited in place (see ExeSource for the limits).

Speech bubbles (script opcode C1 "SAY") hold 24 characters. A longer translation is split at
spaces into several bubbles, each followed by a pause (opcode D2, SPLIT_WAIT_TICKS), exactly
like the game's own "1...  2...  3..." telephone countdown. ITEMS.ITM and PROCS.CMD are then
rebuilt with corrected offset tables.

For dialogues, "Dialogue tree" shows the whole conversation as an expandable tree (each exchange written
out once, where the player first reaches it; other ways to it are ↪ links; endings show the flags the
calling script sets), can export it — or all dialogues — as one HTML page, and "Play through" walks the
conversation like the game does. Both show the translation where there is one, else the German.

Translations are saved automatically on every change, next to the German files:
  NAME.dlgtrans.json (same format as the DLG Translator), ITEMS.trans.json,
  PROCS.trans.json, ROOMS.trans.json, ENGINE.trans.json.
"Build translated files" writes the changed game files, under their original names, to a
folder you choose. Standard library only (tkinter).
"""

from __future__ import annotations

import struct
import json
from pathlib import Path

ENCODING = "cp437"
MAX_STRING = 255          # 256-byte strcpy buffers in 08af:030a
MAX_FILE = 0xFFFB         # 16-bit fread size + 4-byte header in 08af:07d0
WRAP_COLS = 32            # 08af:00b0
MAX_LINES = 16            # 160-px dialogue window / 10-px lines


# ──────────────────────────────────────────────
# Parser — mirrors ENGINE.EXE exactly
# ──────────────────────────────────────────────

def _read_cstring(data: bytes, pos: int) -> tuple[str, int]:
    end = data.index(b"\x00", pos)
    return data[pos:end].decode(ENCODING, errors="replace"), end + 1


def read_node(data: bytes, offset: int) -> dict:
    ptrs = list(struct.unpack_from("<3i", data, offset))
    npc, pos = _read_cstring(data, offset + 12)
    choices = []
    for _ in range(3):
        s, pos = _read_cstring(data, pos)
        if not s:
            break
        choices.append(s)
    return {
        "offset": offset, "ptrs": ptrs, "n_active": len(choices),
        "npc": npc, "choices": list(choices),
        "npc_orig": npc, "choices_orig": list(choices),
    }


def parse_dlg(data: bytes) -> list[dict]:
    """Every node reachable from offset 0, in file order."""
    nodes: dict[int, dict] = {}
    queue = [0]
    while queue:
        off = queue.pop(0)
        if off in nodes:
            continue
        if not (0 <= off <= len(data) - 12):
            raise ValueError(f"pointer 0x{off:x} outside file")
        node = read_node(data, off)
        nodes[off] = node
        for i in range(node["n_active"]):
            t = node["ptrs"][i]
            if t >= 0 and t not in nodes:
                queue.append(t)
    return sorted(nodes.values(), key=lambda n: n["offset"])


# ──────────────────────────────────────────────
# Line-wrap simulation (08af:00b0) and checks
# ──────────────────────────────────────────────

def wrap(s: bytes, width: int = WRAP_COLS) -> tuple[list[bytes], bool]:
    lines, p = [], 0
    while p < len(s):
        si = q = di = 0; q = p; brk = p
        while si < width:
            if q >= len(s):
                brk, di = q, si
                break
            c = s[q]
            if c in (0x20, 0x2D):
                if c == 0x2D and si < width - 1:
                    q += 1; si += 1
                brk, di = q, si
            q += 1; si += 1
        if di == 0:
            return lines, True
        lines.append(s[p:p + di]); p = brk
        while p < len(s) and s[p] == 0x20:
            p += 1
    return lines, False


def wrap_spans(text: str, width: int = WRAP_COLS) -> tuple[list[tuple[int, int]], bool]:
    """Character ranges of the lines the game draws (same rules as wrap()). Words the game
    can't place (longer than a line) are cut at the width so the editor can still show them."""
    lines, p, cut = [], 0, False
    n = len(text)
    while p < n:
        si = di = 0; q = p; brk = p
        while si < width:
            if q >= n:
                brk, di = q, si
                break
            c = text[q]
            if c in " -":
                if c == "-" and si < width - 1:
                    q += 1; si += 1
                brk, di = q, si
            q += 1; si += 1
        if di == 0:                       # the engine stops drawing here
            cut = True
            brk, di = min(p + width, n), min(width, n - p)
        lines.append((p, p + di)); p = brk
        while p < n and text[p] == " ":
            p += 1
    return lines, cut


_CONTROL_NAMES = {"\n": "line break", "\r": "carriage return", "\t": "tab", "\x7f": "DEL"}


def prohibited_chars(text: str) -> list[str]:
    """Characters the game can't show as intended.

    Text is drawn with the VGA BIOS 8×8 font (INT 10h AX=1130h, 10e5:0001), so every
    CP437 character 0x20-0xFE has a glyph. Prohibited are:
      * characters outside CP437 (e.g. … “ ” ’ – € ş) — saved as '?'
      * control characters (line break, tab, …) — the engine has no line breaks; it
        draws them as symbols such as ◙ and ○, and wraps only at spaces and '-'.
    Returns a readable name for each distinct offending character.
    """
    found = []
    for ch in dict.fromkeys(text):
        if ord(ch) < 0x20 or ch == "\x7f":
            found.append(_CONTROL_NAMES.get(ch, f"control U+{ord(ch):04X}"))
        else:
            try:
                ch.encode(ENCODING)
            except UnicodeEncodeError:
                found.append(f"{ch} (U+{ord(ch):04X})")
    return found


def node_screen_lines(texts: list[str]) -> int:
    """Screen lines a dialogue window uses (08af:030a): the NPC line, a blank line, then every choice
    followed by a blank line. The blank after the last choice is never drawn, so it isn't counted."""
    enc = [t.encode(ENCODING, errors="replace") for t in texts]
    n = sum(len(wrap(b)[0]) for b in enc) + 1 + max(0, len(enc) - 2)
    return n


def check_nodes(nodes: list[dict]) -> list[str]:
    problems = []
    for n in nodes:
        for i, t in enumerate([n["npc"], *n["choices"]]):
            bad = prohibited_chars(t)
            if bad:
                who = "NPC" if i == 0 else f"choice {i}"
                problems.append(f"node 0x{n['offset']:04x} {who}: prohibited character(s): {', '.join(bad)}")
        enc = [t.encode(ENCODING, errors="replace") for t in [n["npc"], *n["choices"]]]
        for i, b in enumerate(enc):
            who = "NPC" if i == 0 else f"choice {i}"
            if len(b) > MAX_STRING:
                problems.append(f"node 0x{n['offset']:04x} {who}: {len(b)} bytes (max {MAX_STRING})")
            lines, cut = wrap(b)
            if cut:
                problems.append(f"node 0x{n['offset']:04x} {who}: a word is longer than {WRAP_COLS} chars; the engine stops drawing there")
        total = node_screen_lines([n["npc"], *n["choices"]])
        if total > MAX_LINES:
            problems.append(f"node 0x{n['offset']:04x}: {total} screen lines (window holds {MAX_LINES})")
    return problems


# ──────────────────────────────────────────────
# Builder — reconstruct .dlg with translated text
# ──────────────────────────────────────────────

def build_dlg(nodes: list[dict]) -> bytes:
    """Rebuild the .dlg binary. Root stays at offset 0; negative results are kept."""
    if not nodes or nodes[0]["offset"] != 0:
        raise ValueError("root node (offset 0) missing")
    encoded = []
    for node in nodes:
        if any(not c for c in node["choices"]):
            raise ValueError(f"node 0x{node['offset']:04x}: empty choice would hide the choices after it")
        body = node["npc"].encode(ENCODING, errors="replace") + b"\x00"
        body += b"".join(c.encode(ENCODING, errors="replace") + b"\x00" for c in node["choices"])
        if len(node["choices"]) < 3:
            body += b"\x00"          # empty string ends the choice list
        encoded.append(body)

    old_to_new, cursor = {}, 0
    for node, body in zip(nodes, encoded):
        old_to_new[node["offset"]] = cursor
        cursor += 12 + len(body)
    if cursor > MAX_FILE:
        raise ValueError(f"file would be {cursor} bytes; the engine loads at most {MAX_FILE}")

    out = bytearray()
    for node, body in zip(nodes, encoded):
        ptrs = []
        for i, p in enumerate(node["ptrs"]):
            if i >= len(node["choices"]) or p < 0:
                ptrs.append(p if i < len(node["choices"]) else 0)
            else:
                ptrs.append(old_to_new[p])
        out += struct.pack("<3i", *ptrs) + body
    return bytes(out)




# ──────────────────────────────────────────────
# Script byte-code (ITEMS.ITM, PROCS.CMD) — ENGINE.EXE 058a:01d1 / length table 058a:001f
# ──────────────────────────────────────────────

VM_LEN = {0xc0: 1, 0xc1: 0x1d, 0xc2: 1, 0xc3: 2, 0xc4: 3, 0xc5: 1, 0xc6: 2, 0xc7: 3, 0xc8: 3, 0xc9: 3, 0xca: 9,
          0xcb: 3, 0xcc: 1, 0xcd: 1, 0xce: 3, 0xcf: 3, 0xd0: 1, 0xd1: 3, 0xd2: 3, 0xd3: 0xc, 0xd4: 3, 0xd5: 1,
          0xd6: 1, 0xd7: 1, 0xd8: 1, 0xd9: 1, 0xda: 0xf, 0xdb: 0xe, 0xdc: 7, 0xdd: 3, 0xde: 5, 0xdf: 1, 0xe0: 3,
          0xe1: 1, 0xe2: 9, 0xe3: 1, 0xe4: 1, 0xe5: 1, 0xe6: 1, 0xe7: 1, 0xe8: 3, 0xe9: 1, 0xea: 1, 0xeb: 1,
          0xec: 1, 0xed: 9, 0xee: 3, 0xef: 3, 0xf0: 2, 0xf1: 1, 0xf2: 1, 0xf3: 3, 0xf4: 2, 0xf5: 5, 0xf6: 1, 0xf7: 1}
OP_END, OP_SAY, OP_WAIT = 0xC0, 0xC1, 0xD2
SAY_MAX = 24              # C1: 24-byte text field, copied with memcpy(…, 0x18) + NUL (0a1f:0b9b)
NAME_MAX = 16             # ITEMS.ITM tag 2: 16-byte field (058a:0f77 skips exactly 0x10)
LABEL_MAX = 15            # .ROM exit label: 16 bytes read raw; keep one NUL (longest original is 15)
# Pause between the parts of a split SAY line, in the engine's wait ticks (opcode D2);
# one tick is about 0.11 s (every second 18.2 Hz timer interrupt), so 20 ticks = about 2.2 s.
# The original scripts use D2 40 after "Fünf Minuten später..." — change here if the
# parts go by too fast or too slow in the game.
SPLIT_WAIT_TICKS = 20


def script_ops(d: bytes, p: int):
    """Yield (offset, opcode) for one script, up to and including END."""
    while True:
        op = d[p]
        if op not in VM_LEN:
            raise ValueError(f"unknown script opcode {op:#x} at {p:#x}")
        yield p, op
        p += VM_LEN[op]
        if op == OP_END:
            return


# Opcode names for the context view: op -> (name, operand format, short note).
# Formats: h = s16, B = u8, 8 = char[8] file name, i = s16 item (-1 = this object), b = u8 item.
VM_INFO = {
    0xc0: ("END", "", ""),
    0xc1: ("SAY", "", ""),
    0xc2: ("TAKE_THIS", "", "take this object into the inventory"),
    0xc3: ("MORPH_THIS", "b", "turn this object into another item"),
    0xc4: ("IF_USED_WITH", "i", ""),
    0xc5: ("END_IF", "", ""),
    0xc6: ("ADD_INV", "b", "add to inventory"),
    0xc7: ("SET_FLAG", "h", ""),
    0xc8: ("CLEAR_FLAG", "h", ""),
    0xc9: ("TOGGLE_FLAG", "h", ""),
    0xca: ("DIALOG", "8", ""),
    0xcb: ("IF_FLAG", "h", ""),
    0xcc: ("END_IF", "", ""),
    0xcd: ("START_SIGNAL", "", "start animations waiting for the signal"),
    0xce: ("RELEASE_ANIM", "h", ""),
    0xcf: ("WAIT_ANIM", "h", "until stop point or end"),
    0xd0: ("SHOW_TOM", "", ""),
    0xd1: ("REMOVE_INV", "i", "remove from inventory"),
    0xd2: ("WAIT", "h", "ticks, 1 tick = 0.11 s"),
    0xd3: ("PLACE_ITEM", "ihhhhB", "put an object in the room"),
    0xd4: ("IF_NOT_FLAG", "h", ""),
    0xd5: ("TOM_ANIM_PLAY", "", ""),
    0xd6: ("TOM_ANIM_WAIT", "", ""),
    0xd7: ("TOM_ANIM_RELEASE", "", ""),
    0xd8: ("HIDE_SPRITE", "", "hide this object's picture"),
    0xd9: ("SHOW_SPRITE", "", "show this object's picture"),
    0xda: ("DIALOG_RESULT", "8hhh", "result -1/-2/-3 sets flag 1/2/3"),
    0xdb: ("GOTO_ROOM", "8hhB", "x, y, entry (255 = none)"),
    0xdc: ("START_ANIM", "hhh", "anim, x, y"),
    0xdd: ("STOP_ANIM", "h", ""),
    0xde: ("WALK_TO", "hh", "and wait"),
    0xdf: ("HIDE_TOM", "", ""),
    0xe0: ("TOM_ANIM_LOAD", "h", ""),
    0xe1: ("TOM_ANIM_DROP", "", ""),
    0xe2: ("SET_PLAYER", "8", "swap the player character .NPC"),
    0xe3: ("ENABLE_SAVE", "", "show the Save button"),
    0xe4: ("LABELS_ON", "", "object names under the mouse"),
    0xe5: ("LABELS_OFF", "", "object names under the mouse"),
    0xe6: ("FADE_IN", "", ""),
    0xe7: ("FADE_OUT", "", ""),
    0xe8: ("REMOVE_ITEM", "i", "remove from the room"),
    0xe9: ("HIDE_POINTER", "", ""),
    0xea: ("SHOW_POINTER", "", ""),
    0xeb: ("SCROLL_OFF", "", "screen stops following Tom"),
    0xec: ("SCROLL_ON", "", "screen follows Tom"),
    0xed: ("WALK_VIA", "hhhh", "x1, y1, x2, y2 and wait"),
    0xee: ("SPRITE_FRONT", "i", "draw in front of the characters"),
    0xef: ("SPRITE_BACK", "i", "draw in the background"),
    0xf0: ("TURN_TOM", "B", ""),
    0xf1: ("EXITS_OFF", "", ""),
    0xf2: ("EXITS_ON", "", ""),
    0xf3: ("RESET_ITEM", "i", ""),
    0xf4: ("PULSE_COLOUR", "B", ""),
    0xf5: ("SOUND", "BBBB", "effect, volume (0 = stop), repeats, delay"),
    0xf6: ("HIDE_INVENTORY", "", ""),
    0xf7: ("SCREEN_ON", "", ""),
}
BLOCK_OPEN, BLOCK_CLOSE = {0xc4, 0xcb, 0xd4}, {0xc5, 0xcc}


def disasm(d: bytes, at: int, names=None) -> tuple[str, str]:
    """(instruction text, note) for the instruction at `at`; `names` maps item index -> name."""
    op = d[at]
    name, fmt, note = VM_INFO[op]
    if op == OP_SAY:
        x, y = struct.unpack_from("<hh", d, at + 25)
        pos = {-1: "at the mouse pointer", -2: "screen centre"}.get(x, f"{x},{y}")
        return f'SAY "{fixed_text(d[at + 1:at + 25])}"', pos
    p, args = at + 1, []
    for f in fmt:
        if f == "8":
            args.append(fixed_text(d[p:p + 8])); p += 8
        elif f in "hi":
            v = struct.unpack_from("<h", d, p)[0]; p += 2
            if f == "i":
                args.append("this" if v == -1 else f"#{v}" + (f" {names[v]}" if names and names.get(v) else ""))
            else:
                args.append(str(v))
        else:
            v = d[p]; p += 1
            if f == "b":
                args.append(f"#{v}" + (f" {names[v]}" if names and names.get(v) else ""))
            else:
                args.append(str(v))
    if op == 0xf0:
        args = [{"1": "left", "2": "right", "3": "away", "4": "toward viewer"}.get(args[0], args[0])]
    return (name + (" " + ", ".join(args) if args else "")), note


def script_listing(d: bytes, start: int, current: int, names=None) -> list[tuple[str, str]]:
    """Context lines for a whole script; the SAY at `current` is highlighted."""
    lines, depth = [], 0
    for at, op in script_ops(d, start):
        if op in BLOCK_CLOSE:
            depth = max(0, depth - 1)
        text, note = disasm(d, at, names)
        style = "on" if at == current else ("say" if op == OP_SAY else "code")
        lines.append(("    " * depth + text, style))
        if note:
            lines.append((f"   ; {note}", "note"))      # shown on the same line
        if op in BLOCK_OPEN:
            depth += 1
    return lines


def fixed_text(b: bytes) -> str:
    return b.split(b"\0")[0].decode(ENCODING)


def _greedy_spans(words: list[tuple[int, int]], width: int) -> list[tuple[int, int]]:
    lines, cur = [], None
    for ws, we in words:
        if cur is None:
            cur = [ws, we]
        elif we - cur[0] <= width:
            cur[1] = we
        else:
            lines.append(tuple(cur)); cur = [ws, we]
    if cur is not None:
        lines.append(tuple(cur))
    return lines


def say_spans(text: str) -> tuple[list[tuple[int, int]], str | None]:
    """Character ranges of the ≤24-character bubbles a SAY line is split into: as few
    bubbles as possible, then as even as possible. Returns (spans, error)."""
    import re
    words = [(m.start(), m.end()) for m in re.finditer(r"[^ ]+", text)]
    if not words:
        return [(0, len(text))], None
    for ws, we in words:
        if we - ws > SAY_MAX:
            return [], f"the word '{text[ws:we][:30]}' is longer than {SAY_MAX} characters"
    k = len(_greedy_spans(words, SAY_MAX))
    for width in range(max(we - ws for ws, we in words), SAY_MAX + 1):
        spans = _greedy_spans(words, width)
        if len(spans) <= k:
            return spans, None
    return _greedy_spans(words, SAY_MAX), None


def split_say(text: str) -> tuple[list[str], str | None]:
    """Split a SAY line into ≤24-character bubbles at spaces. Returns (parts, error)."""
    spans, err = say_spans(text)
    return ([text[a:b] for a, b in spans] if not err else []), err


def say_bytes(text: str, x: int, y: int) -> bytes:
    return bytes([OP_SAY]) + text.encode(ENCODING).ljust(SAY_MAX, b"\0") + struct.pack("<hh", x, y)


def apply_patches(data: bytes, patches: list[tuple[int, int, bytes]]) -> tuple[bytes, callable]:
    """Replace data[off:off+n] by new for each patch; return new bytes and an old→new offset map."""
    patches = sorted(patches)
    out = bytearray(); pos = 0; shifts = []
    for off, n, new in patches:
        out += data[pos:off]; out += new; pos = off + n
        shifts.append((off, len(new) - n))
    out += data[pos:]

    def remap(old: int) -> int:
        return old + sum(delta for off, delta in shifts if off < old)
    return bytes(out), remap


# ──────────────────────────────────────────────
# Translatable entries and sources
# ──────────────────────────────────────────────

class Entry:
    """One translatable string."""
    def __init__(self, key: str, original: str, label: str, indent: bool, kind: str, **info):
        self.key = key
        self.original = original
        self.label = label        # short tag in the sidebar: "NPC", "1.", "name", "look"…
        self.indent = indent      # indented under the previous header row
        self.kind = kind          # "npc" | "choice" | "name" | "say" | "label"
        self.info = info
        self.translation = ""

    @property
    def is_translated(self) -> bool:
        return bool(self.translation.strip())

    @property
    def text(self) -> str:        # what goes into the game file
        return self.translation.strip() or self.original

    def list_label(self) -> str:
        mark = "●" if self.is_translated else "○"
        t = self.original.replace("\n", " ") or "(empty)"
        room = 60 if not self.indent else 54
        t = t[:room] + ("…" if len(t) > room else "")
        return f"        {mark} {self.label}  {t}" if self.indent else f"{mark} {self.label}  {t}"


class Source:
    """A group of entries that is translated together and saved to one sidecar."""
    title = ""
    sidecar: Path

    def __init__(self):
        self.entries: list[Entry] = []

    # sidecar -----------------------------------------------------------
    def load_sidecar(self):
        if self.sidecar.exists():
            try:
                saved = json.loads(self.sidecar.read_text(encoding="utf-8"))
                for e in self.entries:
                    if e.key in saved:
                        e.translation = saved[e.key]
            except Exception:
                pass

    def save_sidecar(self):
        data = {e.key: e.translation for e in self.entries if e.translation}
        if data or self.sidecar.exists():
            self.sidecar.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @property
    def done(self) -> int:
        return sum(e.is_translated for e in self.entries)

    def menu_label(self) -> str:
        return f"{self.title}   ({self.done}/{len(self.entries)})"

    # per-source behaviour ------------------------------------------------
    def context(self, e: Entry) -> tuple[str, str, list[tuple[str, str]]]:
        """(heading, badge, [(text, style)]) for the context card."""
        raise NotImplementedError

    def fit(self, e: Entry) -> tuple[str, bool]:
        """One-line size report for the current entry, and whether it is a problem."""
        raise NotImplementedError

    def check(self) -> tuple[list[str], list[str]]:
        """(errors that block building, warnings)."""
        raise NotImplementedError

    def layout(self, e: Entry, text: str) -> list[tuple[int, int]]:
        """Lines as the game shows this kind of text (character ranges into text)."""
        if e.kind in ("npc", "choice"):
            return wrap_spans(text)[0]              # dialogue window: 32-column word wrap
        if e.kind == "say":
            spans, err = say_spans(text)             # one line per speech bubble
            return spans if not err else wrap_spans(text, SAY_MAX)[0]
        return [(0, len(text))]                      # names and exit names: one line

    def build(self) -> dict[str, bytes]:
        """File name → new file contents."""
        raise NotImplementedError


def _prohibited(e: Entry, where: str) -> list[str]:
    bad = prohibited_chars(e.text)
    return [f"{where}: prohibited character(s): {', '.join(bad)}"] if bad else []


class DlgSource(Source):
    def __init__(self, path: Path):
        super().__init__()
        self.path = path
        self.title = f"Dialogue  {path.name}"
        self.sidecar = path.with_suffix(".dlgtrans.json")      # same file as the DLG Translator
        self.nodes = parse_dlg(path.read_bytes())
        for i, n in enumerate(self.nodes):
            self.entries.append(Entry(f"{i}:npc:-1", n["npc"], "NPC", False, "npc", node=i))
            for j, c in enumerate(n["choices"]):
                self.entries.append(Entry(f"{i}:choice:{j}", c, f"{j + 1}.", True, "choice", node=i, choice=j))
        self.by_pos = {(e.info["node"], e.info.get("choice", -1)): k for k, e in enumerate(self.entries)}
        self.calls: list[dict] = []        # scripts that start this dialogue (filled in by load_game)

    def _texts(self, node_idx: int) -> list[str]:
        return [e.text for e in self.entries if e.info["node"] == node_idx]

    def context(self, e):
        n = self.nodes[e.info["node"]]
        k = len(n["choices"])
        head = f"{self.path.name}  ·  NODE 0x{n['offset']:04x}  ·  {k} choice{'s' if k != 1 else ''}"
        badge = "NPC LINE" if e.kind == "npc" else f"PLAYER CHOICE {e.info['choice'] + 1}"
        lines = [(f"[NPC] {n['npc']}", "on" if e.kind == "npc" else "off")]
        for j, c in enumerate(n["choices"]):
            p = n["ptrs"][j]
            dest = f"→ END (result {p})" if p < 0 else ("→ 0x0000 (back to start)" if p == 0 else f"→ 0x{p:04x}")
            on = e.kind == "choice" and e.info["choice"] == j
            lines.append((f"[Choice {j + 1}] {c}  ", "on" if on else "off"))
            lines.append((dest, "dest"))
        return head, badge, lines

    def fit(self, e):
        texts = self._texts(e.info["node"])
        lines = node_screen_lines(texts)
        b = e.text.encode(ENCODING, errors="replace")
        bad = prohibited_chars(e.text)
        msg = f"this string: {len(b)}/{MAX_STRING} bytes   ·   node on screen: {lines}/{MAX_LINES} lines"
        if bad:
            msg += "   ·   prohibited: " + ", ".join(bad)
        return msg, bool(len(b) > MAX_STRING or lines > MAX_LINES or bad or wrap(b)[1])

    def _nodes_with_text(self):
        nodes = [dict(n, choices=list(n["choices"])) for n in self.nodes]
        for e in self.entries:
            n = nodes[e.info["node"]]
            if e.kind == "npc":
                n["npc"] = e.text
            else:
                n["choices"][e.info["choice"]] = e.text
        return nodes

    def check(self):
        return [], [f"{self.path.name} {p}" for p in check_nodes(self._nodes_with_text())]

    def build(self):
        return {self.path.name: build_dlg(self._nodes_with_text())}


# ──────────────────────────────────────────────
# Dialogue tree, HTML export and play-through (shared logic)
# ──────────────────────────────────────────────

def dialogue_walk(src: "DlgSource"):
    """Walk the dialogue breadth-first from the first line.

    Returns (index of node by file offset, home, number): each node is written out once, under
    the first choice that reaches it (home[node] = (parent node, choice) — the shallowest place);
    every other way to it becomes a link. Numbers #1, #2… follow the same order.
    """
    idx_of = {n["offset"]: i for i, n in enumerate(src.nodes)}
    home, order, queue = {0: None}, [0], [0]
    while queue:
        i = queue.pop(0)
        n = src.nodes[i]
        for j in range(n["n_active"]):
            p = n["ptrs"][j]
            if p >= 0 and idx_of.get(p) is not None and idx_of[p] not in home:
                k = idx_of[p]
                home[k] = (i, j); order.append(k); queue.append(k)
    number = {k: pos + 1 for pos, k in enumerate(order)}
    return idx_of, home, number


def dialogue_line(src: "DlgSource", node: int, choice: int = -1) -> tuple[str, bool, int]:
    """(text to show, is it translated, entry index): the translation if there is one, else the German."""
    k = src.by_pos[(node, choice)]
    e = src.entries[k]
    return (e.translation.strip() or e.original), e.is_translated, k


def dialogue_outcome(src: "DlgSource", result: int) -> str:
    """How a negative pointer ends the conversation, with the flags the calling scripts set."""
    k = -result
    parts = []
    for c in src.calls:
        if c["flags"] is None:
            continue
        f = c["flags"][k - 1] if 1 <= k <= 3 else -1
        parts.append(f"{c['where']}: " + (f"sets flag {f}" if f >= 0 else "no flag"))
    txt = f"ends · result {result}"
    if parts:
        txt += " → " + "; ".join(dict.fromkeys(parts))
    elif src.calls:
        txt += " (the calling script ignores the result)"
    return txt


def dialogue_tree(src: "DlgSource"):
    """Nested description of the whole conversation (see dialogue_walk)."""
    idx_of, home, number = dialogue_walk(src)

    def node(i):
        text, done, entry = dialogue_line(src, i)
        n = src.nodes[i]
        out = dict(node=i, num=number[i], text=text, done=done, entry=entry, choices=[])
        for j in range(n["n_active"]):
            ctext, cdone, centry = dialogue_line(src, i, j)
            p = n["ptrs"][j]
            if p < 0:
                then = ("end", p, dialogue_outcome(src, p))
            else:
                k = idx_of[p]
                if k == 0:
                    then = ("start", 0)
                elif home.get(k) == (i, j):
                    then = ("node", node(k))
                else:
                    then = ("link", k)
            out["choices"].append(dict(choice=j, text=ctext, done=cdone, entry=centry, then=then))
        return out

    return node(0), number


def _h(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def dialogue_html(sources: list, title: str) -> str:
    """One self-contained HTML page with the trees of the given dialogues."""
    body, toc = [], []
    for src in sources:
        name = src.path.stem
        root, number = dialogue_tree(src)
        done, total = src.done, len(src.entries)
        toc.append(f'<li><a href="#{_h(name)}">{_h(src.path.name)}</a> <span class="cnt">{done}/{total}</span></li>')

        def line(text, is_done):
            t = _h(text) if text else '<i class="empty">(no line)</i>'
            return t if is_done else f'<span class="de">{t}<sup>DE</sup></span>'

        def node_html(nd):
            out = [f'<div class="node" id="{_h(name)}-{nd["num"]}"><div class="npc"><span class="num">#{nd["num"]}</span>'
                   f'{line(nd["text"], nd["done"])}</div><ul>']
            for c in nd["choices"]:
                kind = c["then"][0]
                ch = f'<span class="cn">{c["choice"] + 1}.</span> {line(c["text"], c["done"])}'
                if kind == "node":
                    out.append(f'<li><details open><summary>{ch}</summary>{node_html(c["then"][1])}</details></li>')
                else:
                    if kind == "end":
                        tail = f'<div class="end">■ {_h(c["then"][2])}</div>'
                    elif kind == "start":
                        tail = f'<div class="go">↪ <a href="#{_h(name)}-1">back to the start (#1)</a></div>'
                    else:
                        k = c["then"][1]
                        t, d, _ = dialogue_line(src, k)
                        tail = (f'<div class="go">↪ <a href="#{_h(name)}-{number[k]}">#{number[k]}</a> '
                                f'<span class="peek">{_h(t[:70])}{"…" if len(t) > 70 else ""}</span></div>')
                    out.append(f'<li><div class="ch">{ch}</div>{tail}</li>')
            out.append("</ul></div>")
            return "".join(out)

        callers = ", ".join(dict.fromkeys(c["where"] for c in src.calls)) or "no script found"
        body.append(f'<section id="{_h(name)}"><h2>{_h(src.path.name)} <span class="cnt">{done}/{total} translated'
                    f'</span></h2><p class="meta">Started by: {_h(callers)} · {len(src.nodes)} nodes</p>'
                    f'{node_html(root)}</section>')
    toc_html = f'<nav><h3>Dialogues</h3><ul>{"".join(toc)}</ul></nav>' if len(sources) > 1 else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_h(title)}</title>
<style>
:root {{ --bg:#fbfaf7; --fg:#22252b; --dim:#6b7280; --line:#d9dce3; --de:#9a5b13; --deb:#fdf1e0;
        --link:#1f5fae; --end:#7a2e8a; --num:#e8ecf3; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#16181d; --fg:#e6e3dc; --dim:#8b93a3; --line:#2e333d;
        --de:#e0a45a; --deb:#2c2418; --link:#7fb0f0; --end:#d59be2; --num:#262b35; }} }}
body {{ background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, sans-serif; margin:0 auto; max-width:1000px;
       padding:24px 16px 80px; }}
h1 {{ font-size:22px; margin:0 0 4px }} h2 {{ font-size:18px; margin:40px 0 2px; border-bottom:1px solid var(--line) }}
.cnt, .meta {{ color:var(--dim); font-size:13px; font-weight:normal }}
nav ul {{ columns:4 160px; padding-left:18px }}
.legend {{ color:var(--dim); font-size:13px }}
ul {{ list-style:none; margin:4px 0 4px 10px; padding-left:16px; border-left:1px solid var(--line) }}
.npc {{ font-weight:600; margin:6px 0 2px }} .num {{ background:var(--num); border-radius:4px; padding:0 6px;
       margin-right:8px; font:600 12px ui-monospace, monospace }}
summary {{ cursor:pointer }} .cn {{ color:var(--dim); font-family:ui-monospace, monospace }}
.de {{ color:var(--de); background:var(--deb); border-radius:3px; padding:0 2px }} .de sup {{ font-size:9px; margin-left:3px }}
.go {{ margin-left:22px; color:var(--dim) }} .go a {{ color:var(--link) }} .peek {{ font-style:italic }}
.end {{ margin-left:22px; color:var(--end); font-size:13px }} .empty {{ color:var(--dim) }}
:target > .npc {{ outline:2px solid var(--link); border-radius:4px }}
</style></head><body>
<h1>{_h(title)}</h1>
<p class="legend">Each exchange is written out once, where the player first reaches it; ↪ links lead to exchanges shown
elsewhere. <span class="de">German<sup>DE</sup></span> = not translated yet. ■ = the conversation ends.</p>
{toc_html}{"".join(body)}
</body></html>
"""


def dialogue_callers(sources: list) -> dict[str, list[dict]]:
    """DLG name → scripts that start it: CA (Esc allowed, result ignored) and DA (result sets flags)."""
    calls: dict[str, list[dict]] = {}

    def scan(d, start, where):
        for at, op in script_ops(d, start):
            if op in (0xCA, 0xDA):
                name = fixed_text(d[at + 1:at + 9]).upper()
                flags = list(struct.unpack_from("<3h", d, at + 9)) if op == 0xDA else None
                calls.setdefault(name, []).append(dict(where=where, flags=flags))

    for s in sources:
        if isinstance(s, ItemsSource):
            for it in s.items:
                for verb, at in it["script_at"].items():
                    scan(s.data, at, f"#{it['index']} {it['name'] or 'item'} ({verb})")
        elif isinstance(s, ProcsSource):
            for i, o in enumerate(s.offsets):
                if o:
                    scan(s.data, o, f"procedure {i}")
    return calls


class ItemsSource(Source):
    """ITEMS.ITM — object names (16-byte fields) and the SAY lines of the verb scripts."""
    VERBS = {3: "look", 5: "take", 6: "use", 7: "walk", 8: "talk"}

    def __init__(self, path: Path):
        super().__init__()
        self.path = path
        self.title = "Items  ITEMS.ITM  (names + speech)"
        self.sidecar = path.with_name("ITEMS.trans.json")
        self.data = d = path.read_bytes()
        self.offsets = list(struct.unpack_from("<256I", d, 0))
        self.items = []
        for idx, p in enumerate(self.offsets):
            if not p:
                break
            item = dict(index=idx, name=None, scripts={}, script_at={})
            while d[p]:
                t = d[p]
                if t == 1:
                    kind = d[p + 1]; p += 6
                    if kind == 0:
                        p += 2 + struct.unpack_from("<H", d, p)[0]
                elif t == 2:
                    item["name_at"] = p + 2; item["name"] = fixed_text(d[p + 2:p + 18]); p += 18
                elif t in self.VERBS:
                    ops = list(script_ops(d, p + 1))
                    item["scripts"][self.VERBS[t]] = ops
                    item["script_at"][self.VERBS[t]] = p + 1
                    p = ops[-1][0] + 1
                elif t == 4:
                    p += 2 + 1024
                elif t == 9:
                    p += 2
                else:
                    raise ValueError(f"ITEMS.ITM item {idx}: unknown tag {t} at {p:#x}")
            self.items.append(item)
        self.names = {it["index"]: it["name"] for it in self.items if it["name"]}
        for it in self.items:
            title = it["name"] or "(no name)"
            if it["name"]:                         # 4 items have an empty name field: nothing to translate
                self.entries.append(Entry(f"name@{it['name_at']:x}", it["name"], f"#{it['index']:<3} name",
                                          False, "name", item=it["index"], at=it["name_at"]))
            else:
                has_say = any(op == OP_SAY and fixed_text(d[at + 1:at + 25])
                              for ops in it["scripts"].values() for at, op in ops)
                if not has_say:
                    continue
            for verb, ops in it["scripts"].items():
                for at, op in ops:
                    if op == OP_SAY and fixed_text(d[at + 1:at + 25]):    # skip the one blank bubble
                        self.entries.append(Entry(f"say@{at:x}", fixed_text(d[at + 1:at + 25]), verb,
                                                  bool(it["name"]), "say", item=it["index"], at=at,
                                                  verb=verb, item_title=title))
        by_item = {}
        for e in self.entries:
            by_item.setdefault(e.info["item"], []).append(e)
        self.by_item = by_item

    def context(self, e):
        it = self.items[e.info["item"]]
        head = f"ITEMS.ITM  ·  ITEM #{it['index']}  ·  {it['name'] or '(no name)'}"
        if e.kind == "name":
            badge = f"OBJECT NAME  (max {NAME_MAX})"
        else:
            badge = f"SAY · {e.info['verb'].upper()}  (bubble {SAY_MAX})"
        if e.kind == "say":
            start = it["script_at"][e.info["verb"]]
            lines = [(f"{e.info['verb'].upper()} script:", "dest")]
            return head, badge, lines + script_listing(self.data, start, e.info["at"], self.names)
        lines = []
        for o in self.by_item[e.info["item"]]:
            tag = "name" if o.kind == "name" else o.info["verb"]
            lines.append((f"[{tag}] {o.original}", "on" if o is e else "off"))
        return head, badge, lines

    def fit(self, e):
        bad = prohibited_chars(e.text)
        if e.kind == "name":
            n = len(e.text.encode(ENCODING, errors="replace"))
            msg, over = f"{n}/{NAME_MAX} characters", n > NAME_MAX
        else:
            parts, err = split_say(e.text)
            if err:
                msg, over = err, True
            elif len(parts) == 1:
                msg, over = f"{len(e.text)}/{SAY_MAX} characters · 1 bubble", False
            else:
                msg = f"{len(e.text)} characters · {len(parts)} bubbles: " + " | ".join(parts)
                over = False
        if bad:
            msg += "   ·   prohibited: " + ", ".join(bad)
        return msg, over or bool(bad)

    def check(self):
        errors = []
        for e in self.entries:
            where = f"ITEMS.ITM item {e.info['item']} {e.label.split()[-1]}"
            errors += _prohibited(e, where)
            if e.kind == "name" and len(e.text.encode(ENCODING, errors="replace")) > NAME_MAX:
                errors.append(f"{where}: '{e.text}' is longer than {NAME_MAX} characters")
            if e.kind == "say":
                errors += [f"{where}: {err}" for err in [split_say(e.text)[1]] if err]
        return errors, []

    def build(self):
        d = self.data; patches = []
        for e in self.entries:
            if not e.is_translated:
                continue
            at = e.info["at"]
            if e.kind == "name":
                patches.append((at, NAME_MAX, e.text.encode(ENCODING).ljust(NAME_MAX, b"\0")))
            else:
                x, y = struct.unpack_from("<hh", d, at + 25)
                parts, _ = split_say(e.text)
                code = b""
                for k, part in enumerate(parts):
                    code += say_bytes(part, x, y)
                    if k < len(parts) - 1:
                        code += bytes([OP_WAIT]) + struct.pack("<h", SPLIT_WAIT_TICKS)
                patches.append((at, VM_LEN[OP_SAY], code))
        new, remap = apply_patches(d, patches)
        new = bytearray(new)
        struct.pack_into("<256I", new, 0, *[remap(o) if o else 0 for o in self.offsets])
        return {self.path.name: bytes(new)}


class ProcsSource(Source):
    """PROCS.CMD — SAY lines of the 32 global scripts."""
    def __init__(self, path: Path):
        super().__init__()
        self.path = path
        self.title = "Scripts  PROCS.CMD  (speech)"
        self.sidecar = path.with_name("PROCS.trans.json")
        self.data = d = path.read_bytes()
        self.offsets = list(struct.unpack_from("<256I", d, 0))
        items = find_file(path.parent, "ITEMS.ITM")
        try:
            self.names = ItemsSource(items).names if items else {}
        except Exception:
            self.names = {}
        for i, o in enumerate(self.offsets):
            if not o:
                continue
            for at, op in script_ops(d, o):
                if op == OP_SAY and fixed_text(d[at + 1:at + 25]):
                    self.entries.append(Entry(f"say@{at:x}", fixed_text(d[at + 1:at + 25]), f"proc {i}",
                                              False, "say", proc=i, at=at))

    def context(self, e):
        head = f"PROCS.CMD  ·  PROCEDURE {e.info['proc']}"
        lines = script_listing(self.data, self.offsets[e.info["proc"]], e.info["at"], self.names)
        return head, f"SAY  (bubble {SAY_MAX})", lines

    fit = ItemsSource.fit

    def check(self):
        errors = []
        for e in self.entries:
            where = f"PROCS.CMD proc {e.info['proc']}"
            errors += _prohibited(e, where)
            errors += [f"{where}: {err}" for err in [split_say(e.text)[1]] if err]
        return errors, []

    def build(self):
        d = self.data; patches = []
        for e in self.entries:
            if not e.is_translated:
                continue
            at = e.info["at"]; x, y = struct.unpack_from("<hh", d, at + 25)
            parts, _ = split_say(e.text); code = b""
            for k, part in enumerate(parts):
                code += say_bytes(part, x, y)
                if k < len(parts) - 1:
                    code += bytes([OP_WAIT]) + struct.pack("<h", SPLIT_WAIT_TICKS)
            patches.append((at, VM_LEN[OP_SAY], code))
        new, remap = apply_patches(d, patches)
        new = bytearray(new)
        struct.pack_into("<256I", new, 0, *[remap(o) if o else 0 for o in self.offsets])
        return {self.path.name: bytes(new)}


def rom_exits(d: bytes):
    """Yield (label_offset, room, label) for every EXIT (opcode 4) in a room file (07a2:0086)."""
    p = 0
    fixed = {1: 1, 2: 13, 3: 9, 5: 0, 7: 10, 8: 13, 9: 2, 10: 0, 11: 13, 12: 3, 13: 7, 14: 0, 15: 2, 16: 3, 18: 4, 19: 0, 20: 0}
    while p < len(d):
        op = d[p]; p += 1
        if op == 4:
            room = fixed_text(d[p + 9:p + 17])
            yield p + 17, room, fixed_text(d[p + 17:p + 33])
            p += 1 + 8 + 8 + 16 + 8
        elif op == 6:
            p += 3 + struct.unpack_from("<h", d, p + 1)[0]
        elif op in fixed:
            p += fixed[op]
        else:
            raise ValueError(f"invalid room opcode {op:#x} at {p - 1:#x}")


class ExitsSource(Source):
    """Exit names from every .ROM file (16-byte fields)."""
    def __init__(self, folder: Path):
        super().__init__()
        self.title = "Room exits  *.ROM  (names)"
        self.sidecar = folder / "ROOMS.trans.json"
        self.files = {}
        for f in sorted(folder.glob("*.ROM")) + sorted(folder.glob("*.rom")):
            if f.name in self.files:
                continue
            d = f.read_bytes(); self.files[f.name] = (f, d)
            for at, room, label in rom_exits(d):
                if label:
                    self.entries.append(Entry(f"{f.name}@{at:x}", label, f"{f.stem:<9}", False, "label",
                                              file=f.name, at=at, room=room))

    def context(self, e):
        head = f"{e.info['file']}  ·  EXIT"
        lines = [(f"[{o.info['file']}] {o.original}  ", "on" if o is e else "off") for o in self.entries if o.info["file"] == e.info["file"]]
        lines.insert(1 if len(lines) else 0, (f"leads to room: {e.info['room']}", "dest"))
        return head, f"EXIT NAME  (max {LABEL_MAX})", lines

    def fit(self, e):
        n = len(e.text.encode(ENCODING, errors="replace")); bad = prohibited_chars(e.text)
        msg = f"{n}/{LABEL_MAX} characters" + ("   ·   prohibited: " + ", ".join(bad) if bad else "")
        return msg, n > LABEL_MAX or bool(bad)

    def check(self):
        errors = []
        for e in self.entries:
            where = f"{e.info['file']} exit"
            errors += _prohibited(e, where)
            if len(e.text.encode(ENCODING, errors="replace")) > LABEL_MAX:
                errors.append(f"{where}: '{e.text}' is longer than {LABEL_MAX} characters")
        return errors, []

    def build(self):
        out = {}
        for name, (f, d) in self.files.items():
            ents = [e for e in self.entries if e.info["file"] == name and e.is_translated]
            if not ents:
                continue
            b = bytearray(d)
            for e in ents:
                b[e.info["at"]:e.info["at"] + 16] = e.text.encode(ENCODING).ljust(16, b"\0")
            out[name] = bytes(b)
        return out



# ──────────────────────────────────────────────
# ENGINE.EXE — texts inside the program (DGROUP strings, edited in place)
# ──────────────────────────────────────────────

EXE_DGROUP = 0x12C70          # file offset of DGROUP (MZ header 0xE00 + segment 11E7h)
EXE_SIZE = 88902
# Strings the code finds by their own address: (DGROUP offset, label, kind, note). They must
# stay where they are, so each one is limited to the length of the German original.
EXE_SINGLE = [
    (0x864, "Walk",  "verb", "verb button label (hover)"),
    (0x869, "Look",  "verb", "verb button label (hover)"),
    (0x86F, "Take",  "verb", "verb button label (hover)"),
    (0x874, "Use",   "verb", "verb button label (hover)"),
    (0x87C, "Talk",  "verb", "verb button label (hover)"),
    (0x883, "Load",  "verb", "verb button label (hover)"),
    (0x889, "Save",  "verb", "verb button label (hover)"),
    (0x4B2, "reply", "reply", "Tom's default answer when an object has no script for the verb"),
    (0x4C9, "reply", "reply", "Tom's default answer when an object has no script for the verb"),
    (0x4E0, "reply", "reply", "Tom's default answer when an object has no script for the verb"),
    (0x4F0, "reply", "reply", "Tom's default answer when an object has no script for the verb"),
    (0x506, "reply", "reply", "Tom's default answer when an object has no script for the verb"),
]
# Menus: a DLG-style node (int32 result[3], prompt, choices…, empty string) reached through a
# far pointer, drawn by the dialogue window. Its strings are read one after another, so they
# may be moved inside the node as long as everything fits in the original space.
EXE_MENUS = [
    (0x334, "Save menu"), (0x3B8, "Load menu"), (0x51B, "Quit question"),
    (0x54F, "Save failed"), (0x58B, "Load failed"),
]
REPLY_MAX = 24                # 0a1f:0b9b copies 24 bytes into the bubble
VERB_LABEL_MAX = 16           # the hover label copies 16 bytes (0a1f:0827)


def exe_encode(text: str) -> bytes:
    return text.replace("\\n", "\n").encode(ENCODING, errors="replace")


def exe_decode(b: bytes) -> str:
    return b.decode(ENCODING).replace("\n", "\\n")


class ExeSource(Source):
    """ENGINE.EXE — verb names, default replies and the Save/Load/Quit menus."""
    def __init__(self, path: Path):
        super().__init__()
        self.path = path
        self.title = "Program  ENGINE.EXE  (verbs, replies, menus)"
        self.sidecar = path.with_name("ENGINE.trans.json")
        self.data = d = path.read_bytes()
        if len(d) != EXE_SIZE or d[EXE_DGROUP + 0x864:EXE_DGROUP + 0x869] != b"Gehe\0":
            raise ValueError(f"{path.name} is not the original German ENGINE.EXE (translated already?) - "
                             "put the untouched file in the game folder to translate it.")
        self.slots = {}
        for off, label, kind, note in EXE_SINGLE:
            at = EXE_DGROUP + off
            end = d.index(b"\0", at)
            self.slots[off] = end - at                       # characters that fit
            self.entries.append(Entry(f"str@{off:04x}", exe_decode(d[at:end]), label, False, kind,
                                      off=off, note=note))
        self.menus = []
        for node, name in EXE_MENUS:
            p = EXE_DGROUP + node + 12
            start, texts = p, []
            while True:
                end = d.index(b"\0", p)
                if end == p:
                    break
                texts.append(d[p:end]); p = end + 1
            self.menus.append(dict(node=node, name=name, start=start, size=p + 1 - start, count=len(texts)))
            for k, t in enumerate(texts):
                self.entries.append(Entry(f"menu@{node:04x}.{k}", exe_decode(t), name if k == 0 else f"{k}.",
                                          k > 0, "npc" if k == 0 else "choice", menu=len(self.menus) - 1, idx=k))

    def _menu_entries(self, m):
        return [e for e in self.entries if e.info.get("menu") == m]

    def _menu_bytes(self, m) -> int:
        return sum(len(exe_encode(e.text)) + 1 for e in self._menu_entries(m)) + 1

    def context(self, e):
        if "menu" in e.info:
            m = self.menus[e.info["menu"]]
            lines = [(o.original, "on" if o is e else "off") if o.info["idx"] == 0 else
                     (f"   {o.info['idx']}. {o.original}", "on" if o is e else "off") for o in self._menu_entries(e.info["menu"])]
            lines.append((f"\nstrings are read one after another, so they may move inside the node's {m['size']} bytes", "dest"))
            return f"ENGINE.EXE  ·  {m['name'].upper()}", "MENU (dialogue window)", lines
        same = [o for o in self.entries if o.kind == e.kind]
        lines = [(o.original, "on" if o is e else "off") for o in same]
        lines.append(("\n" + e.info["note"] + " · fixed address, so it can't grow past the German length", "dest"))
        return f"ENGINE.EXE  ·  {e.kind.upper()}S", f"{e.kind.upper()}  (max {self._limit(e)})", lines

    def _limit(self, e) -> int:
        n = self.slots[e.info["off"]]
        if e.kind == "reply":
            n = min(n, REPLY_MAX)
        if e.kind == "verb":
            n = min(n, VERB_LABEL_MAX - 1)
        return n

    def _problems(self, e) -> list[str]:
        out = []
        if "menu" in e.info:
            m = self.menus[e.info["menu"]]
            used = self._menu_bytes(e.info["menu"])
            if used > m["size"]:
                out.append(f"{m['name']}: needs {used} bytes, the node has {m['size']}")
            lines, bad = wrap(exe_encode(e.text))
            if bad:
                out.append(f"{m['name']}: a word is longer than {WRAP_COLS} characters")
        else:
            n = len(exe_encode(e.text))
            if n > self._limit(e):
                out.append(f"'{e.text}' is {n} characters, the limit is {self._limit(e)}")
        return out

    def fit(self, e):
        bad = prohibited_chars(e.text.replace("\\n", " "))
        if "menu" in e.info:
            m = self.menus[e.info["menu"]]
            ents = self._menu_entries(e.info["menu"])
            lines = node_screen_lines([o.text for o in ents])
            msg = (f"node: {self._menu_bytes(e.info['menu'])}/{m['size']} bytes   ·   "
                   f"window: {lines}/{MAX_LINES} lines")
            over = bool(self._problems(e)) or lines > MAX_LINES
        else:
            msg = f"{len(exe_encode(e.text))}/{self._limit(e)} characters"
            over = bool(self._problems(e))
        if bad:
            msg += "   ·   prohibited: " + ", ".join(bad)
        return msg, over or bool(bad)

    def check(self):
        errors, warnings = [], []
        for e in self.entries:
            errors += _prohibited(e, "ENGINE.EXE")
            errors += [f"ENGINE.EXE: {p}" for p in self._problems(e)]
        for i, m in enumerate(self.menus):
            lines = node_screen_lines([o.text for o in self._menu_entries(i)])
            if lines > MAX_LINES:
                warnings.append(f"ENGINE.EXE {m['name']}: {lines} lines, the window shows {MAX_LINES}")
        return sorted(set(errors)), warnings

    def build(self):
        b = bytearray(self.data)
        for e in self.entries:
            if "off" in e.info and e.is_translated:
                at = EXE_DGROUP + e.info["off"]; room = self.slots[e.info["off"]] + 1
                b[at:at + room] = exe_encode(e.text).ljust(room, b"\0")
        for i, m in enumerate(self.menus):
            ents = self._menu_entries(i)
            if not any(o.is_translated for o in ents):
                continue
            blob = b"".join(exe_encode(o.text) + b"\0" for o in ents) + b"\0"
            b[m["start"]:m["start"] + m["size"]] = blob.ljust(m["size"], b"\0")
        return {self.path.name: bytes(b)}

    def import_translated(self, translated: bytes) -> int:
        """Take the texts from an ENGINE.EXE that was already translated by hand (same layout)."""
        n = 0
        for e in self.entries:
            if "off" in e.info:
                at = EXE_DGROUP + e.info["off"]
                t = exe_decode(translated[at:translated.index(b"\0", at)])
            else:
                m = self.menus[e.info["menu"]]; p = m["start"]
                for _ in range(e.info["idx"] + 1):
                    end = translated.index(b"\0", p); t = exe_decode(translated[p:end]); p = end + 1
            if t != e.original:
                e.translation = t; n += 1
        return n


def find_file(folder: Path, name: str) -> Path | None:
    for f in folder.iterdir():
        if f.name.lower() == name.lower():
            return f
    return None


LOAD_WARNINGS: list[str] = []


def load_game(folder: Path) -> list[Source]:
    sources: list[Source] = []
    for n in ("ITEMS.ITM", "PROCS.CMD"):
        f = find_file(folder, n)
        if f:
            sources.append(ItemsSource(f) if n == "ITEMS.ITM" else ProcsSource(f))
    if any(folder.glob("*.ROM")) or any(folder.glob("*.rom")):
        sources.append(ExitsSource(folder))
    exe = find_file(folder, "ENGINE.EXE")
    LOAD_WARNINGS.clear()
    if exe:
        try:
            sources.append(ExeSource(exe))
        except ValueError as ex:
            LOAD_WARNINGS.append(str(ex))
    dlgs = sorted({f.name: f for f in list(folder.glob("*.DLG")) + list(folder.glob("*.dlg"))}.values())
    sources += [DlgSource(f) for f in dlgs]
    callers = dialogue_callers(sources)
    for s in sources:
        if isinstance(s, DlgSource):
            s.calls = callers.get(s.path.stem.upper(), [])
    for s in sources:
        s.load_sidecar()
    return sources


# ──────────────────────────────────────────────
# Main window (tkinter)
# ──────────────────────────────────────────────

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_NAME = "Tom Long Translator"
BG = "#1a1d23"; BG_SIDE = "#141720"; BG_CARD = "#1e2230"; BG_EDIT = "#12151a"; BG_SRC = "#0e1015"
FG = "#f0ece4"; FG_DIM = "#8090a8"; FG_MUTED = "#7080a0"; BORDER = "#3d4455"
GREEN = "#4caf50"; GREY = "#909ab0"; SEL = "#1e3050"; ACCENT = "#3a80d0"; HEAD_ROW = "#232a3a"


def _mono_font() -> str:
    """First fixed-width font available on this system."""
    from tkinter import font as tkfont
    have = set(tkfont.families())
    for f in ("Consolas", "Cascadia Mono", "Lucida Console", "DejaVu Sans Mono", "Liberation Mono",
              "Menlo", "Courier New"):
        if f in have:
            return f
    return "Courier"


class DialogueTreeWindow(tk.Toplevel):
    """The whole conversation of one .DLG as an expandable tree (translation first, else German)."""
    def __init__(self, app: "TomLongTranslator", src: DlgSource):
        super().__init__(app)
        self.app, self.src = app, src
        self.configure(bg=BG)
        self.geometry("980x720")
        style = ttk.Style(self)
        style.configure("Dlg.Treeview", background=BG_EDIT, fieldbackground=BG_EDIT, foreground=FG,
                        rowheight=22, font=("Segoe UI", 10), borderwidth=0, indent=12)
        style.map("Dlg.Treeview", background=[("selected", SEL)], foreground=[("selected", "#ffffff")])

        bar = tk.Frame(self, bg=BG); bar.pack(fill="x", padx=12, pady=(10, 6))
        for text, cmd in (("Expand all", lambda: self._open_all(True)), ("Collapse all", lambda: self._open_all(False)),
                          ("Play through…", self._play), ("Export HTML…", self._export),
                          ("Export all dialogues…", self._export_all)):
            app._button(bar, text, cmd).pack(side="left", padx=(0, 6))
        self.info = tk.Label(self, bg=BG, fg=FG_DIM, font=("Segoe UI", 9), anchor="w")
        self.info.pack(fill="x", padx=12, pady=(0, 4))

        frame = tk.Frame(self, bg=BG); frame.pack(fill="both", expand=True, padx=12)
        self.tree = ttk.Treeview(frame, show="tree", style="Dlg.Treeview", selectmode="browse")
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y"); self.tree.pack(side="left", fill="both", expand=True)
        self.tree.tag_configure("npc", foreground="#e8e2d4", font=("Segoe UI", 10, "bold"))
        self.tree.tag_configure("choice", foreground="#b8c4d8")
        self.tree.tag_configure("de", foreground="#e0a45a")
        self.tree.tag_configure("link", foreground="#7fb0f0")
        self.tree.tag_configure("end", foreground="#d59be2")
        self.detail = tk.Label(self, bg=BG_CARD, fg=FG, font=("Segoe UI", 10), anchor="w", justify="left",
                               wraplength=940, padx=12, pady=8)
        self.detail.pack(fill="x", padx=12, pady=(6, 4))
        tk.Label(self, bg=BG, fg=FG_MUTED, font=("Segoe UI", 9), anchor="w",
                 text="Double-click a line to edit it · double-click a ↪ link to go to that exchange · "
                      "orange = not translated yet (German shown)").pack(fill="x", padx=12, pady=(0, 8))
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", self._on_double)
        self.what: dict[str, tuple] = {}
        self.refresh()

    def refresh(self):
        opened = {i for i in self.what if self.tree.exists(i) and self.tree.item(i, "open")}
        first = not self.what
        sel = self.tree.selection()
        top = self.tree.yview()[0]
        self.tree.delete(*self.tree.get_children())
        self.what.clear()
        root, number = dialogue_tree(self.src)
        self.number = number
        self.node_iid = {}

        def add_node(parent, nd):
            iid = f"n{nd['num']}"
            self.node_iid[nd["node"]] = iid
            txt = nd["text"] or "(no line)"
            self.tree.insert(parent, "end", iid=iid, text=f"#{nd['num']}   {txt}",
                             tags=("npc",) if nd["done"] else ("npc", "de"), open=first or iid in opened)
            self.what[iid] = ("entry", nd["entry"], txt)
            for c in nd["choices"]:
                cid = f"{iid}c{c['choice'] + 1}"
                self.tree.insert(iid, "end", iid=cid, text=f"{c['choice'] + 1}.  {c['text']}",
                                 tags=("choice",) if c["done"] else ("choice", "de"), open=first or cid in opened)
                self.what[cid] = ("entry", c["entry"], c["text"])
                kind = c["then"][0]
                if kind == "node":
                    add_node(cid, c["then"][1])
                elif kind == "end":
                    self.tree.insert(cid, "end", iid=cid + "e", text=f"■  {c['then'][2]}", tags=("end",))
                    self.what[cid + "e"] = ("end", None, c["then"][2])
                else:
                    k = c["then"][1]
                    t, _, _ = dialogue_line(self.src, k)
                    label = "↪  back to the start (#1)" if kind == "start" else f"↪  #{number[k]}   {t}"
                    self.tree.insert(cid, "end", iid=cid + "g", text=label, tags=("link",))
                    self.what[cid + "g"] = ("link", k, label)

        add_node("", root)
        self.title(f"Dialogue tree — {self.src.path.name}")
        callers = ", ".join(dict.fromkeys(c["where"] for c in self.src.calls)) or "no script found"
        self.info.configure(text=f"{self.src.done}/{len(self.src.entries)} translated · {len(self.src.nodes)} exchanges · "
                                 f"started by {callers}")
        if sel and self.tree.exists(sel[0]):
            self.tree.selection_set(sel[0])
        self.tree.yview_moveto(top)

    def _open_all(self, state: bool):
        for iid in self.what:
            if self.tree.exists(iid):
                self.tree.item(iid, open=state)

    def _on_select(self, _e=None):
        sel = self.tree.selection()
        if sel and sel[0] in self.what:
            self.detail.configure(text=self.what[sel[0]][2])

    def _on_double(self, _e=None):
        sel = self.tree.selection()
        if not sel or sel[0] not in self.what:
            return "break"
        kind, val, _ = self.what[sel[0]]
        if kind == "entry":
            self.app.show_entry(self.src, val)
        elif kind == "link":
            target = self.node_iid[val]
            parent = self.tree.parent(target)
            while parent:
                self.tree.item(parent, open=True); parent = self.tree.parent(parent)
            self.tree.selection_set(target); self.tree.see(target); self.tree.focus(target)
        return "break"

    def _play(self):
        self.app.open_play(self.src)

    def _export(self):
        self.app.export_trees([self.src], f"{self.src.path.stem}_tree.html", self)

    def _export_all(self):
        self.app.export_trees([s for s in self.app.sources if isinstance(s, DlgSource)], "dialogue_trees.html", self)


class PlayThroughWindow(tk.Toplevel):
    """Walk through a conversation like in the game: the NPC line, then click a choice."""
    def __init__(self, app: "TomLongTranslator", src: DlgSource):
        super().__init__(app)
        self.app, self.src = app, src
        self.configure(bg=BG)
        self.resizable(False, False)
        self.number = dialogue_walk(src)[2]
        self.idx_of = dialogue_walk(src)[0]
        self.node, self.history, self.ended = 0, [], None
        mono = _mono_font()
        self.head = tk.Label(self, bg=BG, fg="#7aaae0", font=("Segoe UI", 9, "bold"), anchor="w")
        self.head.pack(fill="x", padx=14, pady=(12, 4))
        self.screen = tk.Text(self, width=WRAP_COLS, height=MAX_LINES, bg="#000000", fg="#d8d8d8", bd=0,
                              font=(mono, 14), padx=14, pady=12, wrap="none", cursor="arrow",
                              highlightthickness=1, highlightbackground=BORDER)
        self.screen.pack(padx=14)
        self.screen.tag_configure("npc", foreground="#ffffff")
        self.screen.tag_configure("c0", foreground="#9fc3f0")
        self.screen.tag_configure("c1", foreground="#c8d8ec")
        self.screen.tag_configure("c2", foreground="#9fc3f0")
        self.screen.tag_configure("de", foreground="#e0a45a")
        self.screen.tag_configure("hover", background="#1e3a66", foreground="#ffffff")
        self.screen.tag_configure("end", foreground="#d59be2", font=(mono, 11), wrap="word")
        self.status = tk.Label(self, bg=BG, fg=FG_DIM, font=("Segoe UI", 9), anchor="w")
        self.status.pack(fill="x", padx=14, pady=(6, 0))
        bar = tk.Frame(self, bg=BG); bar.pack(fill="x", padx=14, pady=(8, 12))
        self.back_btn = app._button(bar, "← Back", self._back); self.back_btn.pack(side="left")
        app._button(bar, "Restart", self._restart).pack(side="left", padx=6)
        app._button(bar, "Edit this line", self._edit).pack(side="right")
        self.render()

    def render(self):
        s = self.screen
        s.configure(state="normal"); s.delete("1.0", "end")
        n = self.src.nodes[self.node]
        text, done, _ = dialogue_line(self.src, self.node)
        lines = 0

        def put(t, tags):
            nonlocal lines
            rows, _ = wrap(t.encode(ENCODING, errors="replace"))
            for r in rows or [b""]:
                s.insert("end", r.decode(ENCODING) + "\n", tags); lines += 1

        if text:
            put(text, ("npc",) if done else ("npc", "de"))
        s.insert("end", "\n"); lines += 1
        untranslated = 0 if done else 1
        if self.ended is None:
            for j in range(n["n_active"]):
                ct, cdone, _ = dialogue_line(self.src, self.node, j)
                untranslated += not cdone
                tag = f"c{j}"
                if j:
                    s.insert("end", "\n"); lines += 1    # the game leaves a blank line after every choice
                put(ct, (tag,) if cdone else (tag, "de"))
                s.tag_raise("de")
                s.tag_bind(tag, "<Enter>", lambda e, t=tag: s.tag_add("hover", f"{t}.first", f"{t}.last"))
                s.tag_bind(tag, "<Leave>", lambda e: s.tag_remove("hover", "1.0", "end"))
                s.tag_bind(tag, "<Button-1>", lambda e, j=j: self._choose(j))
        else:
            ct, cdone, _ = dialogue_line(self.src, self.node, self.ended_choice)
            put(ct, ("hover",) if cdone else ("hover", "de"))
            s.insert("end", f"\n— {self.ended} —\n", ("end",))
        s.configure(state="disabled")
        self.title(f"Play through — {self.src.path.name}")
        self.head.configure(text=f"{self.src.path.name}   ·   exchange #{self.number.get(self.node, '?')}")
        over = f"   ·   too long: {lines}/{MAX_LINES} lines!" if lines > MAX_LINES else f"   ·   {lines}/{MAX_LINES} lines"
        self.status.configure(text=("click a choice" if self.ended is None else "conversation over") + over +
                              (f"   ·   {untranslated} line(s) still German" if untranslated else ""),
                              fg="#e07070" if lines > MAX_LINES else FG_DIM)
        self.back_btn.configure(state="normal" if self.history else "disabled")

    def _choose(self, j: int):
        p = self.src.nodes[self.node]["ptrs"][j]
        self.history.append((self.node, self.ended))
        if p < 0:
            self.ended, self.ended_choice = dialogue_outcome(self.src, p), j
        else:
            self.node = self.idx_of[p]
        self.render()

    def _back(self):
        if self.history:
            self.node, self.ended = self.history.pop()
            self.render()

    def _restart(self):
        self.node, self.history, self.ended = 0, [], None
        self.render()

    def _edit(self):
        self.app.show_entry(self.src, self.src.by_pos[(self.node, -1)])



class TomLongTranslator(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1300x760")
        self.minsize(1100, 640)
        self.configure(bg=BG)

        self.game_dir: Path | None = None
        self.out_dir: Path | None = None
        self.sources: list[Source] = []
        self.src: Source | None = None
        self.current_idx = 0
        self._dirty = False
        self._loading = False

        self.views: list = []            # open dialogue-tree / play-through windows
        self._view_job = None
        self._build_ui()
        self._set_state("empty")
        self.bind_all("<Control-s>", lambda e: self._build_all())
        self.bind_all("<Control-S>", lambda e: self._build_all())
        self.bind_all("<Control-Right>", lambda e: (self._next(), "break")[1])
        self.bind_all("<Control-Left>", lambda e: (self._prev(), "break")[1])
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI construction ──────────────────────────────────

    def _button(self, parent, text, cmd, primary=False):
        return tk.Button(parent, text=text, command=cmd, relief="flat", bd=0, padx=14, pady=6,
                         bg="#1e4a7a" if primary else "#2c3140", fg="#ffffff" if primary else FG,
                         activebackground="#255a90" if primary else "#3a4155", activeforeground=FG,
                         disabledforeground="#556", highlightthickness=1, highlightbackground=BORDER,
                         font=("Segoe UI", 10), cursor="hand2")

    def _build_ui(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Dlg.Horizontal.TProgressbar", troughcolor="#2c3140", background=ACCENT,
                        bordercolor=BG_SIDE, lightcolor=ACCENT, darkcolor=ACCENT, thickness=6)
        style.configure("Dlg.Vertical.TScrollbar", background=BORDER, troughcolor=BG_SIDE,
                        bordercolor=BG_SIDE, arrowcolor=FG_DIM)
        style.configure("Src.TCombobox", fieldbackground="#2c3140", background="#2c3140", foreground=FG,
                        arrowcolor=FG, bordercolor=BORDER)
        style.map("Src.TCombobox", fieldbackground=[("readonly", "#2c3140")], foreground=[("readonly", FG)],
                  selectbackground=[("readonly", "#2c3140")], selectforeground=[("readonly", FG)])
        self.option_add("*TCombobox*Listbox.background", BG_SIDE)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", SEL)
        self.option_add("*TCombobox*Listbox.font", ("Segoe UI", 10))

        self.status_label = tk.Label(self, text="Open the game folder to begin  ·  Ctrl+← / Ctrl+→ to navigate  ·  Ctrl+S to build",
                                     bg=BG_SIDE, fg="#a0a8b8", font=("Segoe UI", 9), anchor="w", padx=8, pady=3)
        self.status_label.pack(side="bottom", fill="x")

        # ── Left sidebar ──
        side = tk.Frame(self, bg=BG_SIDE, width=420)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)

        head = tk.Frame(side, bg=BG_SIDE)
        head.pack(fill="x", padx=12, pady=(14, 10))
        self.open_btn = self._button(head, "Open game folder…", self._open_folder)
        self.open_btn.pack(fill="x")
        self.src_var = tk.StringVar()
        self.src_box = ttk.Combobox(head, textvariable=self.src_var, state="disabled", style="Src.TCombobox",
                                    font=("Segoe UI", 10), height=30)
        self.src_box.pack(fill="x", pady=(10, 0))
        self.src_box.bind("<<ComboboxSelected>>", self._on_source_selected)
        self.progress = ttk.Progressbar(head, style="Dlg.Horizontal.TProgressbar", mode="determinate")
        self.progress.pack(fill="x", pady=(10, 4))
        self.progress_label = tk.Label(head, text="No game folder open", bg=BG_SIDE, fg=FG_DIM,
                                       font=("Segoe UI", 9), anchor="w")
        self.progress_label.pack(fill="x")

        lst = tk.Frame(side, bg=BG_SIDE)
        lst.pack(fill="both", expand=True, padx=6, pady=6)
        self.listbox = tk.Listbox(lst, bg=BG_SIDE, fg=GREY, selectbackground=SEL, selectforeground=FG,
                                  highlightthickness=0, bd=0, activestyle="none", font=("Segoe UI", 10),
                                  exportselection=False)
        sb = ttk.Scrollbar(lst, orient="vertical", command=self.listbox.yview, style="Dlg.Vertical.TScrollbar")
        self.listbox.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.listbox.pack(side="left", fill="both", expand=True)
        self.listbox.bind("<<ListboxSelect>>", self._on_list_select)

        # ── Editor ──
        ed = tk.Frame(self, bg=BG)
        ed.pack(side="left", fill="both", expand=True, padx=24, pady=(20, 8))

        card = tk.Frame(ed, bg=BG_CARD, highlightthickness=1, highlightbackground="#2c3140")
        card.pack(fill="x")
        top = tk.Frame(card, bg=BG_CARD)
        top.pack(fill="x", padx=14, pady=(12, 4))
        self.ctx_head = tk.Label(top, bg=BG_CARD, fg="#7aaae0", font=("Segoe UI", 9, "bold"))
        self.ctx_head.pack(side="left")
        self.ctx_badge = tk.Label(top, bg="#1a2a40", fg="#60a0f0", font=("Segoe UI", 9), padx=7)
        self.ctx_badge.pack(side="right")
        self.ctx_body = tk.Text(card, height=6, bg=BG_CARD, fg="#b0b8c8", bd=0, highlightthickness=0,
                                wrap="word", font=("Segoe UI", 10), cursor="arrow")
        self.ctx_body.pack(fill="x", padx=14, pady=(0, 12))
        self.ctx_body.tag_configure("on", foreground="#c8e0ff", font=("Segoe UI", 10, "bold"))
        self.ctx_body.tag_configure("off", foreground=FG_MUTED)
        self.ctx_body.tag_configure("dest", foreground="#5a80a8")
        cmono = (_mono_font(), 9)
        self.ctx_body.tag_configure("code", foreground="#8a93a6", font=cmono)
        self.ctx_body.tag_configure("say", foreground="#b0b8c8", font=cmono)
        self.ctx_body.tag_configure("note", foreground="#566070", font=cmono)
        self.ctx_body.tag_configure("oncode", foreground="#e8f0ff", background="#26344a", font=(_mono_font(), 9, "bold"))
        self.ctx_body.bind("<MouseWheel>", lambda ev: self.ctx_body.yview_scroll(-1 if ev.delta > 0 else 1, "units"))
        self.ctx_body.configure(state="disabled")

        # Both panels are 32 fixed-width columns — the width of the game's dialogue window —
        # and show the text with the game's own line breaks (display only, never saved).
        mono = _mono_font()
        panes = tk.Frame(ed, bg=BG)
        self.panes = panes
        for col, (title, attr) in enumerate((("Original (German) — as in the game", "source_box"),
                                             ("Translation (English) — as in the game", "trans_box"))):
            box = tk.Frame(panes, bg=BG)
            box.grid(row=0, column=col, sticky="nw", padx=(0, 24) if col == 0 else 0)
            tk.Label(box, text=title, bg=BG, fg=FG_DIM, font=("Segoe UI", 9, "bold"), anchor="w").pack(fill="x", pady=(16, 4))
            editable = attr == "trans_box"
            t = tk.Text(box, width=WRAP_COLS, height=MAX_LINES, wrap="none", bd=0, highlightthickness=1,
                        padx=10, pady=8, font=(mono, 13), undo=False, exportselection=False,
                        bg=BG_EDIT if editable else BG_SRC, fg=FG if editable else "#c0d4f0",
                        insertbackground=FG, selectbackground="#2d5a8a", selectforeground="#ffffff",
                        inactiveselectbackground="#2d4a6a",
                        highlightbackground=BORDER if editable else "#2a3448", highlightcolor="#4a80c0")
            t.pack(anchor="w")
            t.tag_configure("soft")               # display-only line break
            # Never scroll sideways: the game's line breaks are drawn by the panel itself, and a
            # space left at the end of a line must not push the view to the right.
            t.configure(xscrollcommand=lambda first, last, w=t: self._pin_left(w, first))
            setattr(self, attr, t)
            t.bind("<<Copy>>", lambda e, w=t: (self._copy(w), "break")[1])
            t.bind("<Button-3>", lambda e, w=t: self._context_menu(e, w))
            for seq in ("<Control-a>", "<Control-A>", "<Control-slash>"):
                t.bind(seq, lambda e, w=t: (self._select_all(w), "break")[1])
            if not editable:                              # read-only panel still takes focus for Ctrl+C
                t.bind("<Button-1>", lambda e, w=t: w.focus_set(), add="+")
        self.source_box.configure(state="disabled")
        tb = self.trans_box
        tb.bind("<<Modified>>", self._on_translation_changed)
        tb.bind("<Return>", lambda e: "break")        # the game has no line breaks
        tb.bind("<KP_Enter>", lambda e: "break")
        tb.bind("<<Cut>>", lambda e: (self._cut(), "break")[1])
        tb.bind("<<Paste>>", lambda e: (self._paste(), "break")[1])
        for seq in ("<Control-z>", "<Control-Z>", "<<Undo>>"):
            tb.bind(seq, lambda e: (self._undo(), "break")[1])
        for seq in ("<Control-y>", "<Control-Y>", "<<Redo>>"):
            tb.bind(seq, lambda e: (self._redo(), "break")[1])
        tb.bind("<BackSpace>", self._skip_soft_back)
        tb.bind("<Left>", self._skip_soft_back)
        tb.bind("<Delete>", self._skip_soft_fwd)
        tb.bind("<Right>", self._skip_soft_fwd)
        self._hist: list[tuple[str, int]] = []
        self._future: list[tuple[str, int]] = []
        self._last_text = ""

        self.fit_label = tk.Label(ed, bg=BG, fg=FG_DIM, font=("Consolas", 9), anchor="w", justify="left",
                                  wraplength=800)
        self.fit_label.pack(side="bottom", fill="x", pady=(4, 0))

        nav = tk.Frame(ed, bg=BG)
        nav.pack(side="bottom", fill="x", pady=(12, 0), before=self.fit_label)
        panes.pack(fill="both", expand=True, before=nav)
        self.prev_btn = self._button(nav, "← Prev", self._prev); self.prev_btn.pack(side="left")
        self.next_btn = self._button(nav, "Next →", self._next); self.next_btn.pack(side="left", padx=6)
        self.skip_btn = self._button(nav, "Skip untranslated →", self._skip_to_next_untranslated); self.skip_btn.pack(side="left")
        self.tree_btn = self._button(nav, "Dialogue tree", lambda: self.open_tree(self.src))
        self.tree_btn.pack(side="left", padx=(18, 0))
        self.play_btn = self._button(nav, "Play through", lambda: self.open_play(self.src))
        self.play_btn.pack(side="left", padx=6)
        self.build_btn = self._button(nav, "Build translated files…", self._build_all, primary=True)
        self.build_btn.pack(side="right")
        self.counter_label = tk.Label(nav, bg=BG, fg=FG_DIM, font=("Segoe UI", 10))
        self.counter_label.pack(side="right", padx=10)

    # ── helpers ──────────────────────────────────────────

    def _set_text(self, widget: tk.Text, text: str, readonly: bool):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        if readonly:
            widget.configure(state="disabled")

    def _set_state(self, state: str):
        on = "normal" if state == "editing" else "disabled"
        for w in (self.prev_btn, self.next_btn, self.skip_btn, self.build_btn):
            w.configure(state=on)
        dlg = "normal" if state == "editing" and isinstance(self.src, DlgSource) else "disabled"
        for w in (self.tree_btn, self.play_btn):
            w.configure(state=dlg)
        self.trans_box.configure(state=on)
        self.src_box.configure(state="readonly" if self.sources else "disabled")
        if state != "editing":
            self._set_text(self.source_box, "", True)
            self._set_text(self.trans_box, "", False)
            self._set_text(self.ctx_body, "", True)
            for lbl in (self.ctx_head, self.counter_label, self.fit_label):
                lbl.configure(text="")
            self.ctx_badge.configure(text="", bg=BG_CARD)

    # ── fixed-width panels with the game's line breaks ───

    def _render(self, w: tk.Text, text: str, spans: list[tuple[int, int]], cursor: int | None = None):
        """Show text with a display-only newline (tag 'soft') where the game starts a new line."""
        readonly = w is self.source_box
        self._loading = True
        w.configure(state="normal")
        w.delete("1.0", "end")
        starts = [a for a, _ in spans[1:]]
        prev = 0
        for a in starts:
            w.insert("end", text[prev:a])
            w.insert("end", "\n", ("soft",))
            prev = a
        w.insert("end", text[prev:])
        if cursor is not None:
            shown = cursor + sum(1 for a in starts if a <= cursor)
            w.mark_set("insert", f"1.0 + {shown} chars")
            w.see("insert")
        if readonly:
            w.configure(state="disabled")
        w.edit_modified(False)
        self._loading = False

    def _pin_left(self, w: tk.Text, first):
        if float(first) > 0:
            w.after_idle(lambda: w.xview_moveto(0))

    @staticmethod
    def _soft_offsets(w: tk.Text) -> list[int]:
        r = w.tag_ranges("soft")
        out = []
        for i in range(0, len(r), 2):
            n = w.count("1.0", r[i], "chars")
            out.append(n[0] if n else 0)
        return out

    def _plain(self, w: tk.Text, a: str = "1.0", b: str = "end-1c") -> str:
        """Text between two indexes without the display-only breaks."""
        full = w.get("1.0", "end-1c")
        na = (w.count("1.0", a, "chars") or (0,))[0]
        nb = (w.count("1.0", b, "chars") or (0,))[0]
        soft = set(self._soft_offsets(w))
        return "".join(ch for k, ch in enumerate(full[na:nb], na) if k not in soft)

    def _editor_text(self) -> str:
        return self._plain(self.trans_box)

    def _editor_cursor(self) -> int:
        w = self.trans_box
        n = (w.count("1.0", "insert", "chars") or (0,))[0]
        return n - sum(1 for k in self._soft_offsets(w) if k < n)

    def _skip_soft_back(self, _e=None):
        w = self.trans_box
        if not w.tag_ranges("sel") and "soft" in w.tag_names("insert-1c"):
            w.mark_set("insert", "insert-1c")       # step over the display break, then act normally

    def _skip_soft_fwd(self, _e=None):
        w = self.trans_box
        if not w.tag_ranges("sel") and "soft" in w.tag_names("insert"):
            w.mark_set("insert", "insert+1c")

    # ── clipboard, undo, context menu ────────────────────

    def _copy(self, w: tk.Text):
        if w.tag_ranges("sel"):
            text = self._plain(w, "sel.first", "sel.last")
            self.clipboard_clear(); self.clipboard_append(text)

    def _cut(self):
        w = self.trans_box
        if w.tag_ranges("sel") and str(w["state"]) == "normal":
            self._copy(w); w.delete("sel.first", "sel.last")

    def _paste(self):
        w = self.trans_box
        if str(w["state"]) != "normal":
            return
        try:
            text = self.clipboard_get()
        except tk.TclError:
            return
        # the game has no line breaks or tabs: paste them as spaces
        text = " ".join(text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ").split("\n"))
        if w.tag_ranges("sel"):
            w.delete("sel.first", "sel.last")
        w.insert("insert", text)

    def _delete_sel(self):
        w = self.trans_box
        if w.tag_ranges("sel"):
            w.delete("sel.first", "sel.last")

    def _select_all(self, w: tk.Text):
        w.tag_add("sel", "1.0", "end-1c"); w.mark_set("insert", "end-1c"); w.focus_set()

    def _restore(self, text: str, cur: int):
        e = self.entries[self.current_idx]
        was = e.is_translated
        e.translation = self._last_text = text
        self._render(self.trans_box, text, self.src.layout(e, text), cursor=cur)
        self._dirty = True
        try:
            self.src.save_sidecar()
        except Exception:
            pass
        if was != e.is_translated:
            self._refresh_list_row(self.current_idx); self._refresh_source_menu()
        self._update_progress(); self._update_fit()

    def _undo(self):
        if self.entries and self._hist:
            self._future.append((self._last_text, self._editor_cursor()))
            self._restore(*self._hist.pop())

    def _redo(self):
        if self.entries and self._future:
            self._hist.append((self._last_text, self._editor_cursor()))
            self._restore(*self._future.pop())

    def _context_menu(self, event, w: tk.Text):
        editable = w is self.trans_box and str(w["state"]) == "normal"
        has_sel = bool(w.tag_ranges("sel"))
        try:
            has_clip = bool(self.clipboard_get())
        except tk.TclError:
            has_clip = False
        m = tk.Menu(self, tearoff=0, bg=BG_CARD, fg=FG, activebackground=SEL, activeforeground=FG,
                    disabledforeground="#556", bd=1, relief="solid", font=("Segoe UI", 10))
        st = lambda ok: "normal" if ok else "disabled"
        if w is self.trans_box:
            m.add_command(label="Undo", accelerator="Ctrl+Z", command=self._undo, state=st(editable and self._hist))
            m.add_command(label="Redo", accelerator="Ctrl+Y", command=self._redo, state=st(editable and self._future))
            m.add_separator()
            m.add_command(label="Cut", accelerator="Ctrl+X", command=self._cut, state=st(editable and has_sel))
        m.add_command(label="Copy", accelerator="Ctrl+C", command=lambda: self._copy(w), state=st(has_sel))
        if w is self.trans_box:
            m.add_command(label="Paste", accelerator="Ctrl+V", command=self._paste, state=st(editable and has_clip))
            m.add_command(label="Delete", accelerator="Del", command=self._delete_sel, state=st(editable and has_sel))
        m.add_separator()
        m.add_command(label="Select All", accelerator="Ctrl+A", command=lambda: self._select_all(w))
        if w is self.trans_box and editable:
            w.focus_set()
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()
        return "break"

    @property
    def entries(self) -> list[Entry]:
        return self.src.entries if self.src else []

    # ── opening ──────────────────────────────────────────

    def _open_folder(self):
        self._flush()
        folder = filedialog.askdirectory(title="Open the Tom Long game folder")
        if not folder:
            return
        try:
            sources = load_game(Path(folder))
        except Exception as ex:
            messagebox.showerror("Can't read the game files", str(ex))
            return
        if not sources:
            messagebox.showerror("Nothing to translate", "No .DLG, ITEMS.ITM, PROCS.CMD or .ROM files in that folder.")
            return
        if LOAD_WARNINGS:
            messagebox.showwarning(APP_NAME, "\n\n".join(LOAD_WARNINGS))
        self.game_dir, self.sources = Path(folder), sources
        self.title(f"{APP_NAME} — {self.game_dir.name}")
        self._refresh_source_menu()
        self._select_source(0)
        self.status_label.configure(text=f"{self.game_dir}  ·  pick a file in the drop-down  ·  "
                                         "Ctrl+← / Ctrl+→ to navigate  ·  Ctrl+S to build")

    def _refresh_source_menu(self):
        self.src_box.configure(values=[s.menu_label() for s in self.sources], state="readonly")
        if self.src:
            self.src_var.set(self.src.menu_label())

    def _on_source_selected(self, _e=None):
        self._select_source(self.src_box.current())

    def _select_source(self, i: int):
        self._flush()
        self.src = self.sources[i]
        self.current_idx = 0
        self._set_state("editing" if self.entries else "empty")
        self._rebuild_list()
        self._refresh_source_menu()
        if self.entries:
            self._load_entry(0)

    # ── dialogue tree / play-through ─────────────────────

    def _open_view(self, cls, src):
        if not isinstance(src, DlgSource):
            return
        self._flush()
        self.views = [v for v in self.views if v.winfo_exists()]
        for v in self.views:
            if isinstance(v, cls) and v.src is src:
                v.deiconify(); v.lift(); v.focus_force()
                return
        v = cls(self, src)
        self.views.append(v)

    def open_tree(self, src):
        self._open_view(DialogueTreeWindow, src)

    def open_play(self, src):
        self._open_view(PlayThroughWindow, src)

    def show_entry(self, src, idx: int):
        """Bring the main window to one line of one source (used by the tree and play windows)."""
        if src is not self.src:
            self._select_source(self.sources.index(src))
        self._goto(idx)
        self.lift(); self.trans_box.focus_set()

    def _refresh_views(self):
        self._view_job = None
        self.views = [v for v in self.views if v.winfo_exists()]
        for v in self.views:
            if v.src is self.src:
                try:
                    v.refresh() if isinstance(v, DialogueTreeWindow) else v.render()
                except tk.TclError:
                    pass

    def _schedule_view_refresh(self):
        if self.views:
            if self._view_job:
                self.after_cancel(self._view_job)
            self._view_job = self.after(500, self._refresh_views)

    def export_trees(self, sources: list, default_name: str, parent):
        self._flush()
        if not sources:
            return
        path = filedialog.asksaveasfilename(parent=parent, title="Save the dialogue tree as HTML",
                                            defaultextension=".html", initialfile=default_name,
                                            initialdir=str(self.game_dir or Path.home()),
                                            filetypes=[("Web page", "*.html")])
        if not path:
            return
        title = (f"Tom Long – {sources[0].path.name} dialogue tree" if len(sources) == 1
                 else "Tom Long – all dialogue trees")
        try:
            Path(path).write_text(dialogue_html(sources, title), encoding="utf-8")
        except Exception as ex:
            messagebox.showerror("Export failed", str(ex), parent=parent)
            return
        if messagebox.askyesno(APP_NAME, f"Saved {Path(path).name}.\n\nOpen it in the browser now?", parent=parent):
            import webbrowser
            webbrowser.open(Path(path).resolve().as_uri())

    def _flush(self):
        """Store the editor's text and write the current sidecar."""
        if self.src and self.entries:
            self.entries[self.current_idx].translation = self._editor_text()
            try:
                self.src.save_sidecar()
            except Exception:
                pass

    # ── list ─────────────────────────────────────────────

    def _row_style(self, i: int):
        e = self.entries[i]
        self.listbox.itemconfigure(i, fg=GREEN if e.is_translated else GREY,
                                   bg=BG_SIDE if e.indent else HEAD_ROW)

    def _rebuild_list(self):
        lb = self.listbox
        lb.delete(0, "end")
        for i, e in enumerate(self.entries):
            lb.insert("end", e.list_label())
            self._row_style(i)
        self._update_progress()
        if self.entries:
            lb.selection_set(self.current_idx)
            lb.see(self.current_idx)

    def _refresh_list_row(self, i: int):
        lb = self.listbox
        lb.delete(i)
        lb.insert(i, self.entries[i].list_label())
        self._row_style(i)
        if i == self.current_idx:
            lb.selection_set(i)

    def _update_progress(self):
        done = sum(1 for e in self.entries if e.is_translated)
        total = len(self.entries)
        self.progress.configure(maximum=max(total, 1), value=done)
        all_done = sum(s.done for s in self.sources); all_total = sum(len(s.entries) for s in self.sources)
        self.progress_label.configure(text=f"{done} / {total} in this file   ·   {all_done} / {all_total} in the game")
        if total:
            self.counter_label.configure(text=f"{self.current_idx + 1} / {total}  ({done} done)")

    # ── editor ───────────────────────────────────────────

    def _load_entry(self, idx: int):
        self.current_idx = idx
        e = self.entries[idx]
        head, badge, lines = self.src.context(e)
        self.ctx_head.configure(text=head)
        self.ctx_badge.configure(text=badge, bg="#2a1a40" if e.kind in ("npc", "name", "label") else "#1a2a40",
                                 fg="#c080ff" if e.kind in ("npc", "name", "label") else "#60a0f0")
        body = self.ctx_body
        body.configure(state="normal")
        body.delete("1.0", "end")
        code = any(st in ("code", "say") for _, st in lines)
        for k, (text, style) in enumerate(lines):
            if k and style != "note" and (style != "dest" or code):
                body.insert("end", "\n")
            if style == "on" and code:
                style = "oncode"
            body.insert("end", text, style)
        rows = sum(1 for _, st in lines if st != "note" and (st != "dest" or code))
        body.configure(height=max(3, min(rows + 1, 14)), state="disabled")
        hit = body.tag_ranges("oncode") or body.tag_ranges("on")
        body.yview_moveto(0)
        if hit:
            body.see(hit[0])

        self._render(self.source_box, e.original, self.src.layout(e, e.original))
        self._render(self.trans_box, e.translation, self.src.layout(e, e.translation), cursor=len(e.translation))
        self._hist, self._future, self._last_text = [], [], e.translation
        self._update_fit()
        self._update_progress()
        self.prev_btn.configure(state="normal" if idx > 0 else "disabled")
        self.next_btn.configure(state="normal" if idx < len(self.entries) - 1 else "disabled")
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(idx)
        self.listbox.see(idx)
        self.trans_box.focus_set()

    def _update_fit(self):
        msg, bad = self.src.fit(self.entries[self.current_idx])
        self.fit_label.configure(text=msg, fg="#e07070" if bad else FG_DIM)

    # ── navigation ───────────────────────────────────────

    def _on_list_select(self, _event=None):
        sel = self.listbox.curselection()
        if sel and sel[0] != self.current_idx:
            self._goto(sel[0])

    def _goto(self, idx: int):
        self.entries[self.current_idx].translation = self._editor_text()
        old = self.current_idx
        self._load_entry(idx)
        self._refresh_list_row(old)

    def _prev(self):
        if self.entries and self.current_idx > 0:
            self._goto(self.current_idx - 1)

    def _next(self):
        if self.entries and self.current_idx < len(self.entries) - 1:
            self._goto(self.current_idx + 1)

    def _skip_to_next_untranslated(self):
        order = list(range(self.current_idx + 1, len(self.entries))) + list(range(0, self.current_idx))
        for i in order:
            if not self.entries[i].is_translated:
                self._goto(i)
                return
        self.status_label.configure(text="Everything in this file is translated!")

    # ── editing (autosave on every change) ───────────────

    def _on_translation_changed(self, _event=None):
        if not self.trans_box.edit_modified():
            return
        self.trans_box.edit_modified(False)
        if self._loading or not self.entries:
            return
        e = self.entries[self.current_idx]
        was = e.is_translated
        text, cur = self._editor_text(), self._editor_cursor()
        if text != self._last_text:                      # own undo history (soft breaks stay out of it)
            self._hist.append((self._last_text, min(cur, len(self._last_text))))
            del self._hist[:-200]
            self._future.clear()
            self._last_text = text
        self._render(self.trans_box, text, self.src.layout(e, text), cursor=cur)
        e.translation = text
        self._dirty = True
        try:
            self.src.save_sidecar()
        except Exception as ex:
            self.status_label.configure(text=f"Couldn't autosave: {ex}")
        if was != e.is_translated:
            self._refresh_list_row(self.current_idx)
            self._refresh_source_menu()
        self._update_progress()
        self._update_fit()
        self._schedule_view_refresh()

    # ── building ─────────────────────────────────────────

    def _build_all(self):
        if not self.sources:
            return
        self._flush()
        todo = [s for s in self.sources if s.done]
        if not todo:
            messagebox.showinfo(APP_NAME, "Nothing is translated yet.")
            return
        errors, warnings = [], []
        for s in todo:
            er, wa = s.check()
            errors += er; warnings += wa
        if errors:
            shown = "\n".join(errors[:30]) + ("\n…" if len(errors) > 30 else "")
            messagebox.showerror("Can't build — fix these first", shown)
            return
        if warnings:
            shown = "\n".join(warnings[:25]) + ("\n…" if len(warnings) > 25 else "")
            if not messagebox.askyesno("Text may not fit in the game", shown + "\n\nBuild anyway?", icon="warning"):
                return
        out = filedialog.askdirectory(title="Folder for the translated game files (not the game folder itself)",
                                      initialdir=str(self.out_dir or self.game_dir.parent))
        if not out:
            return
        out = Path(out)
        if self.game_dir and out.resolve() == self.game_dir.resolve():
            messagebox.showerror(APP_NAME, "Choose a different folder: building into the game folder would overwrite "
                                           "the German originals the translator reads from.")
            return
        self.out_dir = out
        written = []
        try:
            for s in todo:
                for name, data in s.build().items():
                    (out / name).write_bytes(data)
                    written.append(name)
        except Exception as ex:
            messagebox.showerror("Build failed", str(ex))
            return
        self._dirty = False
        self.status_label.configure(text=f"Built {len(written)} file(s) into {out}")
        messagebox.showinfo(APP_NAME, f"Wrote {len(written)} file(s) to\n{out}\n\n"
                                      "Copy them over the originals in a copy of the game to play-test.")

    def _on_close(self):
        # Translations are already autosaved on every change; just make sure the
        # current line is written, then close without asking.
        self._flush()
        self.destroy()


if __name__ == "__main__":
    TomLongTranslator().mainloop()
