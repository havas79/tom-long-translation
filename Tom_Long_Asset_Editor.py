#!/usr/bin/env python3
"""
Tom Long Asset Editor — browse, preview, export and re-import the graphics of
"Tom Long: The Time Adventure" (1994).

  python Tom_Long_Asset_Editor.py            (needs Pillow:  pip install pillow)

Covers: room backgrounds and cursors (GRAFIK.PLB, SYSTEM.PLB), characters (*.NPC),
room animations (ANIMS.ANI), item icons and room sprites (ITEMS.ITM), UI graphics
(*.ANM), the intro title animation (TITEL.ACF + TITEL.ACP) and foreground masks (MASKS.MSK).

Workflow
  1. Open the game folder. Browse the tree; groups show a sheet or play as animations.
  2. Export selected… / Export all… writes 8-bit indexed PNGs (with the game palette),
     GIFs of the animations and the palettes as GIMP .gpl files.
  3. Edit the PNGs in any editor, keeping them indexed with the same palette and size.
  4. Import PNG… (one image) or Import folder… (a whole exported folder). Imports are
     strict: indexed PNG, same size, and every used colour must equal the game palette's
     colour at that index. Anything else is refused with the reason.
  5. Save changed game files… writes only the changed files, under their original names,
     to a folder you choose. If that folder already holds the same file (e.g. ITEMS.ITM
     built by the Tom Long Translator), the image changes are applied on top of it.

Animation frames (ANIMS.ANI) may be imported at a different size (width a multiple of 4);
the Resize anchor decides which point stays in place (top-left, centre or bottom-centre).
All other images keep their size. Sprites are re-compressed with the game's own RLE. The intro frames are re-packed with PowerPacker.
"""

from __future__ import annotations




# ══════════════════════════════════════════════════════════════════════════
# Game formats: decoders, encoders, container rebuilders
# ══════════════════════════════════════════════════════════════════════════

import struct

PIC_TYPES = {0: (320, 240), 1: (320, 144), 2: (320, 160), 3: (320, 400), 4: (320, 200),
             5: (480, 160), 6: (480, 80), 7: (352, 160), 8: (400, 160), 9: (640, 160),
             11: (32, 32), 12: (16, 16)}


