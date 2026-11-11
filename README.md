# Tom Long - The Time Adventure (1994) – Translation Project

Reverse engineering of the German DOS adventure *Tom Long – The Time Adventure* and the tools built to
translate it into English and edit its graphics.

**Current tools** (both standard Python + tkinter; the Asset Editor also needs Pillow):

| Tool | File | Purpose |
|---|---|---|
| **Tom Long Translator** | `Tom_Long_Translator.py` | Translate every in-game text file (dialogues, item names and speech, script speech, exit names) and build the translated game files |
| **Tom Long Asset Editor** | `Tom_Long_Asset_Editor.py` | Browse, preview and play all graphics; export PNG/GIF/palettes; re-import edited PNGs; build the changed game files |

---

## Contents

1. [The engine at a glance](#1-the-engine-at-a-glance)
2. [File inventory](#2-file-inventory)
3. [Graphics formats](#3-graphics-formats)
4. [Rooms: ROM byte-code, walk maps, masks](#4-rooms-rom-byte-code-walk-maps-masks)
5. [Objects and scripts: ITEMS.ITM, PROCS.CMD, the script VM](#5-objects-and-scripts)
6. [Dialogues: DLG](#6-dialogues-dlg)
7. [Other files: flags, saves, music, intro](#7-other-files)
8. [Text and translation facts](#8-text-and-translation-facts)
9. [Tom Long Translator](#9-tom-long-translator)
10. [Tom Long Asset Editor](#10-tom-long-asset-editor)
11. [Files, and what is obsolete](#11-files-and-what-is-obsolete)
12. [Open questions and next steps](#12-open-questions-and-next-steps)
13. [Code address reference](#13-code-address-reference)

---

## 1. The engine at a glance

- **ENGINE.EXE** is a 16-bit real-mode DOS MZ executable, **not packed**, built with **Borland Turbo C++ 1990**
  (large memory model). INTRO.EXE and CONFIG.EXE are separate programs.
- Every asset is opened **by name** through the C runtime (`fopen`/`fread`/`fseek` or `open`/`read`).
  There is **no archive, no encryption**, and the only compression is a simple RLE for sprites
  (plus PowerPacker in the intro, which INTRO.EXE handles). All integers are little-endian.
- **Addresses** in this document are `segment:offset` in the load module. Ghidra's MZ loader adds its load segment,
  **0x1000** (confirmed), so `07A2:0086` appears as `17A2:0086`. The data segment (DGROUP) is `11E7`.
- **Graphics**: VGA 256 colours in **Mode X** (planar, 4 planes). Palettes are **6-bit** values written straight to
  the DAC (`1082:00C0`); many stored bytes carry junk in the top two bits, which the hardware ignores — always
  mask with `& 0x3F`.
- **Screen palette of a room** = the room background's palette with **colours 0–15 replaced by the system palette**.
  The system palette is SYSTEM.PLB picture 0's palette with colour 0 forced to black and colour 1 to white
  (`093A:044D`).
- **Text font**: the PC's built-in **VGA BIOS 8×8 font**, fetched with `INT 10h AX=1130h` (`10E5:0001`). It is the
  CP437 character set, so every CP437 character (0x20–0xFE) can be drawn and nothing else.
- **Command line** (`main` = `093A:0300`): exactly one argument is looked at, compared without regard to case.
  `music` → music and sound effects; `fx` → sound effects only (no music); anything else, no argument or more than
  one argument → no sound at all (the music driver isn't even started). Nothing else is read: no other switches,
  no environment variables. At start the engine refuses to run with less than 460 000 bytes of free memory (the
  error text claims 590 KB). The game starts in room `main` and ends when a script goes to room `Exitus`.
  Leftover debug-log routines that write `DEBUG.OUT` (`0000:3344`, `0000:338B`) are never called.
- **Timer**: the engine replaces the PC timer interrupt (INT 8, `11D4:0013`, installed by `11CB:000B`) but never
  reprograms the timer chip, so it fires at the standard **18.2 Hz**. Every second interrupt advances the tick
  counter `DGROUP:F02C` → **1 tick ≈ 0.11 s (9.1 ticks per second)**. The same tick redraws animations and speech
  bubbles, which is why things keep moving while scripts wait.

## 2. File inventory

| File(s) | Loaded by (ENGINE.EXE) | Content |
|---|---|---|
| `GRAFIK.PLB` | `1014:0002`, `1014:00EF` | 23 room backgrounds (PCLL picture library) |
| `SYSTEM.PLB` | same | 5 mouse cursors; picture 0 also supplies the system palette |
| `*.ROM` (22) | `07A2:06F0` → interpreter `07A2:0086` | room definitions (byte-code) incl. walk map, masks, exits |
| `ANIMS.ANI` | `084B:0005` index, `084B:0066` load | 149 room/cut-scene animations, 1 451 frames |
| `*.NPC` (9) | `071D:0076` | characters: 5 animations each, 285 frames in total |
| `*.ANM` (8) | `11A5:0002` | verb buttons, cursors, inventory arrows |
| `MASKS.MSK` | `099B:002B` | 28 foreground masks, addressed from the room files |
| `ITEMS.ITM` | `058A:10E9`, records `058A:0EAB` | 122 objects: name, inventory icon, room sprite, verb scripts |
| `PROCS.CMD` | `033F:0114`, `033F:0003` | 32 global scripts |
| `*.DLG` (58) | `08AF:07D0` load, `08AF:030A` walk | dialogue trees |
| `FLAGS.FLG` | `0356:000B` | the 512-byte flag array's initial state |
| `*.OGM`, `MCCAINGM.OGD`, `MCCAINFX.OGD` | `0B55:0197`, `0B55:000B` | songs (`OMID`) and instrument/FX banks (`ORPH`), passed whole to the built-in music driver (`0B51:xxxx`) |
| `SAVEGAME.001–003` | `035A:0009` write, `035A:00F7` read | save games |
| `VIDEO.SWP`, `DEBUG.OUT` | `08AF:0604`, various | runtime temp/log files, not assets |
| `TITEL.ACF`, `TITEL.ACP`, `TITEL1.PCX`, `TITEL2.PCX` | **INTRO.EXE** `065A:0051` | intro title animation, its palette, two title screens |

## 3. Graphics formats

### 3.1 PLB – picture library (`PCLL`)

```
0x000  char magic[4] = "PCLL"      0x004  u16 version (must be 1)
0x006  u8   count-1                0x008  u32 data size      0x00C  u32 unknown
0x010  { u8 type; u8 pad[3]; u32 offset; } dir[256]
0x810  picture i at 0x810 + dir[i].offset:  u8 palette[768] (6-bit) · u8 pixels[w*h] (raw, row-major)
```

Size comes from the type (table `DGROUP:0F58`): 0 = 320×240, 1 = 320×144, 2 = 320×160, 3 = 320×400, 4 = 320×200,
5 = 480×160, 6 = 480×80, 7 = 352×160, 8 = 400×160, 9 = 640×160, 11 = 32×32, 12 = 16×16.

### 3.2 Sprite frames (NPC, ANIMS.ANI, item room sprites)

```
u16 w, u16 h, s16 x_offset, s16 y_offset
RLE stream:  0x01 n v  = n copies of colour v;  any other byte = one pixel
+ 2 slack bytes after every frame
```

- The stream holds the **four Mode-X planes one after another**: plane p = columns p, p+4, p+8 … Each plane is
  h rows × w/4 bytes (blitter `1147:000E`). Therefore **width must be a multiple of 4** (true for all 1 736 frames).
- **Colour 0 is transparent.** No original frame uses colour 1 (it would need the run form `01 01 01`).
- Runs cross row boundaries freely but never cross from one plane into the next.
- Frames are drawn at *position + (x_offset, y_offset)*; each frame's header is read at every draw, so frames of one
  animation may differ in size (77 of the 149 animations already do). **Confirmed in DOSBox:** enlarging an
  ANIMS.ANI frame works without leftover pixels.

### 3.3 NPC – characters

Header of **5 records × 166 bytes**: `u16 frame_count, u16 w, u16 h, u32 frame_offset[40]` (absolute file offsets),
followed by the frames. The 5 records are the character's animations (walking directions, talking). No frame is
shared between records.

### 3.4 ANIMS.ANI – room animations

`u32 offset[256], u32 size[256]` (149 slots used; unused slots are 0), records contiguous. Each record:

```
u16 frame_count, u16 step_count, u16 flags
s16 step_frame[n], s16 step_dx[n], s16 step_dy[n]    (step_frame < 0 = control step, see below)
frame data
u32 frame_offset[frame_count]                         (relative to the record start)
```

Each animation is loaded into a buffer sized from its own record, so records may grow.

**Flags:** bit 0 = wait for the global start signal (script opcode CD) before playing (7 animations); bit 1 = stay
on the last frame at the end instead of disappearing (1 animation). Without bit 1 an animation that runs off its
last step is removed. There is no "loop" flag: looping is done with control steps.
Animations advance **one step per tick** (9.1 per second, only one control step handled per tick); step dx/dy are
offsets from the animation's origin, not accumulated. The "behind" layer is drawn **just behind the player**
(sort key = player's bottom edge − 1), not behind everything. Details: `spec/02_rendering.md` §4.2.

**Control steps** (a step whose frame number is negative; its dx/dy are ignored):

| step | count | meaning |
|---|---|---|
| −1 | 39 | set the loop mark here |
| −2 | 38 | jump back to the loop mark |
| −3 | 159 | **stop point**: pause here until a script releases the animation (CE; D7 for the player) |
| −4 | 7 | draw in front of everything from now on |
| −5 | 29 | halt and hide; as the first step the animation starts invisible until a script releases it |
| −6 | 1 | back to normal depth sorting (by the sprite's bottom edge) |

In the player's special animation (E0/D5) only −3 (stop point) and −4 (in front) mean anything; −1/−2 are skipped.

Runtime state lives in 12 animation slots at `DGROUP:09EE`; the fields a script can see are "paused at a stop
point", "ended", "hidden", "waiting for start" and the draw layer (behind / sorted / in front).

### 3.5 ANM – UI graphics

`"ANIM", u16 frames, u16 w, u16 h`, then `frames × w*h` raw pixels (verb buttons 16×16, arrows 24×40).

### 3.6 Palettes in practice

Characters use colours 224–255, and those palette entries **differ slightly from room to room** (lighting).
Sprites therefore have no fixed colours: files store palette *indices*, and the room decides the colours.

## 4. Rooms: ROM byte-code, walk maps, masks

A room file is a script executed once when the room is entered. `pad` = unused byte (always 0).
IF/IFNOT blocks run to ENDIF; the file ends at END.

| op | operands | meaning |
|---|---|---|
| 01 | u8 pic | background from GRAFIK.PLB |
| 02 | pad, s16 x, s16 y, char name[8] | add `name.anm` (unused in the shipped rooms) |
| 03 | pad, char song[8] | play `song.ogm` (`none` = silence, `last` = resume) |
| 04 | u8 entry, s16 x1,y1,x2,y2, char room[8], char label[16], s16 spawn_x, spawn_y, walk_x, walk_y | **exit**: hotspot, target room, display name ("Toms Zimmer"); `entry` = how Tom appears in the target room (see below) |
| 05 | – | end of file |
| 06 | pad, s16 len, then u16 bytes_per_row, u16 rows, 1-bit bitmap | **walkable-area map** (MSB = leftmost pixel) |
| 07 | u8 item, s16 x, y, a, b, u8 c | place an ITEMS.ITM object unless gone: x,y = hotspot top-left and sprite position (hotspot size from ITEMS tag 01), a,b = **walk-to point**, c = layer (0 background, 1 attached to an overlapping mask so it is depth-sorted with it, 2 in front — unused, and buggy: never drawn) |
| 08 | pad, s16 x, y, w, h, u32 off | **foreground mask**: w×h bytes at `off` in MASKS.MSK (max 20 per room) |
| 09 / 0F | s16 flag | IF flag set / IF flag clear … 0A |
| 0A | – | ENDIF |
| 0B | pad, s16 x, s16 y, char name[8] | load `name.NPC` standing (anim 4) with its **feet** at x,y |
| 0C | pad, s16 proc | **start-up procedure**: after the room has faded in, run PROCS.CMD procedure `proc` once |
| 0D | pad, s16 anim, s16 x, s16 y | start an ANIMS.ANI animation. Negative `anim` = index −anim−1, drawn **in front**; `x` with bit 0x2000 set = drawn **behind** everything; otherwise depth-sorted |
| 0E | – | keep Tom visible and able to move while the room is set up (only MAIN and CASTLE use it); without it Tom is hidden until the entry walk starts |
| 13 | – | keep the screen dark during Tom's walk-in and switch it on afterwards (default: on before the walk-in) |
| 14 | – | screen stays dark after entry, until a script turns it on (F7) — used by rooms whose start-up procedure does a cut-scene |
| 10 | pad, s16 anim | preload `anim` into Tom's special-animation slot (same as script E0) |
| 12 | s16 x, s16 y | add a **path-finding waypoint** (max 10 per room; see `spec/01_walking.md` §3.5) |

**Entry byte** (exit byte of ROM op 04, and the `dir` byte of script op DB): bits 0–1 = direction Tom walks in
from 40 px outside his spawn point (0 = walking right, 1 = walking left, 2 = walking up/away, 3 = walking down/toward
the viewer); **+4 = nothing at all** (no walk-in, no turn; values 4–7); **+8 = no walk-in, stand in a 3/4 view**
(8 = facing right, 9 = facing left). The shipped exits use 0, 1, 5, 8 and 9. The byte is read as a **signed** value, so **255 = −1 = "no entry"**: Tom simply appears at x,y, keeps
his current facing, can move at once, and the screen comes on straight away. 29 of the 32 GOTO_ROOM calls in the
scripts use 255 (cut-scene jumps such as the time machine); the others use 8 and 9.

**MASKS.MSK** has no header: raw w×h blocks of room pixels (colour 0 = see-through), found only via opcode 08.

## 5. Objects and scripts

### 5.1 ITEMS.ITM

`u32 offset[256]` (list ends at the first 0), 122 records of tagged fields ending with tag 00:

| tag | layout | meaning |
|---|---|---|
| 01 | u8 kind, s16 w, s16 h; if kind = 0: u16 len + sprite frame | **hotspot width and height** (checked against all 112 placements); kind 0 = with a room sprite, otherwise an invisible hotspot |
| 02 | pad, char name[16] | object name — **16-byte field** |
| 03 / 05 / 06 / 08 | script up to C0 | LOOK / TAKE / USE (also use-with) / TALK handler |
| 07 | script up to C0 | runs **after every verb** on the object (not a Walk handler); the "Zum …" exit-like objects use it |
| 04 | pad, u8 icon[32×32] | inventory icon (raw) |
| 09 | u8 dir | direction Tom turns to after walking up to the object: 1 = left, 2 = right, 3 = away, 4 = toward the viewer. Without this tag he turns towards the object |

Scripts contain **no jump offsets** (blocks are skipped with the length table), so records can grow; only the
offset table must be rebuilt. The engine loads the whole file into one buffer (`ITEM:buf`).

### 5.2 PROCS.CMD

`u32 offset[256]`; procedure *i* runs from `offset[i]` to the next non-zero offset (or EOF). 32 procedures.

### 5.3 The script VM (`058A:01D1`, lengths `058A:001F`)

Opcodes C0–F7, every instruction has a **fixed length** (opcode included):

```
C0:1  C1:29 C2:1  C3:2  C4:3  C5:1  C6:2  C7:3  C8:3  C9:3  CA:9  CB:3  CC:1  CD:1  CE:3  CF:3
D0:1  D1:3  D2:3  D3:12 D4:3  D5:1  D6:1  D7:1  D8:1  D9:1  DA:15 DB:14 DC:7  DD:3  DE:5  DF:1
E0:3  E1:1  E2:9  E3:1  E4:1  E5:1  E6:1  E7:1  E8:3  E9:1  EA:1  EB:1  EC:1  ED:9  EE:3  EF:3
F0:2  F1:1  F2:1  F3:3  F4:2  F5:5  F6:1  F7:1
```

All operands are little-endian; `item` = ITEMS.ITM index, −1 means "this object" where noted.

| op | operands | meaning |
|---|---|---|
| C0 | – | END |
| **C1** | char text[24], s16 x, s16 y | **SAY** (no NUL needed). Position: x = −1 → **at the mouse pointer** at the moment the SAY runs (151 of the 157 lines), x = −2 → screen centre (6 lines), otherwise room coordinates; the bubble is centred on that point (x − 4 × length, y − 8) and kept on screen. Non-blocking: shown for 35 ticks |
| C2 | – | take this object (remove from the room, add to the inventory); does **not** mark it gone (D1 does) |
| C3 | u8 item | turn this object into `item` |
| C4 … C5 | s16 item | IF the object was used with `item` … ENDIF |
| C6 | u8 item | add `item` to the inventory |
| C7 / C8 / C9 | s16 flag | set / clear / toggle flag |
| CA | char name[8] | DIALOG: run `name.DLG` (Esc allowed) |
| CB … CC | s16 flag | IF flag … ENDIF (also D4 … CC = IF NOT flag) |
| CD | – | give the start signal to every animation waiting for it (ANI flag bit 0); cleared on leaving the room |
| CE | s16 anim | release animation: continue from a stop point (−3), un-hide a halted one (−5), or leave its loop; also clears "waiting for start" |
| CF | s16 anim | wait until the animation reaches a stop point or ends |
| D0 | – | show Tom again (at his current position) |
| D1 | s16 item | remove `item` from the inventory (−1 = this object), mark it gone and remove it from the room |
| **D2** | s16 n | **WAIT n ticks**, 1 tick ≈ 0.11 s (typical values 35–55 ≈ 4–6 s) |
| D3 | s16 item, s16 x, s16 y, s16 a, s16 b, u8 c | place `item` in the room (same fields as ROM op 07) |
| D4 | s16 flag | IF NOT flag |
| D5 | – | play Tom's special animation (loaded with E0) from the start |
| D6 | – | wait until Tom's special animation reaches a stop point or ends |
| D7 | – | release Tom's special animation from its stop point |
| D8 / D9 | – | hide / show this object's room sprite (the object stays clickable) |
| **DA** | char name[8], s16 flag[3] | DIALOG with no Esc; result −1/−2/−3 sets flag[0]/[1]/[2] and clears the other two |
| DB | char room[8], s16 x, s16 y, u8 entry | GOTO_ROOM; `entry` as for ROM exits (section 4); 255 = no entry movement |
| DC | s16 anim, s16 x, s16 y | start a room animation. Same rules as ROM op 0D, plus: x ≥ 28000 means relative to Tom (x − 30000 + Tom's x, y + Tom's y) |
| DD | s16 anim | stop and remove the animation |
| DE | s16 x, s16 y | walk Tom to x,y in a straight line, **ignoring the walk map**, and wait until he arrives |
| DF | – | hide Tom |
| E0 | s16 anim | load an ANIMS.ANI animation as Tom's special animation (replaces his sprite while it plays) |
| E1 | – | drop Tom's special animation |
| E2 | char name[8] | swap the player character to `name.NPC` |
| E3 | – | enable the Save button (the 7th button in the verb bar). Saving is locked at the start of a new game: only Hardy's scripts (item 25 TALK, item 26 USE) run E3, once flag 18 is set, i.e. when the Hardy dialogue ends with "Viel Glück, Tom…". Loading a saved game also unlocks it (`035A:00F7`); the lock itself isn't stored in save files |
| E4 / E5 | – | show / hide the name label of the object under the mouse (on by default) |
| E6 | – | fade in (colours 16–255; the UI colours 0–15 stay) |
| E7 | – | fade out (colours 16–255; 0–15 stay), 64 vertical retraces ≈ 0.9 s |
| E8 | s16 item | make `item` unclickable (−1 = this object); its sprite **stays visible** |
| E9 / EA | – | hide / show the mouse pointer |
| EB / EC | – | stop / resume scrolling the screen with Tom |
| ED | s16 x1, y1, x2, y2 | walk Tom to x1,y1 then on to x2,y2 (two straight lines, ignoring the walk map) and wait |
| EE / EF | s16 item | draw the object's room sprite in front of the characters / back in the background layer (−1 = this object) |
| F0 | u8 dir | turn Tom: 1 = left, 2 = right, 3 = away, 4 = toward the viewer |
| F1 / F2 | – | disable / enable the room's exits (only objects react to the mouse while disabled) |
| F3 | s16 item | reset the object to its original ITEMS.ITM state |
| F4 | u8 colour | pulse palette colour `colour`: blue 0 → 63 → 0 in steps of 3, one step per frame, then black until the room is left |
| F5 | u8 note, u8 velocity, u8 repeats, u8 delay | **sound effect**: sends a MIDI-style Note-On (status 0x90) to the music driver on the effects channel, instrument 0 of MCCAINFX.OGD; `note` picks the effect (the instrument is a key map, one sound per note), `velocity` 127 = play, 0 = stop that effect (note off). Sent `repeats` times with `delay` ticks after each; nothing is sent when sound is off |
| F6 | – | hide the inventory row |
| F7 | – | turn the screen on (after ROM op 14) |

## 6. Dialogues: DLG

```
node:  int32 target[3]
       char npc_line[]\0            may be empty (END.DLG's root is)
       char choice[]\0  × 1..3      reading stops at the first EMPTY string
```

- **Number of choices** = number of non-empty strings, not of non-zero pointers. Pointer slots after the last choice
  are ignored.
- **Choosing choice *i*:** `target[i] ≥ 0` → jump to that file offset (**0 = back to the root**, used 9 times);
  `target[i] < 0` → leave the dialogue and return the value. Opcode DA turns **−1/−2/−3 into three different outcome
  flags**, so −2 (31×) and −3 (2×) carry quest logic.
- **Rendering** (`08AF:00B0`): word wrap in a 32-column window, breaking at a space or after `-`. Because the engine
  only scans 32 characters ahead and never sees the character after column 32, **a line holds at most 31 visible
  characters** (the German files never exceed 31 either). A word longer than a line stops the drawing of that string.
- **Screen space:** the NPC line, a blank line, then **every choice followed by a blank line**, at 10 px per line in
  a 160-px area → NPC + 1 + Σ choices + (choices − 1) **≤ 16 lines** (the blank after the last choice isn't drawn).
  The German files use exactly up to 16 (3 nodes), which confirms the rule. (Earlier versions of this summary and
  of the Translator forgot the blank lines between choices; fixed.)
- **Limits:** each string ≤ **255 bytes** (256-byte stack buffers); file < **65 532 bytes** (16-bit read + 4-byte header).

## 7. Other files

- **FLAGS.FLG** — the initial 512-byte flag array (`DGROUP:00F8`) used by IF/SET_FLAG.
- **SAVEGAME.00n** — 1 356 bytes: `char room[8], s16 x, s16 y, s16 inventory[30], s16 count, s16 scroll,
  u8 flags[512], u8 item_gone[256], u16 morph[256]` (which ITEMS.ITM record each item currently shows; that is
  the whole "per-object state"). Loading re-applies the morphs, unlocks Save and re-enters the room normally (the
  ROM and its start-up procedure run again; Tom appears at the saved position without a walk-in).
- **Music** — `.OGM` songs ("OMID", tracker-like order list + patterns) and `.OGD` banks ("ORPH", instruments as
  small register scripts) are played by the linked-in **ORPHEUS Player386** (Christoph Winkler / Chris Hülsbeck,
  VISION Engineering 1994) on an **OPL2 at port 0x388**, timed by the **CMOS RTC interrupt (INT 70h, 1024 Hz)** at
  floor(1.6 × tempo) Hz. Fully specified, with a Python re-implementation that matches the original register for
  register: `spec/03_music.md`.
- **Intro (INTRO.EXE)**:
  - `TITEL.ACP` = 768-byte clean 6-bit palette.
  - `TITEL.ACF` = `u16 count` (104), `count × {u16 x, y, w, h; u32 packed_size; u16 delay_ms}`, then one blob per
    frame. Blobs are **PowerPacker 2.0** (Amiga packer) with the `PP20` magic replaced by a u32 packed length:
    `u32 len, u8 eff[4], bit stream (read backwards in 32-bit big-endian words), u24 unpacked_len, u8 skip_bits`.
    Frame 0 = full 320×160 picture; frames 1… overwrite rectangles (the title letters appear one by one).
  - INTRO.EXE reads all frame data with **one read of 64 000 bytes** — the packed total must stay below that
    (original 38 704).
  - The lightning flash is hard-coded: on frames 33–103 INTRO.EXE fades colour 0 from black to sky blue and back;
    after the last frame it waits ~8 s or for a key.
  - `TITEL1/2.PCX` are standard PCX files (any image editor opens them).

## 8. Text and translation facts

### 8.1 Where German text lives

| Source | Strings | Limit | How it's changed |
|---|---|---|---|
| 58 `*.DLG` | 3 216 | 255 bytes; 31 visible chars per line; ≤ 16 lines per node | file rebuilt, any length |
| ITEMS.ITM object names | 118 | 16 characters | overwritten in place |
| ITEMS.ITM speech (SAY) | 151 | 24 characters per bubble | longer lines split into several bubbles |
| PROCS.CMD speech (SAY) | 6 | 24 characters per bubble | same |
| 13 `.ROM` files, exit names | 18 | 15 characters | overwritten in place |
| ENGINE.EXE | 7 verb labels, 5 default replies, 5 menus (Save, Load, Quit, Save failed, Load failed); error messages are left in German on purpose | verbs/replies: German length (fixed addresses); menus: the node's original bytes (strings read one after another, like a DLG node) | Translator (ENGINE.trans.json) |
| CONFIG.EXE, LIESMICH.1ST | sound setup menu, readme | — | not covered |

Total handled by the translator: **3 509 strings**.

### 8.2 Characters

- Allowed: every **CP437** character. German umlauts and ß are fine; English needs nothing special.
- **Prohibited:** anything outside CP437 (`…`, `“ ”`, `’`, `–`, `€` …, which would become `?`) and control
  characters (line breaks, tabs), because the engine has no line breaks and would draw them as symbols (`◙`).
- **Greek:** CP437 has only 12 Greek letters (α Γ π Σ σ τ Φ Θ Ω δ φ ε). A Greek version would need a patched
  font (the font pointers are set in one place, `10E5:0001`) — not attempted.

### 8.3 Behaviour worth knowing

- **Untranslated strings keep the German text**; files without any translation are not written at all.
  A translation that is empty or only spaces also falls back to German.
- **Long speech lines:** a SAY line longer than 24 characters is split at spaces into as few, as even bubbles as
  possible, with `WAIT 40` between them — the same pattern the original scripts use. The pause length
  (`SPLIT_WAIT_TICKS`) is a guess taken from the original scripts and can be tuned.
- **Name and exit fields cannot grow** — the engine reads exactly 16 bytes; only an EXE patch would change that.

## 9. Tom Long Translator

`python Tom_Long_Translator.py` · standard library only.

### 9.1 What it does

- **Open game folder…** loads every text source; a drop-down switches between *Items*, *Scripts*, *Room exits* and
  each of the 58 *Dialogues*, each with its progress (e.g. "(2/269)") and a game-wide total.
- **Sidebar:** dialogue NPC lines / item names as header rows, choices / speech lines indented below;
  ●/○ = translated/untranslated.
- **Context card:** for dialogues the whole node with each choice's destination (`→ END (result −2)`,
  `→ 0x0000 (back to start)`); for an item name all texts of that object; for a speech line (items and PROCS.CMD)
  the **whole script around it**, with opcode names, arguments, item names, indented IF blocks, a short note on the
  less obvious opcodes, and the current line highlighted.
- **Two panels, "as in the game":** German and English side by side, **32 fixed-width columns**, showing the game's
  **own line breaks** (display-only, never saved): exact engine word-wrap for dialogues, one line per bubble for
  speech, one line for names. The panels never scroll sideways.
- **Fit line** under the editor, live: bytes, lines on screen (dialogues), characters / bubble preview (speech),
  characters (names, exits), and any prohibited characters — red when something won't fit.
- **Editing:** Enter is blocked (no line breaks in the game); right-click menus (Undo, Redo, Cut, Copy, Paste,
  Delete, Select All — German panel: Copy, Select All) and the usual shortcuts; copying never includes the display
  breaks; pasting turns line breaks/tabs into spaces; own undo history (200 steps per line).
- **Autosave on every change** to sidecar files next to the German files:
  `NAME.dlgtrans.json` (same format as the old DLG Translator), `ITEMS.trans.json`, `PROCS.trans.json`,
  `ROOMS.trans.json`, `ENGINE.trans.json`. Closing the window needs no confirmation.
- **ENGINE.EXE** is read only if it is the untouched German file; the build writes a translated ENGINE.EXE
  (text only, no bug fixes). Menu texts may be moved inside their node; all other texts keep their address.
- **Dialogue tree** (dialogues only): the whole conversation as an expandable tree, walked breadth-first from the
  first line. Each exchange is written out once, where the player first reaches it, numbered #1, #2…; other ways
  to it are ↪ links (301 of the 1 168 exchanges are reachable from more than one choice, and writing every path
  out would run past a million branches in DIAL18). Endings show the result and, for dialogues started with DA,
  the flag the calling script sets (with CA the result is ignored). Translation shown where there is one,
  German (orange) otherwise; double-click edits a line or follows a link; updates as you type. **Export HTML…**
  (this dialogue) / **Export all dialogues…** write one self-contained page with collapsible branches and links.
- **Play through**: the conversation in a 32×16 game-style window — click a choice to follow it, Back, Restart,
  "Edit this line"; shows the line count and how many lines are still German.
- **Build translated files…** (Ctrl+S) checks everything first (hard errors block, warnings ask), then writes only
  the files that have translations, under their **original names**, into a folder you choose (never the game folder).

### 9.2 Checks

| Kind | Hard error (blocks build) | Warning |
|---|---|---|
| Dialogue | – | > 255 bytes, > 16 lines, word longer than a line, prohibited characters |
| Object name | > 16 chars, prohibited characters | – |
| Exit name | > 15 chars, prohibited characters | – |
| Speech | a single word > 24 chars, prohibited characters | – |

### 9.3 Settings (top of the script)

`SPLIT_WAIT_TICKS = 20` (pause between split bubbles) · `SAY_MAX = 24` · `NAME_MAX = 16` · `LABEL_MAX = 15` ·
`MAX_STRING = 255` · `MAX_LINES = 16` · `WRAP_COLS = 32` (window width; lines hold 31 visible characters).

### 9.4 Verified

Untranslated build is byte-identical for ITEMS.ITM, PROCS.CMD and the ROM files; all 58 DLG files behave identically
after a round trip; long translations keep every item's and procedure's structure; the GUI was exercised end to end.

## 10. Tom Long Asset Editor

`python Tom_Long_Asset_Editor.py` · needs Pillow (`pip install pillow`).

### 10.1 Coverage — 1 997 images

Backgrounds (23), cursors (5), characters (9 files, 285 frames), room animations (149, 1 451 frames),
item icons and room sprites (86), UI graphics (8 files), intro frames (104), foreground masks (28).
Not included by choice: the two title PCX files and the walk maps.

### 10.2 Browsing

- Tree of all assets, each labelled with the rooms that use it.
- Preview with zoom (Fit, 1×–8×, crisp pixels), checkerboard behind transparent colour 0; groups without animation
  show as a sheet.
- **▶ Play** for characters, room animations (including their movement steps), UI graphics and the intro
  (with its original frame delays); fps adjustable.
- **Palette** drop-down: every asset previews in a suitable palette (its own for backgrounds/cursors/intro; the
  screen palette of a room where it appears for sprites, icons and masks) and can be switched to any room.

### 10.3 Workflow

1. **Export all…** / **Export selected…** → 8-bit **indexed PNGs** with the game palette, **GIFs** of every
   animation, and the palettes as **GIMP `.gpl`** files, in a fixed folder layout
   (`backgrounds/`, `characters/<npc>/animN_frameNN.png`, `animations/animNNN/frameNN.png`, `items/`, `ui/`,
   `intro/`, `masks/`, `palettes/`).
2. Edit the PNGs in any editor, keeping them indexed with the same palette.
3. **Import folder…** (matches files by name) or **Import PNG…** for one image. **Revert** undoes changes.
4. **Save changed game files…** writes only the changed files, under their original names, to a folder you choose
   (never the game folder).

### 10.4 Import rules (strict palette)

- Must be an **indexed (8-bit) PNG**; every colour used must equal the game palette's colour at that index
  (any genuine game palette is accepted, trimmed palettes are fine).
- **Same size as the original** — except **ANIMS.ANI frames**, which may change size: any height, width a multiple
  of 4. The **Resize anchor** (bottom-centre / centre / top-left) decides which point stays in place; the new draw
  offset is written into the frame header.
- Refusals name the file and the reason (not indexed, wrong size, width not a multiple of 4, colours not in the
  game palette).

### 10.5 How changes are written

- Backgrounds, cursors, icons, UI graphics and masks: pixels replaced in place.
- Sprites: re-encoded with the game's RLE (the encoder reproduces the original NPC files byte for byte);
  NPC, ANIMS.ANI and ITEMS.ITM offset tables are rebuilt.
- Intro: frames re-packed with a PowerPacker-compatible packer (verified against INTRO.EXE's decoder logic),
  total checked against the 64 000-byte limit.
- **Works with the translator:** if the output folder already contains a file of the same name — typically a
  translated ITEMS.ITM — the image changes are applied **on top of it**, keeping the translation.

### 10.6 Verified

Export → edit (one image per type) → import → save → reload: all edited images read back exactly, all other
1 988 images unchanged; bad PNGs refused; resized animation frame saved with the correct header and **confirmed in
the game**.

## 11. Recommended build order

1. Build the translation with the Translator into an output folder.
2. Save the graphics changes with the Asset Editor **into the same folder**.
3. Copy that folder's files over a *copy* of the game and test in DOSBox.


## 12. Open questions and next steps

- **SPLIT_WAIT_TICKS** — set to 20 ticks (≈ 2.2 s) after play-testing; 40 was too slow.
- **Split bubbles move with the mouse** — a SAY at x = −1 is placed at the pointer's current position, so if the
  player moves the mouse during the pause the next part of a split line appears somewhere else.
- All script, room and animation opcodes are decoded; walking, rendering, timing, interaction, saves and sound are
  specified for a new engine (`ENGINE_SPEC.md`).

### Engine bugs found

- **Save lock only half done.** The verb-bar drawing (`06C0:0192`) hides the Save button until E3 runs, but the
  click handler (`06C0:0330`) and the hover label (`0A1F:0827`) test all 7 buttons regardless, so the invisible Save
  button still shows "Speichern" and opens the save menu.
- **Esc in the Load or Save menu leaves that menu selected as the current verb.** The main loop (`0468:0815`)
  restores the previous verb (`[0x806] = di`) only after a slot was picked; on Esc it jumps straight past the
  restore (`0468:0CEA` Load, `0468:0DC0` Save). From then on every click reopens that menu before the click's own
  action. **Patch:** point both jumps at the restore at `0468:0E28` (file offsets byte `0x616B`: `7F` → `3B` (jump `E9 7F 01` becomes
  `E9 3B 01`), byte `0x6241`: `A9` → `65` (`E9 A9 00` becomes `E9 65 00`)).
- **Optional:** unlock saving from the start by setting the initial value of `DGROUP:032A` to 1
  (file offset `0x12F9A`: `00` → `01`).
- **Ready-made fix:** `TOMFIX.COM` applies just these 3 bytes to the original or to a translated ENGINE.EXE
  (it checks only the 3 bytes and the file size).

## 13. Code address reference

ENGINE.EXE load-module addresses (add 0x1000 to the segment in Ghidra).

| Address | Function |
|---|---|
| `093A:0300` | `main`: command line, engine init (loads PLBs, system palette, anims, music), room loop |
| `1014:0002` / `00EF` | PLB open / load picture |
| `1082:00C0` | upload palette to the VGA DAC |
| `1147:000E` | RLE Mode-X sprite blitter |
| `10E5:0001` / `0042` | fetch BIOS 8×8 font / draw text |
| `07A2:06F0` / `0086` | load room / room byte-code interpreter |
| `084B:0005` / `0066` / `02F4` | ANIMS.ANI index / load animation / start animation |
| `071D:0076` | load NPC |
| `11A5:0002` | load .ANM |
| `099B:002B` | add mask from MASKS.MSK |
| `058A:10E9` / `0EAB` | load ITEMS.ITM / parse item record |
| `058A:01D1` / `001F` | script VM interpreter / instruction lengths |
| `033F:0114` / `0003` | PROCS.CMD open / load procedure |
| `0A1F:0B9B` | SAY bubble |
| `08AF:07D0` / `0604` / `030A` / `00B0` | DLG load / run / walk nodes / word-wrap and draw |
| `0356:000B` | load FLAGS.FLG |
| `035A:0009` / `00F7` | write / read save game |
| `0B55:000B` / `0197` | music init (.OGD) / play song (.OGM) |
| `11D4:0013` / `11CB:000B` | timer interrupt (tick counter `DGROUP:F02C`, every 2nd INT 8) / its installer |

INTRO.EXE: `065A:0051` intro routine (loads PCX, ACP, ACF), `0735:0095` PowerPacker decoder, `0735:000A` rectangle
blit, `0735:0045` set one palette colour.