def vga_to_rgb(pal: bytes) -> list[tuple[int, int, int]]:
    """6-bit VGA palette → 8-bit RGB. The DAC ignores the top two bits (1082:00c0)."""
    return [tuple(((pal[i * 3 + k] & 0x3F) * 255) // 63 for k in range(3)) for i in range(len(pal) // 3)]


# ── PCLL picture library (SYSTEM.PLB, GRAFIK.PLB) ───────────────────────────

def plb_entries(d: bytes):
    """[(type, w, h, palette_offset, pixel_offset)] — 1014:0002 / 1014:00ef."""
    if d[:4] != b"PCLL":
        raise ValueError("not a PCLL library")
    out = []
    for i in range(d[6] + 1):
        t, off = struct.unpack_from("<B3xI", d, 0x10 + 8 * i)
        w, h = PIC_TYPES[t]
        p = 0x810 + off
        out.append((t, w, h, p, p + 768))
    return out


# ── RLE sprite frames (NPC, ANIMS.ANI, item room sprites) ───────────────────

def sprite_decode(d: bytes, p: int):
    """→ (w, h, x, y, pixels row-major). Blitter 1147:000e: Mode-X planes, 0x01 n v = run."""
    w, h, x, y = struct.unpack_from("<HHhh", d, p)
    q = p + 8; buf = bytearray()
    while len(buf) < w * h:
        c = d[q]; q += 1
        if c == 1:
            buf += bytes([d[q + 1]]) * d[q]; q += 2
        else:
            buf.append(c)
    out = bytearray(w * h); i = 0; cols = w // 4
    for plane in range(4):
        for r in range(h):
            for c in range(cols):
                out[r * w + c * 4 + plane] = buf[i]; i += 1
    return w, h, x, y, bytes(out)


def sprite_encode(w: int, h: int, x: int, y: int, pixels: bytes) -> bytes:
    """Inverse of sprite_decode. Runs of ≥3 (and any colour-1 pixel, since 0x01 is the run
    marker) become 01 n v; runs never cross a plane boundary, like the originals."""
    if w % 4:
        raise ValueError("sprite width must be a multiple of 4")
    cols = w // 4
    out = bytearray(struct.pack("<HHhh", w, h, x, y))
    for plane in range(4):
        stream = bytes(pixels[r * w + c * 4 + plane] for r in range(h) for c in range(cols))
        i = 0; n = len(stream)
        while i < n:
            v = stream[i]; j = i
            while j < n and stream[j] == v and j - i < 255:
                j += 1
            run = j - i
            if run >= 3 or v == 1:
                out += bytes((1, run, v)); i = j
            else:
                out.append(v); i += 1
    return bytes(out) + b"\0\0"          # the originals end every frame with 2 slack bytes


# ── NPC ─────────────────────────────────────────────────────────────────────

def npc_frames(d: bytes) -> list[list[int]]:
    """5 records × 166 bytes: u16 frames, u16 w, u16 h, u32 offset[40] — 071d:0076."""
    return [list(struct.unpack_from("<40I", d, r * 166 + 6)[:struct.unpack_from("<H", d, r * 166)[0]])
            for r in range(5)]


def npc_rebuild(d: bytes, new_frames: dict[int, bytes]) -> bytes:
    """Replace frames (keyed by original file offset) and relink the offset tables."""
    recs = npc_frames(d)
    order = sorted({o for r in recs for o in r})
    ends = order[1:] + [len(d)]
    head = bytearray(d[:0x33E]); body = bytearray(); newoff = {}
    for o, e in zip(order, ends):
        newoff[o] = 0x33E + len(body)
        body += new_frames.get(o, d[o:e])
    for r, offs in enumerate(recs):
        for k, o in enumerate(offs):
            struct.pack_into("<I", head, r * 166 + 6 + 4 * k, newoff[o])
    return bytes(head + body)


# ── ANIMS.ANI ───────────────────────────────────────────────────────────────

def ani_index(d: bytes):
    return list(struct.unpack_from("<256I", d, 0)), list(struct.unpack_from("<256I", d, 0x400))


def ani_record(d: bytes, i: int):
    """→ (frame_count, steps[(frame,dx,dy)], flags, [absolute frame offsets]) — 084b:0066."""
    offs, sizes = ani_index(d)
    o, s = offs[i], sizes[i]
    nf, ns, fl = struct.unpack_from("<3H", d, o)
    steps = list(zip(*(struct.unpack_from(f"<{ns}h", d, o + 6 + k * 2 * ns) for k in range(3))))
    rel = struct.unpack_from(f"<{nf}I", d, o + s - 4 * nf)
    return nf, steps, fl, [o + r for r in rel]


def ani_rebuild(d: bytes, new_frames: dict[int, bytes]) -> bytes:
    """Replace frames (keyed by absolute original offset); rebuild records and the index."""
    offs, sizes = ani_index(d)
    used = sorted((offs[i], i) for i in range(256) if sizes[i])
    out = bytearray(d[:0x800]); new_offs = [0] * 256; new_sizes = [0] * 256
    for o, i in used:
        s = sizes[i]
        nf, ns = struct.unpack_from("<2H", d, o)
        head_len = 6 + 6 * ns
        rel = list(struct.unpack_from(f"<{nf}I", d, o + s - 4 * nf))
        order = sorted(set(rel))
        ends = order[1:] + [s - 4 * nf]
        rec = bytearray(d[o:o + head_len]); newrel = {}
        if order and order[0] > head_len:                 # keep any bytes before the first frame
            rec += d[o + head_len:o + order[0]]
        for r, e in zip(order, ends):
            newrel[r] = len(rec)
            rec += new_frames.get(o + r, d[o + r:o + e])
        rec += struct.pack(f"<{nf}I", *[newrel[r] for r in rel])
        new_offs[i] = len(out); new_sizes[i] = len(rec)
        out += rec
    struct.pack_into("<256I", out, 0, *new_offs)
    struct.pack_into("<256I", out, 0x400, *new_sizes)
    return bytes(out)


# ── ANM (cursors, verb buttons) ─────────────────────────────────────────────

def anm_parse(d: bytes):
    if d[:4] != b"ANIM":
        raise ValueError("no ANIM header")
    n, w, h = struct.unpack_from("<3H", d, 4)
    return n, w, h, [10 + k * w * h for k in range(n)]


# ── ITEMS.ITM (icons, room sprites) ─────────────────────────────────────────

VM_LEN = {0xc0: 1, 0xc1: 0x1d, 0xc2: 1, 0xc3: 2, 0xc4: 3, 0xc5: 1, 0xc6: 2, 0xc7: 3, 0xc8: 3, 0xc9: 3, 0xca: 9,
          0xcb: 3, 0xcc: 1, 0xcd: 1, 0xce: 3, 0xcf: 3, 0xd0: 1, 0xd1: 3, 0xd2: 3, 0xd3: 0xc, 0xd4: 3, 0xd5: 1,
          0xd6: 1, 0xd7: 1, 0xd8: 1, 0xd9: 1, 0xda: 0xf, 0xdb: 0xe, 0xdc: 7, 0xdd: 3, 0xde: 5, 0xdf: 1, 0xe0: 3,
          0xe1: 1, 0xe2: 9, 0xe3: 1, 0xe4: 1, 0xe5: 1, 0xe6: 1, 0xe7: 1, 0xe8: 3, 0xe9: 1, 0xea: 1, 0xeb: 1,
          0xec: 1, 0xed: 9, 0xee: 3, 0xef: 3, 0xf0: 2, 0xf1: 1, 0xf2: 1, 0xf3: 3, 0xf4: 2, 0xf5: 5, 0xf6: 1, 0xf7: 1}


def items_parse(d: bytes):
    """→ [dict(index, name, icon_at, sprite_len_at, sprite_at, sprite_len)] — 058a:10e9 / 058a:0eab."""
    out = []
    for idx, p in enumerate(struct.unpack_from("<256I", d, 0)):
        if not p:
            break
        it = dict(index=idx, name="", offset=p)
        while d[p]:
            t = d[p]
            if t == 1:
                kind = d[p + 1]; p += 6
                if kind == 0:
                    n = struct.unpack_from("<H", d, p)[0]
                    it.update(sprite_len_at=p, sprite_at=p + 2, sprite_len=n)
                    p += 2 + n
            elif t == 2:
                it["name"] = d[p + 2:p + 18].split(b"\0")[0].decode("cp437"); p += 18
            elif t in (3, 5, 6, 7, 8):
                p += 1
                while True:
                    op = d[p]; p += VM_LEN[op]
                    if op == 0xC0:
                        break
            elif t == 4:
                it["icon_at"] = p + 2; p += 2 + 1024
            elif t == 9:
                p += 2
            else:
                raise ValueError(f"ITEMS.ITM item {idx}: unknown tag {t}")
        out.append(it)
    return out


def items_rebuild(d: bytes, icons: dict[int, bytes], sprites: dict[int, bytes]) -> bytes:
    """Replace icons (in place) and room sprites (relocating) by item index."""
    items = items_parse(d)
    patches = []
    for it in items:
        if it["index"] in icons and "icon_at" in it:
            patches.append((it["icon_at"], 1024, icons[it["index"]]))
        if it["index"] in sprites and "sprite_at" in it:
            new = sprites[it["index"]]
            patches.append((it["sprite_len_at"], 2 + it["sprite_len"], struct.pack("<H", len(new)) + new))
    patches.sort()
    out = bytearray(); pos = 0; shifts = []
    for off, n, new in patches:
        out += d[pos:off]; out += new; pos = off + n; shifts.append((off, len(new) - n))
    out += d[pos:]
    offs = struct.unpack_from("<256I", d, 0)
    remap = lambda o: o + sum(dl for off, dl in shifts if off < o)
    struct.pack_into("<256I", out, 0, *[remap(o) if o else 0 for o in offs])
    return bytes(out)


# ── Room files: which room picture, masks, NPCs, animations ─────────────────

def rom_ops(d: bytes):
    """Yield (op, args) for a room file (07a2:0086)."""
    p = 0
    rd = lambda fmt: struct.unpack_from(fmt, d, p)
    while p < len(d):
        op = d[p]; p += 1
        if op == 1:
            yield op, dict(pict=d[p]); p += 1
        elif op == 2: p += 13
        elif op == 3: p += 9
        elif op == 4: p += 41
        elif op == 6: p += 3 + struct.unpack_from("<h", d, p + 1)[0]
        elif op == 7:
            yield op, dict(item=d[p]); p += 10
        elif op == 8:
            x, y, w, h, off = struct.unpack_from("<hhhhI", d, p + 1)
            yield op, dict(x=x, y=y, w=w, h=h, off=off); p += 13
        elif op in (9, 15): p += 2
        elif op == 11:
            yield op, dict(npc=d[p + 5:p + 13].split(b"\0")[0].decode("cp437")); p += 13
        elif op == 12: p += 3
        elif op == 13:
            a = struct.unpack_from("<h", d, p + 1)[0]
            yield op, dict(anim=(-a - 1) if a < 0 else a); p += 7
        elif op == 16:
            a = struct.unpack_from("<h", d, p + 1)[0]
            yield op, dict(anim=(-a - 1) if a < 0 else a); p += 3
        elif op == 18: p += 4
        elif op in (5, 10, 14, 19, 20): pass
        else:
            raise ValueError(f"bad room opcode {op:#x}")


# ── TITEL.ACF / PowerPacker ─────────────────────────────────────────────────

def pp_decrunch(blob: bytes) -> bytes:
    """PowerPacker 2.0 (INTRO.EXE 0735:0095)."""
    eff = blob[4:8]; out_len = int.from_bytes(blob[-4:-1], "big"); skip = blob[-1]
    src = blob[8:-4]; pos = len(src); out = bytearray(out_len); o = out_len
    buf = nbits = 0

    def bits(n):
        nonlocal buf, nbits, pos
        while nbits < n:
            pos -= 1
            buf |= src[pos] << nbits; nbits += 8
        v = 0
        for _ in range(n):
            v = (v << 1) | (buf & 1); buf >>= 1
        nbits -= n
        return v

    bits(skip)
    while o > 0:
        if bits(1) == 0:
            n = 1
            while True:
                x = bits(2); n += x
                if x != 3:
                    break
            for _ in range(n):
                o -= 1; out[o] = bits(8)
            if o <= 0:
                break
        x = bits(2); ob = eff[x]; n = x + 2
        if x == 3:
            if bits(1) == 0:
                ob = 7
            off = bits(ob)
            while True:
                y = bits(3); n += y
                if y != 7:
                    break
        else:
            off = bits(ob)
        for _ in range(n):
            o -= 1; out[o] = out[o + 1 + off]
    return bytes(out)


PP_EFF = (9, 10, 11, 12)


def pp_crunch(data: bytes) -> bytes:
    """PowerPacker 2.0 compatible packer (greedy LZ77 on the reversed data).
    Blob layout as in TITEL.ACF: u32le packed_len, eff[4], stream, u24be unpacked_len, u8 skip."""
    R = data[::-1]; N = len(R)
    maxd = [1 << b for b in PP_EFF]                    # distance = offset + 1 ≤ 2^bits
    heads: dict[bytes, list[int]] = {}
    bits: list[int] = []

    def put(v, n):
        for k in range(n - 1, -1, -1):
            bits.append((v >> k) & 1)

    def best_match(i):
        if i + 2 > N:
            return 0, 0
        cands = heads.get(R[i:i + 2], [])
        best_len, best_d = 0, 0
        for j in reversed(cands[-256:]):
            d = i - j
            if d > maxd[3]:
                break
            ln = 0
            while i + ln < N and R[j + ln] == R[i + ln] and ln < 4096:
                ln += 1
            limit = maxd[min(ln, 5) - 2] if ln >= 2 else 0
            if ln >= 2 and d > limit:                  # too far for this length class: shorten
                for L in (4, 3, 2):
                    if ln >= L and d <= maxd[L - 2]:
                        ln = L; break
                else:
                    continue
            if ln > best_len:
                best_len, best_d = ln, d
        return best_len, best_d

    def add(i):
        if i + 2 <= N:
            heads.setdefault(R[i:i + 2], []).append(i)

    tokens = []; lits = []; i = 0
    while i < N:
        ln, d = best_match(i)
        if ln >= 2:
            tokens.append(("L", lits)); lits = []
            tokens.append(("M", ln, d))
            for k in range(ln):
                add(i + k)
            i += ln
        else:
            lits.append(R[i]); add(i); i += 1
    tokens.append(("L", lits))

    # serialise: literal runs are only allowed right before a match (or at the very end)
    k = 0
    while k < len(tokens):
        t = tokens[k]
        if t[0] == "L":
            if t[1]:
                put(0, 1)
                n = len(t[1]) - 1
                while n >= 3:
                    put(3, 2); n -= 3
                put(n, 2)
                for b in t[1]:
                    put(b, 8)
                k += 1
                continue                                # a match follows (or the end)
            k += 1
            if k < len(tokens) and tokens[k][0] == "M":
                put(1, 1)                                # match without literals
            continue
        _, ln, d = t
        off = d - 1
        if ln < 5:
            put(ln - 2, 2); put(off, PP_EFF[ln - 2])
        else:
            put(3, 2)
            if off < 128:
                put(0, 1); put(off, 7)
            else:
                put(1, 1); put(off, PP_EFF[3])
            n = ln - 5
            while n >= 7:
                put(7, 3); n -= 7
            put(n, 3)
        k += 1

    skip = (-len(bits)) % 32
    allbits = [0] * skip + bits
    nbytes = len(allbits) // 8
    stream = bytearray(nbytes)
    for kbit, b in enumerate(allbits):
        if b:
            stream[nbytes - 1 - kbit // 8] |= 1 << (kbit % 8)
    body = bytes(PP_EFF) + bytes(stream) + len(data).to_bytes(3, "big") + bytes([skip])
    return struct.pack("<I", 4 + len(stream)) + body           # u32 = packed size - 8


def acf_parse(d: bytes):
    """→ [dict(x, y, w, h, delay, blob_at, blob_len)] — INTRO.EXE 065a:0256."""
    n = struct.unpack_from("<H", d, 0)[0]; p = 2 + 14 * n; out = []
    for i in range(n):
        x, y, w, h, size, delay = struct.unpack_from("<4HIH", d, 2 + 14 * i)
        out.append(dict(x=x, y=y, w=w, h=h, delay=delay, blob_at=p, blob_len=size)); p += size
    return out


ACF_MAX_PACKED = 0xFA00      # INTRO.EXE reads all frame data with one fread(…, 0xFA00)


def acf_rebuild(d: bytes, new_pixels: dict[int, bytes]) -> bytes:
    frames = acf_parse(d)
    head = bytearray(d[:2 + 14 * len(frames)]); body = bytearray()
    for i, f in enumerate(frames):
        blob = pp_crunch(new_pixels[i]) if i in new_pixels else d[f["blob_at"]:f["blob_at"] + f["blob_len"]]
        struct.pack_into("<I", head, 2 + 14 * i + 8, len(blob))
        body += blob
    if len(body) > ACF_MAX_PACKED:
        raise ValueError(f"intro frames pack to {len(body)} bytes; INTRO.EXE loads at most {ACF_MAX_PACKED}")
    return bytes(head + body)


# ══════════════════════════════════════════════════════════════════════════
# Asset catalogue
# ══════════════════════════════════════════════════════════════════════════

import os
import re
from pathlib import Path


def _find(folder: Path, name: str) -> Path | None:
    for f in folder.iterdir():
        if f.name.lower() == name.lower():
            return f
    return None


class Frame:
    """One editable image. `key` identifies it semantically (independent of file offsets)."""
    def __init__(self, key, label, w, h, loader, x=0, y=0, transparent=True, pal=None, note=""):
        self.key = key            # e.g. ("npc", "PLAYER.NPC", 0, 3)
        self.label = label
        self.w, self.h = w, h
        self.x, self.y = x, y     # sprite draw offsets (header of the frame)
        self._loader = loader     # () -> bytes (w*h palette indices)
        self.transparent = transparent
        self.pal = pal            # default palette id
        self.note = note
        self._orig = None

    def original(self) -> bytes:
        if self._orig is None:
            self._orig = self._loader()
        return self._orig


class Group:
    """A node in the tree: holds frames and/or child groups; can be played if animated."""
    def __init__(self, label, frames=None, children=None, anim=None, pal=None, note=""):
        self.label = label
        self.frames: list[Frame] = frames or []
        self.children: list[Group] = children or []
        self.anim = anim          # None | dict(kind="seq"|"ani"|"acf", ...)
        self.pal = pal
        self.note = note


class Game:
    def __init__(self, folder: Path):
        self.folder = folder
        self.files: dict[str, bytes] = {}
        self.palettes: dict[str, list[tuple[int, int, int]]] = {}   # id -> 256 RGB
        self.pal6: dict[str, bytes] = {}                              # id -> 768 6-bit values
        self.pal_names: dict[str, str] = {}
        self.rooms: dict[str, dict] = {}
        self.groups: list[Group] = []
        self._load()

    def data(self, name: str) -> bytes:
        if name not in self.files:
            f = _find(self.folder, name)
            if f is None:
                raise FileNotFoundError(name)
            self.files[name] = f.read_bytes()
        return self.files[name]

    def has(self, name: str) -> bool:
        return _find(self.folder, name) is not None

    # palettes -----------------------------------------------------------
    def _add_pal(self, pid, name, pal6: bytes):
        p = bytes(v & 0x3F for v in pal6[:768]).ljust(768, b"\0")
        self.pal6[pid] = p
        self.palettes[pid] = vga_to_rgb(p)
        self.pal_names[pid] = name

    def _load(self):
        g = self.data("GRAFIK.PLB")
        s = self.data("SYSTEM.PLB")
        sys_ents = plb_entries(s)
        sys_pal = bytearray(s[sys_ents[0][3]:sys_ents[0][3] + 768])
        sys_pal[0:3] = b"\0\0\0"; sys_pal[3:6] = b"\x3f\x3f\x3f"         # 093a:044d
        self.sys_pal = bytes(sys_pal)
        # rooms: which picture, items, masks, NPCs, animations (07a2:0086)
        room_of_pict: dict[int, list[str]] = {}
        for f in sorted(self.folder.iterdir()):
            if f.suffix.lower() != ".rom":
                continue
            info = dict(picts=[], items=set(), masks=[], npcs=set(), anims=set())
            for op, a in rom_ops(f.read_bytes()):
                if op == 1: info["picts"].append(a["pict"])
                elif op == 7: info["items"].add(a["item"])
                elif op == 8: info["masks"].append(a)
                elif op == 11: info["npcs"].add(a["npc"].upper() + ".NPC")
                elif op in (13, 16): info["anims"].add(a["anim"])
            self.rooms[f.stem.upper()] = info
            for p in info["picts"]:
                room_of_pict.setdefault(p, []).append(f.stem.upper())
        self.room_of_pict = room_of_pict
        g_ents = plb_entries(g)
        for i, (t, w, h, pp, px) in enumerate(g_ents):
            rooms = ", ".join(room_of_pict.get(i, [])) or "not used by a room"
            self._add_pal(f"pic{i}", f"Background {i:02d} ({rooms}) — file palette", g[pp:pp + 768])
            runtime = self.sys_pal[:48] + g[pp + 48:pp + 768]
            self._add_pal(f"room{i}", f"Room {rooms} (background {i:02d})", runtime)
        for i, (t, w, h, pp, px) in enumerate(sys_ents):
            self._add_pal(f"sys{i}", f"Cursor {i} — file palette", s[pp:pp + 768])
        if self.has("TITEL.ACP"):
            self._add_pal("acp", "Intro (TITEL.ACP)", self.data("TITEL.ACP"))

        def room_pal(names):
            for r in sorted(names):
                picts = self.rooms.get(r, {}).get("picts", [])
                if picts:
                    return f"room{picts[0]}"
            main = self.rooms.get("MAIN", {}).get("picts", [0])
            return f"room{main[0] if main else 0}"

        def rooms_with(field, value):
            return sorted(r for r, i in self.rooms.items() if value in i[field])

        G = self.groups
        # backgrounds
        bg = Group("Backgrounds  (GRAFIK.PLB)")
        for i, (t, w, h, pp, px) in enumerate(g_ents):
            rooms = ", ".join(room_of_pict.get(i, [])) or "—"
            bg.frames.append(Frame(("plb", "GRAFIK.PLB", i), f"{i:02d}  {w}×{h}  {rooms}", w, h,
                                   (lambda px=px, n=w * h: self.data("GRAFIK.PLB")[px:px + n]),
                                   transparent=False, pal=f"pic{i}",
                                   note=f"Rooms: {rooms}. Colours 0–15 are replaced by the system colours in the game."))
        G.append(bg)
        cur = Group("Cursors  (SYSTEM.PLB)")
        for i, (t, w, h, pp, px) in enumerate(sys_ents):
            cur.frames.append(Frame(("plb", "SYSTEM.PLB", i), f"{i}  {w}×{h}", w, h,
                                    (lambda px=px, n=w * h: self.data("SYSTEM.PLB")[px:px + n]),
                                    transparent=True, pal=f"sys{i}"))
        G.append(cur)
        # characters
        chars = Group("Characters  (*.NPC)")
        for f in sorted(p for p in self.folder.iterdir() if p.suffix.lower() == ".npc"):
            name = f.name.upper(); d = self.data(name)
            rooms = rooms_with("npcs", name)
            pal = room_pal(rooms)
            fg = Group(f"{name}" + (f"   ({', '.join(rooms)})" if rooms else ""), pal=pal)
            for r, offs in enumerate(npc_frames(d)):
                if not offs:
                    continue
                ag = Group(f"animation {r}  ({len(offs)} frames)", anim=dict(kind="seq", fps=8), pal=pal)
                for k, o in enumerate(offs):
                    w, h, x, y = struct.unpack_from("<HHhh", d, o)
                    ag.frames.append(Frame(("npc", name, r, k), f"frame {k:02d}  {w}×{h}", w, h,
                                           (lambda name=name, o=o: sprite_decode(self.data(name), o)[4]),
                                           x=x, y=y, pal=pal))
                fg.children.append(ag)
            chars.children.append(fg)
        G.append(chars)
        # room animations
        anis = Group("Animations  (ANIMS.ANI)")
        d = self.data("ANIMS.ANI"); offs, sizes = ani_index(d)
        for i in range(256):
            if not sizes[i]:
                continue
            nf, steps, fl, fos = ani_record(d, i)
            rooms = rooms_with("anims", i)
            pal = room_pal(rooms)
            ag = Group(f"anim {i:03d}  ({nf} frames)" + (f"   {', '.join(rooms)}" if rooms else ""),
                       anim=dict(kind="ani", steps=steps, loop=bool(fl & 1), fps=10), pal=pal)
            for k, fo in enumerate(fos):
                w, h, x, y = struct.unpack_from("<HHhh", d, fo)
                ag.frames.append(Frame(("ani", i, k), f"frame {k:02d}  {w}×{h}", w, h,
                                       (lambda fo=fo: sprite_decode(self.data("ANIMS.ANI"), fo)[4]),
                                       x=x, y=y, pal=pal))
            anis.children.append(ag)
        G.append(anis)
        # items
        items = Group("Items  (ITEMS.ITM)")
        d = self.data("ITEMS.ITM")
        for it in items_parse(d):
            if "icon_at" not in it and "sprite_at" not in it:
                continue
            rooms = rooms_with("items", it["index"])
            pal = room_pal(rooms)
            ig = Group(f"#{it['index']:03d}  {it['name'] or '(no name)'}" + (f"   {', '.join(rooms)}" if rooms else ""), pal=pal)
            if "icon_at" in it:
                ig.frames.append(Frame(("icon", it["index"]), "inventory icon  32×32", 32, 32,
                                       (lambda a=it["icon_at"]: self.data("ITEMS.ITM")[a:a + 1024]), pal=pal))
            if "sprite_at" in it:
                w, h, x, y = struct.unpack_from("<HHhh", d, it["sprite_at"])
                ig.frames.append(Frame(("isprite", it["index"]), f"room sprite  {w}×{h}", w, h,
                                       (lambda a=it["sprite_at"]: sprite_decode(self.data("ITEMS.ITM"), a)[4]),
                                       x=x, y=y, pal=pal))
            items.children.append(ig)
        G.append(items)
        # UI .anm
        ui = Group("UI graphics  (*.ANM)")
        main_pal = room_pal(["MAIN"])
        for f in sorted(p for p in self.folder.iterdir() if p.suffix.lower() == ".anm"):
            name = f.name.upper(); n, w, h, fo = anm_parse(self.data(name))
            ag = Group(f"{name}  ({n} frame{'s' if n != 1 else ''}, {w}×{h})",
                       anim=dict(kind="seq", fps=4) if n > 1 else None, pal=main_pal)
            for k, o in enumerate(fo):
                ag.frames.append(Frame(("anm", name, k), f"frame {k}", w, h,
                                       (lambda name=name, o=o, n=w * h: self.data(name)[o:o + n]), pal=main_pal))
            ui.children.append(ag)
        G.append(ui)
        # intro
        if self.has("TITEL.ACF"):
            d = self.data("TITEL.ACF")
            fr = acf_parse(d)
            ig = Group(f"Intro title  (TITEL.ACF, {len(fr)} frames)",
                       anim=dict(kind="acf", rects=[(f["x"], f["y"], f["w"], f["h"], f["delay"]) for f in fr]), pal="acp")
            for k, f in enumerate(fr):
                ig.frames.append(Frame(("acf", k), f"frame {k:03d}  {f['w']}×{f['h']} at {f['x']},{f['y']}", f["w"], f["h"],
                                       (lambda a=f["blob_at"], n=f["blob_len"]: pp_decrunch(self.data("TITEL.ACF")[a:a + n])),
                                       x=f["x"], y=f["y"], transparent=False, pal="acp"))
            G.append(Group("Intro  (INTRO.EXE)", children=[ig]))
        # masks
        if self.has("MASKS.MSK"):
            mg = Group("Foreground masks  (MASKS.MSK)")
            for r, info in sorted(self.rooms.items()):
                if not info["masks"]:
                    continue
                pal = f"room{info['picts'][0]}" if info["picts"] else main_pal
                rg = Group(f"{r}  ({len(info['masks'])} mask{'s' if len(info['masks']) != 1 else ''})", pal=pal)
                for k, m in enumerate(info["masks"]):
                    rg.frames.append(Frame(("msk", m["off"], m["w"], m["h"]),
                                           f"mask {k}  {m['w']}×{m['h']} at {m['x']},{m['y']}", m["w"], m["h"],
                                           (lambda o=m["off"], n=m["w"] * m["h"]: self.data("MASKS.MSK")[o:o + n]),
                                           x=m["x"], y=m["y"], pal=pal))
                mg.children.append(rg)
            G.append(mg)

    def all_frames(self):
        def walk(g, path):
            for f in g.frames:
                yield path + [g], f
            for c in g.children:
                yield from walk(c, path + [g])
        for g in self.groups:
            yield from walk(g, [])


# ── export names (used by Export all / Import folder) ────────────────────

def export_name(key) -> str:
    k = key[0]
    if k == "plb":
        return f"{'backgrounds' if key[1] == 'GRAFIK.PLB' else 'cursors'}/{key[1].split('.')[0].lower()}_{key[2]:02d}.png"
    if k == "npc":
        return f"characters/{key[1].split('.')[0].lower()}/anim{key[2]}_frame{key[3]:02d}.png"
    if k == "ani":
        return f"animations/anim{key[1]:03d}/frame{key[2]:02d}.png"
    if k == "icon":
        return f"items/item{key[1]:03d}_icon.png"
    if k == "isprite":
        return f"items/item{key[1]:03d}_room.png"
    if k == "anm":
        return f"ui/{key[1].split('.')[0].lower()}_frame{key[2]}.png"
    if k == "acf":
        return f"intro/titel_frame{key[1]:03d}.png"
    if k == "msk":
        return f"masks/mask_{key[1]:06x}_{key[2]}x{key[3]}.png"
    raise KeyError(key)


# ── strict palette import ─────────────────────────────────────────────────

class Pix(bytes):
    """New pixels for a frame; for resized animation frames also the new size and offset."""
    def __new__(cls, data, w, h, x=0, y=0):
        o = super().__new__(cls, data)
        o.w, o.h, o.x, o.y = w, h, x, y
        return o


ANCHORS = ("top-left", "centre", "bottom-centre")


def resizable(frame: Frame) -> bool:
    """Only ANIMS.ANI frames may change size: each frame carries its own w/h/x/y header,
    read at every draw (1147:000e); widths must stay a multiple of 4 (Mode-X planes)."""
    return frame.key[0] == "ani"


def anchored_offset(frame: Frame, w: int, h: int, anchor: str) -> tuple[int, int]:
    """New draw offset for a frame resized to w×h, keeping the chosen point in place."""
    if anchor == "centre":
        return frame.x + (frame.w - w) // 2, frame.y + (frame.h - h) // 2
    if anchor == "bottom-centre":
        return frame.x + (frame.w - w) // 2, frame.y + (frame.h - h)
    return frame.x, frame.y

def read_indexed_png(path: str, frame: Frame, game: Game, anchor: str = "top-left") -> tuple[bytes | None, str]:
    """Return (indices, message). Strict: 8-bit indexed PNG, same size, and every colour the
    image uses must equal the game palette's colour at that index (for one game palette)."""
    from PIL import Image
    try:
        im = Image.open(path)
    except Exception as ex:
        return None, f"can't open: {ex}"
    if im.mode != "P":
        return None, (f"the image is {im.mode}, not indexed (8-bit palette). Save it as an indexed PNG "
                      f"using the game palette — e.g. in GIMP: Image ▸ Mode ▸ Indexed ▸ Use custom palette.")
    w, h = im.size
    if (w, h) != (frame.w, frame.h):
        if not resizable(frame):
            return None, f"size is {w}×{h}, but this image must stay {frame.w}×{frame.h}"
        if w % 4:
            return None, (f"width {w} is not a multiple of 4 — the game draws in 4 interleaved planes, "
                          f"so animation frames must be 4, 8, 12, … pixels wide")
        if not (4 <= w <= 640 and 1 <= h <= 400):
            return None, f"size {w}×{h} is out of range for an animation frame (max 640×400)"
    px = im.tobytes()
    pal = im.getpalette() or []
    pal = pal + [0] * (768 - len(pal))
    used = sorted(set(px))

    def matches(pid):
        ref = game.pal6[pid]
        for i in used:
            for k in range(3):
                v8 = pal[i * 3 + k]; c6 = ref[i * 3 + k]
                if v8 >> 2 != c6 and round(v8 * 63 / 255) != c6:
                    return False
        return True

    order = [frame.pal] + [p for p in game.palettes if p != frame.pal]
    for pid in order:
        if pid and pid in game.pal6 and matches(pid):
            if (w, h) != (frame.w, frame.h):
                x, y = anchored_offset(frame, w, h, anchor)
                return Pix(px, w, h, x, y), (f"palette OK ({game.pal_names[pid]}); resized "
                                             f"{frame.w}×{frame.h} → {w}×{h}, anchored {anchor}, offset {x},{y}")
            return px, f"palette OK ({game.pal_names[pid]})"
    bad = []
    ref = game.pal6.get(frame.pal) or next(iter(game.pal6.values()))
    for i in used:
        if any(pal[i * 3 + k] >> 2 != ref[i * 3 + k] and round(pal[i * 3 + k] * 63 / 255) != ref[i * 3 + k] for k in range(3)):
            bad.append(i)
    return None, (f"{len(bad)} of the {len(used)} colours used don't match any game palette "
                  f"(first mismatching index: {bad[:6]}). Keep the palette the image was exported with.")


# ── saving ────────────────────────────────────────────────────────────────

def file_of(key) -> str:
    """Game file an edit belongs to."""
    if key[0] in ("plb", "npc", "anm"):
        return key[1]
    return {"ani": "ANIMS.ANI", "icon": "ITEMS.ITM", "isprite": "ITEMS.ITM",
            "acf": "TITEL.ACF", "msk": "MASKS.MSK"}[key[0]]


def build_files(game: Game, edits: dict, base_dir: Path | None) -> dict[str, bytes]:
    """Apply edits (key -> indices) to each affected file. If base_dir already holds a file of
    the same name (e.g. a translated ITEMS.ITM), the edits go on top of that file."""
    by_file: dict[str, list] = {}
    for key, px in edits.items():
        by_file.setdefault(file_of(key), []).append((key, px))
    out = {}
    for name, lst in by_file.items():
        base = None
        if base_dir is not None:
            f = _find(base_dir, name) if base_dir.exists() else None
            if f is not None:
                base = f.read_bytes()
        d = base if base is not None else game.data(name)
        if name.endswith(".PLB"):
            b = bytearray(d); ents = plb_entries(d)
            for key, px in lst:
                t, w, h, pp, pxo = ents[key[2]]; b[pxo:pxo + w * h] = px
            out[name] = bytes(b)
        elif name.endswith(".NPC"):
            recs = npc_frames(d); new = {}
            for key, px in lst:
                o = recs[key[2]][key[3]]
                w, h, x, y = struct.unpack_from("<HHhh", d, o)
                new[o] = sprite_encode(w, h, x, y, px)
            out[name] = npc_rebuild(d, new)
        elif name == "ANIMS.ANI":
            new = {}
            for key, px in lst:
                fo = ani_record(d, key[1])[3][key[2]]
                w, h, x, y = struct.unpack_from("<HHhh", d, fo)
                if isinstance(px, Pix):                   # resized frame: new header
                    w, h, x, y = px.w, px.h, px.x, px.y
                new[fo] = sprite_encode(w, h, x, y, px)
            out[name] = ani_rebuild(d, new)
        elif name == "ITEMS.ITM":
            items = {it["index"]: it for it in items_parse(d)}
            icons, sprites = {}, {}
            for key, px in lst:
                it = items[key[1]]
                if key[0] == "icon":
                    icons[key[1]] = px
                else:
                    w, h, x, y = struct.unpack_from("<HHhh", d, it["sprite_at"])
                    sprites[key[1]] = sprite_encode(w, h, x, y, px)
            out[name] = items_rebuild(d, icons, sprites)
        elif name == "TITEL.ACF":
            out[name] = acf_rebuild(d, {key[1]: px for key, px in lst})
        elif name == "MASKS.MSK":
            b = bytearray(d)
            for key, px in lst:
                b[key[1]:key[1] + len(px)] = px
            out[name] = bytes(b)
        else:                                            # .ANM
            b = bytearray(d); n, w, h, fo = anm_parse(d)
            for key, px in lst:
                b[fo[key[2]]:fo[key[2]] + w * h] = px
            out[name] = bytes(b)
    return out


# ══════════════════════════════════════════════════════════════════════════
# Window
# ══════════════════════════════════════════════════════════════════════════

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from PIL import Image, ImageTk
except ImportError:                                   # pragma: no cover
    Image = None

APP_NAME = "Tom Long Asset Editor"
BG = "#1a1d23"; BG_SIDE = "#141720"; BG_CARD = "#1e2230"; FG = "#f0ece4"; FG_DIM = "#8090a8"
BORDER = "#3d4455"; SEL = "#1e3050"; ACCENT = "#3a80d0"; EDITED = "#e0b050"
CHECK_A, CHECK_B = (46, 50, 60), (36, 40, 48)
ZOOMS = ["Fit", "1×", "2×", "3×", "4×", "6×", "8×"]


def _flat(pal):
    return [c for rgb in pal for c in rgb]


class AssetEditor(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1360x820")
        self.minsize(1100, 640)
        self.configure(bg=BG)
        self.game: Game | None = None
        self.edits: dict = {}                 # frame key -> new indices
        self.node: dict[str, object] = {}     # tree item id -> Group | Frame
        self.frame_item: dict = {}            # frame key -> tree item id
        self.parent_of: dict[str, list] = {}
        self.sel = None                       # selected Group | Frame
        self.sel_path: list[Group] = []
        self.playing = False
        self._after = None
        self._anim_i = 0
        self._photo = None
        self.out_dir: Path | None = None
        self._pal_override = None
        self._build_ui()
        self._set_enabled(False)

    # ── UI ───────────────────────────────────────────────────────────────

    def _button(self, parent, text, cmd, primary=False):
        return tk.Button(parent, text=text, command=cmd, relief="flat", bd=0, padx=12, pady=5,
                         bg="#1e4a7a" if primary else "#2c3140", fg="#ffffff" if primary else FG,
                         activebackground="#255a90" if primary else "#3a4155", activeforeground=FG,
                         disabledforeground="#556", highlightthickness=1, highlightbackground=BORDER,
                         font=("Segoe UI", 10), cursor="hand2")

    def _build_ui(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("Treeview", background=BG_SIDE, fieldbackground=BG_SIDE, foreground=FG,
                     bordercolor=BG_SIDE, rowheight=22, font=("Segoe UI", 10))
        st.map("Treeview", background=[("selected", SEL)], foreground=[("selected", "#ffffff")])
        st.configure("Vertical.TScrollbar", background=BORDER, troughcolor=BG_SIDE, bordercolor=BG_SIDE, arrowcolor=FG_DIM)
        st.configure("Horizontal.TScrollbar", background=BORDER, troughcolor=BG_SIDE, bordercolor=BG_SIDE, arrowcolor=FG_DIM)
        st.configure("TCombobox", fieldbackground="#2c3140", background="#2c3140", foreground=FG, arrowcolor=FG)
        st.map("TCombobox", fieldbackground=[("readonly", "#2c3140")], foreground=[("readonly", FG)],
               selectbackground=[("readonly", "#2c3140")], selectforeground=[("readonly", FG)])
        self.option_add("*TCombobox*Listbox.background", BG_SIDE)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", SEL)

        self.status = tk.Label(self, text="Open the game folder to begin.", bg=BG_SIDE, fg="#a0a8b8",
                               font=("Segoe UI", 9), anchor="w", padx=8, pady=3)
        self.status.pack(side="bottom", fill="x")

        bar = tk.Frame(self, bg=BG); bar.pack(side="top", fill="x", padx=10, pady=8)
        self.b_open = self._button(bar, "Open game folder…", self._open); self.b_open.pack(side="left")
        tk.Frame(bar, bg=BG, width=18).pack(side="left")
        self.b_export = self._button(bar, "Export selected…", self._export_selected); self.b_export.pack(side="left")
        self.b_import = self._button(bar, "Import PNG…", self._import_png); self.b_import.pack(side="left", padx=6)
        self.b_revert = self._button(bar, "Revert", self._revert); self.b_revert.pack(side="left")
        tk.Frame(bar, bg=BG, width=18).pack(side="left")
        self.b_export_all = self._button(bar, "Export all…", self._export_all); self.b_export_all.pack(side="left")
        self.b_import_dir = self._button(bar, "Import folder…", self._import_folder); self.b_import_dir.pack(side="left", padx=6)
        self.b_save = self._button(bar, "Save changed game files…", self._save, primary=True); self.b_save.pack(side="right")
        self.edit_label = tk.Label(bar, bg=BG, fg=EDITED, font=("Segoe UI", 10)); self.edit_label.pack(side="right", padx=10)

        body = tk.Frame(self, bg=BG); body.pack(fill="both", expand=True)
        left = tk.Frame(body, bg=BG_SIDE, width=430); left.pack(side="left", fill="y"); left.pack_propagate(False)
        self.tree = ttk.Treeview(left, show="tree", selectmode="browse")
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y"); self.tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        self.tree.tag_configure("edited", foreground=EDITED)
        self.tree.tag_configure("group", foreground="#c8d0e0")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        right = tk.Frame(body, bg=BG); right.pack(side="left", fill="both", expand=True, padx=14, pady=(0, 8))
        ctl = tk.Frame(right, bg=BG); ctl.pack(fill="x", pady=(0, 6))
        tk.Label(ctl, text="Zoom", bg=BG, fg=FG_DIM, font=("Segoe UI", 9)).pack(side="left")
        self.zoom = ttk.Combobox(ctl, values=ZOOMS, width=5, state="readonly"); self.zoom.set("Fit")
        self.zoom.pack(side="left", padx=(4, 14)); self.zoom.bind("<<ComboboxSelected>>", lambda e: self._render())
        tk.Label(ctl, text="Palette", bg=BG, fg=FG_DIM, font=("Segoe UI", 9)).pack(side="left")
        self.pal_box = ttk.Combobox(ctl, width=34, state="readonly")
        self.pal_box.pack(side="left", padx=(4, 14)); self.pal_box.bind("<<ComboboxSelected>>", self._on_palette)
        self.b_play = self._button(ctl, "▶ Play", self._toggle_play); self.b_play.pack(side="left")
        tk.Label(ctl, text="fps", bg=BG, fg=FG_DIM, font=("Segoe UI", 9)).pack(side="left", padx=(10, 2))
        self.fps = tk.Spinbox(ctl, from_=1, to=30, width=3, bg="#2c3140", fg=FG, buttonbackground="#2c3140",
                              relief="flat", highlightthickness=0, insertbackground=FG)
        self.fps.delete(0, "end"); self.fps.insert(0, "8"); self.fps.pack(side="left")
        tk.Label(ctl, text="Resize anchor", bg=BG, fg=FG_DIM, font=("Segoe UI", 9)).pack(side="left", padx=(18, 4))
        self.anchor = ttk.Combobox(ctl, values=ANCHORS, width=13, state="readonly"); self.anchor.set("bottom-centre")
        self.anchor.pack(side="left")

        cv = tk.Frame(right, bg=BORDER); cv.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(cv, bg="#101216", highlightthickness=0)
        xs = ttk.Scrollbar(cv, orient="horizontal", command=self.canvas.xview)
        ys = ttk.Scrollbar(cv, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=xs.set, yscrollcommand=ys.set)
        xs.pack(side="bottom", fill="x"); ys.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True, padx=1, pady=1)
        self.canvas.bind("<Configure>", lambda e: self.zoom.get() == "Fit" and not self.playing and self._render())

        self.info = tk.Label(right, bg=BG_CARD, fg="#b0b8c8", font=("Segoe UI", 10), anchor="nw", justify="left",
                             padx=12, pady=8, wraplength=860)
        self.info.pack(fill="x", pady=(8, 0))

    def _set_enabled(self, on):
        s = "normal" if on else "disabled"
        for b in (self.b_export, self.b_import, self.b_revert, self.b_export_all, self.b_import_dir, self.b_save, self.b_play):
            b.configure(state=s)

    # ── loading ──────────────────────────────────────────────────────────

    def _open(self):
        if Image is None:
            messagebox.showerror(APP_NAME, "This tool needs Pillow:\n\n    pip install pillow")
            return
        folder = filedialog.askdirectory(title="Open the Tom Long game folder")
        if not folder:
            return
        try:
            game = Game(Path(folder))
        except Exception as ex:
            messagebox.showerror("Can't read the game files", str(ex))
            return
        self._stop()
        self.game, self.edits = game, {}
        self.tree.delete(*self.tree.get_children())
        self.node.clear(); self.frame_item.clear(); self.parent_of.clear()
        for g in game.groups:
            self._insert_group("", g, [])
        self.pal_ids = list(game.palettes)
        self.pal_box.configure(values=[game.pal_names[p] for p in self.pal_ids])
        self._set_enabled(True)
        self._update_edit_count()
        n = sum(1 for _ in game.all_frames())
        self.title(f"{APP_NAME} — {Path(folder).name}")
        self.status.configure(text=f"{folder}  ·  {n} images")
        first = self.tree.get_children()[0]
        self.tree.selection_set(first); self.tree.focus(first)

    def _insert_group(self, parent, g: Group, path):
        iid = self.tree.insert(parent, "end", text=g.label, open=False, tags=("group",))
        self.node[iid] = g; self.parent_of[iid] = path + [g]
        for c in g.children:
            self._insert_group(iid, c, path + [g])
        for f in g.frames:
            fid = self.tree.insert(iid, "end", text=f.label)
            self.node[fid] = f; self.parent_of[fid] = path + [g]; self.frame_item[f.key] = fid
        return iid

    # ── helpers ──────────────────────────────────────────────────────────

    def pixels(self, f: Frame) -> bytes:
        return self.edits.get(f.key) or f.original()

    def dims(self, f: Frame) -> tuple[int, int, int, int]:
        """(w, h, x, y) — a resized animation frame has its own size and offset."""
        e = self.edits.get(f.key)
        if isinstance(e, Pix):
            return e.w, e.h, e.x, e.y
        return f.w, f.h, f.x, f.y

    def _frames_under(self, obj) -> list[Frame]:
        if isinstance(obj, Frame):
            return [obj]
        out = list(obj.frames)
        for c in obj.children:
            out += self._frames_under(c)
        return out

    def _palette_id(self, f: Frame) -> str:
        return self._pal_override or f.pal or self.pal_ids[0]

    def _image(self, f: Frame, pid: str | None = None):
        w, h, _, _ = self.dims(f)
        im = Image.frombytes("P", (w, h), self.pixels(f))
        im.putpalette(_flat(self.game.palettes[pid or self._palette_id(f)]))
        return im

    def _rgba(self, f: Frame, pid=None):
        im = self._image(f, pid).convert("RGBA")
        if f.transparent:
            mask = Image.frombytes("L", im.size, bytes(0 if v == 0 else 255 for v in self.pixels(f)))
            im.putalpha(mask)
        return im

    @staticmethod
    def _checker(w, h, cell=8):
        tile = Image.new("RGB", (2, 2), CHECK_A)
        tile.putpixel((1, 0), CHECK_B); tile.putpixel((0, 1), CHECK_B)
        tile = tile.resize((cell * 2, cell * 2), Image.NEAREST)
        im = Image.new("RGB", (w, h), CHECK_A)
        for y in range(0, h, cell * 2):
            for x in range(0, w, cell * 2):
                im.paste(tile, (x, y))
        return im

    def _update_edit_count(self):
        n = len(self.edits)
        self.edit_label.configure(text=f"{n} image{'s' if n != 1 else ''} changed" if n else "")

    def _mark(self, f: Frame):
        iid = self.frame_item[f.key]
        label = f.label
        e = self.edits.get(f.key)
        if isinstance(e, Pix):
            label = label.replace(f"{f.w}×{f.h}", f"{e.w}×{e.h} (was {f.w}×{f.h})")
        self.tree.item(iid, text=("✎ " if f.key in self.edits else "") + label,
                       tags=("edited",) if f.key in self.edits else ())

    # ── selection & preview ──────────────────────────────────────────────

    def _on_select(self, _e=None):
        sel = self.tree.selection()
        if not sel:
            return
        self._stop()
        self.sel = self.node[sel[0]]
        self.sel_path = self.parent_of[sel[0]]
        self._pal_override = None
        f = self._frames_under(self.sel)[:1]
        pal = f[0].pal if f else None
        if pal in self.pal_ids:
            self.pal_box.current(self.pal_ids.index(pal))
        self.b_play.configure(state="normal" if isinstance(self.sel, Group) and self.sel.anim else "disabled")
        if isinstance(self.sel, Group) and self.sel.anim:
            self.fps.delete(0, "end"); self.fps.insert(0, str(self.sel.anim.get("fps", 8)))
        self._render()

    def _on_palette(self, _e=None):
        self._pal_override = self.pal_ids[self.pal_box.current()]
        self._render()

    def _zoom_for(self, w, h):
        z = self.zoom.get()
        if z == "Fit":
            cw = max(self.canvas.winfo_width() - 20, 50); ch = max(self.canvas.winfo_height() - 20, 50)
            return max(1, min(8, cw // max(w, 1), ch // max(h, 1)))
        return int(z.rstrip("×"))

    def _show(self, im, zoom=None):
        z = zoom or self._zoom_for(*im.size)
        big = im.resize((im.size[0] * z, im.size[1] * z), Image.NEAREST)
        bg = self._checker(*big.size, cell=8)
        if big.mode == "RGBA":
            bg.paste(big, (0, 0), big)
        else:
            bg = big.convert("RGB")
        self._photo = ImageTk.PhotoImage(bg)
        c = self.canvas
        c.delete("all")
        cw, ch = c.winfo_width(), c.winfo_height()
        x = max((cw - bg.size[0]) // 2, 0); y = max((ch - bg.size[1]) // 2, 0)
        c.create_image(x, y, image=self._photo, anchor="nw")
        c.configure(scrollregion=(0, 0, max(cw, bg.size[0]), max(ch, bg.size[1])))
        return z

    def _render(self):
        if not self.game or self.sel is None:
            return
        obj = self.sel
        if isinstance(obj, Frame):
            z = self._show(self._rgba(obj))
            self._info_frame(obj, z)
        elif obj.anim:
            self._anim_i = 0
            self._draw_anim_frame(first=True)
        elif obj.frames:
            self._sheet(obj.frames)
        else:
            self.canvas.delete("all")
            n = len(self._frames_under(obj)); e = sum(1 for f in self._frames_under(obj) if f.key in self.edits)
            self.info.configure(text=f"{obj.label}\n{n} images" + (f", {e} changed" if e else "") +
                                ".  Open the group to browse, or use Export selected… to export all of them.")

    def _sheet(self, frames):
        ims = [self._rgba(f) for f in frames[:120]]
        pad = 6
        cw = max(self.canvas.winfo_width() - 20, 200)
        z = 1 if self.zoom.get() == "Fit" else self._zoom_for(1, 1)
        if self.zoom.get() == "Fit":
            maxw = max(i.size[0] for i in ims)
            z = max(1, min(4, (cw // max(1, min(len(ims), 6))) // max(maxw + pad, 1)))
        x = y = pad; rowh = 0; places = []
        for im in ims:
            w, h = im.size[0] * z, im.size[1] * z
            if x + w + pad > cw and x > pad:
                x = pad; y += rowh + pad; rowh = 0
            places.append((x, y)); x += w + pad; rowh = max(rowh, h)
        W = max(cw, max(px + i.size[0] * z for (px, _), i in zip(places, ims)) + pad)
        H = y + rowh + pad
        sheet = Image.new("RGBA", (W, H), (16, 18, 22, 255))
        for (px, py), im in zip(places, ims):
            big = im.resize((im.size[0] * z, im.size[1] * z), Image.NEAREST)
            chk = self._checker(*big.size).convert("RGBA")
            chk.paste(big, (0, 0), big)
            sheet.paste(chk, (px, py))
        self._show(sheet, zoom=1)
        e = sum(1 for f in frames if f.key in self.edits)
        grp = self.sel.label
        self.info.configure(text=f"{grp}\n{len(frames)} images" + (f", {e} changed" if e else "") +
                            (f" (showing first 120)" if len(frames) > 120 else "") +
                            f"  ·  palette: {self.game.pal_names[self._palette_id(frames[0])]}")

    def _info_frame(self, f: Frame, z):
        where = " › ".join(g.label.split("   ")[0] for g in self.sel_path)
        w, h, x, y = self.dims(f)
        size = f"{w}×{h}" + (f" (original {f.w}×{f.h})" if (w, h) != (f.w, f.h) else "")
        lines = [f"{where} › {f.label}",
                 f"{size} pixels   ·   zoom {z}×   ·   palette: {self.game.pal_names[self._palette_id(f)]}"]
        if f.key[0] in ("npc", "ani", "isprite"):
            lines.append(f"draw offset {x},{y}   ·   colour 0 is transparent")
        if resizable(f):
            lines.append(f"Animation frame: may be imported at another size (width a multiple of 4); "
                         f"placed by the Resize anchor ({self.anchor.get()}).")
        if f.note:
            lines.append(f.note)
        lines.append("Export file name: " + export_name(f.key))
        if f.key in self.edits:
            lines.append("✎ Changed — will be written by “Save changed game files”.  Revert restores the original.")
        self.info.configure(text="\n".join(lines))

    # ── animation ────────────────────────────────────────────────────────

    def _anim_layout(self, g: Group):
        """[(frame, x, y)] per step and the bounding box."""
        a = g.anim
        if a["kind"] == "ani":
            steps = [(g.frames[fr], self.dims(g.frames[fr])[2] + dx, self.dims(g.frames[fr])[3] + dy)
                     for fr, dx, dy in a["steps"] if 0 <= fr < len(g.frames)]
        else:
            steps = [(f, self.dims(f)[2], self.dims(f)[3]) for f in g.frames]
        if not steps:
            return [], (0, 0, 1, 1)
        x0 = min(x for f, x, y in steps); y0 = min(y for f, x, y in steps)
        x1 = max(x + self.dims(f)[0] for f, x, y in steps); y1 = max(y + self.dims(f)[1] for f, x, y in steps)
        return steps, (x0, y0, x1, y1)

    def _draw_anim_frame(self, first=False):
        g = self.sel
        if not isinstance(g, Group) or not g.anim:
            return
        a = g.anim
        if a["kind"] == "acf":
            if first or not hasattr(self, "_acf_canvas") or self._anim_i == 0:
                self._acf_canvas = Image.new("RGB", (320, 160))
                if not first:
                    pass
            f = g.frames[self._anim_i]
            self._acf_canvas.paste(self._image(f).convert("RGB"), (f.x, f.y))
            z = self._show(self._acf_canvas)
            delay = max(a["rects"][self._anim_i][4], 40)
            self._anim_i = (self._anim_i + 1) % len(g.frames)
            if self._anim_i == 0:
                delay = 2500
        else:
            steps, (x0, y0, x1, y1) = self._anim_layout(g)
            if not steps:
                return
            f, x, y = steps[self._anim_i % len(steps)]
            im = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
            fr = self._rgba(f); im.paste(fr, (x - x0, y - y0), fr)
            z = self._show(im)
            self._anim_i = (self._anim_i + 1) % len(steps)
            try:
                delay = int(1000 / max(1, int(self.fps.get())))
            except ValueError:
                delay = 125
        shown = (self._anim_i - 1) % max(len(g.frames), 1)
        self.info.configure(text=f"{g.label}\n{len(g.frames)} frames   ·   zoom {z}×   ·   palette: "
                                 f"{self.game.pal_names[self._palette_id(g.frames[0])]}"
                                 + ("   ·   ▶ playing" if self.playing else "   ·   press Play to animate"))
        if self.playing:
            self._after = self.after(delay, self._draw_anim_frame)

    def _toggle_play(self):
        if self.playing:
            self._stop()
        else:
            self.playing = True; self.b_play.configure(text="■ Stop")
            self._anim_i = 0
            if hasattr(self, "_acf_canvas"):
                del self._acf_canvas
            self._draw_anim_frame(first=True)

    def _stop(self):
        self.playing = False
        if self._after:
            self.after_cancel(self._after); self._after = None
        self.b_play.configure(text="▶ Play")

    # ── export ───────────────────────────────────────────────────────────

    def _save_png(self, f: Frame, path: Path, pid=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        im = self._image(f, pid)
        if f.transparent:
            im.info["transparency"] = 0
            im.save(path, transparency=0)
        else:
            im.save(path)

    def _save_gif(self, g: Group, path: Path):
        a = g.anim
        pal = _flat(self.game.palettes[self._palette_id(g.frames[0])])
        frames, durations = [], []
        if a["kind"] == "acf":
            canvas = Image.new("P", (320, 160)); canvas.putpalette(pal)
            for f, r in zip(g.frames, a["rects"]):
                canvas.paste(self._image(f), (f.x, f.y))
                frames.append(canvas.copy()); durations.append(max(r[4], 40))
            durations[-1] = 2500
            frames[0].save(path, save_all=True, append_images=frames[1:], duration=durations, loop=0)
            return
        steps, (x0, y0, x1, y1) = self._anim_layout(g)
        try:
            ms = int(1000 / max(1, int(self.fps.get())))
        except ValueError:
            ms = 125
        for f, x, y in steps:
            im = Image.new("P", (x1 - x0, y1 - y0), 0); im.putpalette(pal)
            fr = self._image(f)
            mask = Image.frombytes("L", fr.size, bytes(0 if v == 0 else 255 for v in self.pixels(f)))
            im.paste(fr, (x - x0, y - y0), mask)
            frames.append(im)
        if frames:
            frames[0].save(path, save_all=True, append_images=frames[1:], duration=ms, loop=0,
                           transparency=0, disposal=2)

    def _save_gpl(self, pid: str, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["GIMP Palette", f"Name: Tom Long — {self.game.pal_names[pid]}", "Columns: 16", "#"]
        lines += [f"{r:3d} {g:3d} {b:3d}\tindex {i}" for i, (r, g, b) in enumerate(self.game.palettes[pid])]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _export_frames(self, frames, root: Path, groups=()):
        pals = set()
        for f in frames:
            self._save_png(f, root / export_name(f.key)); pals.add(self._palette_id(f))
        for g in groups:
            if g.anim and g.frames:
                sub = export_name(g.frames[0].key).rsplit("/", 1)[0]
                name = re.sub(r"[^A-Za-z0-9_-]+", "_", g.label.split("(")[0].strip()).strip("_").lower()
                self._save_gif(g, root / sub / f"{name}.gif")
        for pid in pals:
            self._save_gpl(pid, root / "palettes" / f"{pid}.gpl")
        return len(frames)

    def _groups_under(self, obj):
        if isinstance(obj, Frame):
            return []
        out = [obj]
        for c in obj.children:
            out += self._groups_under(c)
        return out

    def _export_selected(self):
        if self.sel is None:
            return
        if isinstance(self.sel, Frame):
            f = self.sel
            path = filedialog.asksaveasfilename(title="Export image", defaultextension=".png",
                                                initialfile=export_name(f.key).rsplit("/", 1)[-1],
                                                filetypes=[("PNG image", "*.png")])
            if path:
                self._save_png(f, Path(path))
                self.status.configure(text=f"Exported {Path(path).name} (indexed PNG, palette: {self.game.pal_names[self._palette_id(f)]})")
            return
        out = filedialog.askdirectory(title="Export into folder")
        if not out:
            return
        n = self._export_frames(self._frames_under(self.sel), Path(out), self._groups_under(self.sel))
        self.status.configure(text=f"Exported {n} images to {out}")
        messagebox.showinfo(APP_NAME, f"Exported {n} images" + (" and animations (GIF)" if any(g.anim for g in self._groups_under(self.sel)) else "")
                            + f" to\n{out}\n\nEdit the PNGs keeping their palette, then use Import folder…")

    def _export_all(self):
        out = filedialog.askdirectory(title="Export every asset into folder")
        if not out:
            return
        self.config(cursor="watch"); self.update()
        try:
            frames = [f for _, f in self.game.all_frames()]
            groups = [g for top in self.game.groups for g in self._groups_under(top)]
            saved_override, self._pal_override = self._pal_override, None
            n = self._export_frames(frames, Path(out), groups)
            self._pal_override = saved_override
        finally:
            self.config(cursor="")
        self.status.configure(text=f"Exported all {n} images to {out}")
        messagebox.showinfo(APP_NAME, f"Exported {n} images, the animations as GIFs and the palettes (.gpl) to\n{out}")

    # ── import ───────────────────────────────────────────────────────────

    def _apply(self, f: Frame, px: bytes):
        if px == f.original() and not isinstance(px, Pix):
            self.edits.pop(f.key, None)
        else:
            self.edits[f.key] = px
        self._mark(f)

    def _import_png(self):
        if not isinstance(self.sel, Frame):
            messagebox.showinfo(APP_NAME, "Select a single image in the tree to import a PNG over it.\n"
                                          "To import many at once, use Import folder… with an exported folder.")
            return
        f = self.sel
        path = filedialog.askopenfilename(title=f"Import PNG for {f.label}", filetypes=[("PNG image", "*.png")])
        if not path:
            return
        px, msg = read_indexed_png(path, f, self.game, self.anchor.get())
        if px is None:
            messagebox.showerror("Can't import this PNG", f"{Path(path).name}: {msg}")
            return
        self._apply(f, px)
        self._update_edit_count(); self._render()
        self.status.configure(text=f"Imported {Path(path).name} — {msg}" + ("" if f.key in self.edits else " (identical to the original)"))

    def _import_folder(self):
        src = filedialog.askdirectory(title="Import edited PNGs from an exported folder")
        if not src:
            return
        root = Path(src); changed = same = 0; errors = []
        self.config(cursor="watch"); self.update()
        try:
            for _, f in self.game.all_frames():
                p = root / export_name(f.key)
                if not p.exists():
                    continue
                px, msg = read_indexed_png(str(p), f, self.game, self.anchor.get())
                if px is None:
                    errors.append(f"{export_name(f.key)}: {msg}")
                    continue
                before = self.pixels(f); before_dims = self.dims(f)
                self._apply(f, px)
                if px != before or self.dims(f) != before_dims:
                    changed += 1
                else:
                    same += 1
        finally:
            self.config(cursor="")
        self._update_edit_count(); self._render()
        text = f"{changed} image(s) changed, {same} unchanged."
        if errors:
            text += f"\n\n{len(errors)} file(s) refused:\n" + "\n".join(errors[:15]) + ("\n…" if len(errors) > 15 else "")
            messagebox.showwarning("Import folder", text)
        else:
            messagebox.showinfo("Import folder", text)
        self.status.configure(text=text.split("\n")[0])

    def _revert(self):
        if self.sel is None:
            return
        frames = [f for f in self._frames_under(self.sel) if f.key in self.edits]
        for f in frames:
            del self.edits[f.key]; self._mark(f)
        self._update_edit_count(); self._render()
        self.status.configure(text=f"Reverted {len(frames)} image(s) to the original.")

    # ── save ─────────────────────────────────────────────────────────────

    def _save(self):
        if not self.edits:
            messagebox.showinfo(APP_NAME, "Nothing has been changed yet.")
            return
        out = filedialog.askdirectory(title="Folder for the changed game files (not the game folder itself)",
                                      initialdir=str(self.out_dir or self.game.folder.parent))
        if not out:
            return
        out = Path(out)
        if out.resolve() == self.game.folder.resolve():
            messagebox.showerror(APP_NAME, "Choose a different folder: saving into the game folder would overwrite "
                                           "the originals this editor reads from.")
            return
        self.out_dir = out
        try:
            existing = {file_of(k) for k in self.edits if _find(out, file_of(k)) is not None}
            files = build_files(self.game, self.edits, out)
        except Exception as ex:
            messagebox.showerror("Save failed", str(ex))
            return
        for name, data in files.items():
            (out / name).write_bytes(data)
        note = ""
        if existing:
            note = ("\n\nThese files already existed there and were updated in place "
                    "(e.g. a translated ITEMS.ITM keeps its translation):\n" + ", ".join(sorted(existing)))
        self.status.configure(text=f"Saved {len(files)} file(s) to {out}")
        messagebox.showinfo(APP_NAME, f"Wrote {len(files)} file(s) to\n{out}\n\n" + ", ".join(sorted(files)) + note)


if __name__ == "__main__":
    AssetEditor().mainloop()
