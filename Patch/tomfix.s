# TOMFIX.COM - engine fixes for "Tom Long: The Time Adventure" (1994), ENGINE.EXE
#
# A tiny DOS .COM program. Run it in the game folder (DOSBox works). It
#   1. opens ENGINE.EXE and checks its size (88902 bytes),
#   2. reads the 3 bytes it is going to change: if they hold the original values it goes on,
#      if they already hold the new values it says "already patched", otherwise it stops,
#   3. copies ENGINE.EXE to ENGINE.BAK (only if no ENGINE.BAK exists yet),
#   4. writes the 3 new bytes.
# Only the size and those 3 bytes are checked, so it works on the original German ENGINE.EXE
# and on a translated one (texts edited in place).
#
# The 3 bytes:
#   0x0616B  7F -> 3B   jump at 0468:0CEA: Esc in the Load menu now restores the previous verb
#   0x06241  A9 -> 65   jump at 0468:0DC0: Esc in the Save menu now restores the previous verb
#   0x12F9A  00 -> 01   initial value of DGROUP:032A: Save button available from the start
#
# Build (GNU binutils, e.g. on Linux or with MinGW/Cygwin on Windows):
#   as --32 -o tomfix.o tomfix.s
#   ld -m elf_i386 -Ttext 0x100 --oformat binary -e _start -o TOMFIX.COM tomfix.o
# The result is byte-for-byte identical to the distributed TOMFIX.COM (839 bytes).
#
# The code part is the generic patch applier also used by make_patch.py; everything the
# patch does is described by the table at `pdata` at the end of this file.

        .intel_syntax noprefix
        .code16
        .text
        .globl _start
_start:                         # a .COM program is loaded at CS:0100h, CS=DS=ES=SS
    lea dx, [pdata + 10]
    call print
    lea dx, [fname]
    mov ax, 0x3d02              # open ENGINE.EXE read/write
    int 0x21
    jnc opened
    lea dx, [m_nofile]
    jmp fail
opened:
    mov [handle], ax
    mov bx, ax
    mov ax, 0x4202              # size = seek to end
    xor cx, cx
    xor dx, dx
    int 0x21
    cmp ax, [pdata]
    jne wrongver
    cmp dx, [pdata + 2]
    jne wrongver
    mov ax, [pdata + 8]
    add ax, offset pdata
    mov [buf], ax

    # ---- pass 1: does the file hold the original bytes, or the patched ones?
    mov byte ptr [isold], 1
    mov byte ptr [isnew], 1
    call first_rec
check_loop:
    call seek_rec
    mov cx, [si + 4]
    mov dx, [buf]
    mov bx, [handle]
    mov ah, 0x3f
    int 0x21
    jc ioerr
    cmp ax, [si + 4]
    jne wrongver
    mov cx, [si + 4]
    push si
    add si, 6
    mov di, [buf]
    repe cmpsb
    je same_old
    mov byte ptr [isold], 0
same_old:
    pop si
    push si
    mov cx, [si + 4]
    add si, 6
    add si, cx                  # -> new bytes
    mov di, [buf]
    repe cmpsb
    je same_new
    mov byte ptr [isnew], 0
same_new:
    pop si
    call next_rec
    dec word ptr [count]
    jnz check_loop

    cmp byte ptr [isold], 1
    je do_patch
    cmp byte ptr [isnew], 1
    jne wrongver
    lea dx, [m_already]
    jmp done_msg

do_patch:
    # ---- backup ENGINE.BAK (only if none exists yet)
    lea dx, [bakname]
    xor cx, cx
    mov ah, 0x5b                # create new file, fails if it exists
    int 0x21
    jc no_backup
    mov [bakh], ax
    mov bx, [handle]
    mov ax, 0x4200
    xor cx, cx
    xor dx, dx
    int 0x21
copy_loop:
    mov bx, [handle]
    mov cx, 0x4000
    mov dx, [buf]
    mov ah, 0x3f
    int 0x21
    jc ioerr
    or ax, ax
    jz copy_done
    mov cx, ax
    mov bx, [bakh]
    mov dx, [buf]
    mov ah, 0x40
    int 0x21
    jc ioerr
    cmp ax, cx
    jne ioerr
    jmp copy_loop
copy_done:
    mov bx, [bakh]
    mov ah, 0x3e
    int 0x21
    lea dx, [m_backup]
    call print
no_backup:

    # ---- pass 2: write the new bytes
    call first_rec
write_loop:
    call seek_rec
    mov cx, [si + 4]
    mov dx, si
    add dx, 6
    add dx, cx                  # -> new bytes
    mov bx, [handle]
    mov ah, 0x40
    int 0x21
    jc ioerr
    cmp ax, [si + 4]
    jne ioerr
    call next_rec
    dec word ptr [count]
    jnz write_loop
    lea dx, [m_done]
done_msg:
    call print
    mov bx, [handle]
    mov ah, 0x3e
    int 0x21
    mov ax, 0x4c00
    int 0x21

wrongver:
    lea dx, [m_wrong]
    jmp fail
ioerr:
    lea dx, [m_io]
fail:
    call print
    mov ax, 0x4c01
    int 0x21

# si = first record, [count] = number of records
first_rec:
    mov si, [pdata + 6]
    add si, offset pdata
    mov ax, [pdata + 4]
    mov [count], ax
    ret
# si += 6 + 2*length
next_rec:
    mov ax, [si + 4]
    shl ax, 1
    add ax, 6
    add si, ax
    ret
# seek ENGINE.EXE to the record's file offset
seek_rec:
    mov bx, [handle]
    mov dx, [si]
    mov cx, [si + 2]
    mov ax, 0x4200
    int 0x21
    jc ioerr
    ret
print:
    mov ah, 9
    int 0x21
    ret

fname:     .ascii "ENGINE.EXE"
           .byte 0
bakname:   .ascii "ENGINE.BAK"
           .byte 0
m_nofile:  .ascii "ENGINE.EXE not found. Run this in the game folder."
           .byte 13, 10
           .ascii "$"
m_wrong:   .ascii "This ENGINE.EXE is a version this patch does not know. Nothing was changed."
           .byte 13, 10
           .ascii "$"
m_io:      .ascii "Disk error. The patch may be incomplete - restore ENGINE.BAK."
           .byte 13, 10
           .ascii "$"
m_already: .ascii "ENGINE.EXE is already patched."
           .byte 13, 10
           .ascii "$"
m_backup:  .ascii "Original saved as ENGINE.BAK."
           .byte 13, 10
           .ascii "$"
m_done:    .ascii "ENGINE.EXE patched successfully."
           .byte 13, 10
           .ascii "$"
handle:    .word 0
bakh:      .word 0
buf:       .word 0
count:     .word 0
isold:     .byte 0
isnew:     .byte 0

# ─────────────────────────────────────────────────────────────────────────────
# Patch description. Layout (offsets from pdata):
#   +0  dword  required size of ENGINE.EXE
#   +4  word   number of records
#   +6  word   offset of the first record
#   +8  word   offset of the free memory used as copy/compare buffer (16 KB)
#   +10 title line, ending in '$' (DOS print-string convention)
#   records: dword file offset, word length, old bytes[length], new bytes[length]
# ─────────────────────────────────────────────────────────────────────────────
pdata:
        .long 88902                     # size of ENGINE.EXE
        .word 3                         # 3 records
        .word records - pdata
        .word buffer - pdata
        .ascii "Tom Long - engine fixes: Esc in the Load/Save menus, Save button from the start"
        .byte 13, 10, 13, 10
        .ascii "$"
records:
        .long 0x616B                    # Esc in the Load menu (jump E9 7F 01 -> E9 3B 01)
        .word 1
        .byte 0x7F                      # old
        .byte 0x3B                      # new
        .long 0x6241                    # Esc in the Save menu (jump E9 A9 00 -> E9 65 00)
        .word 1
        .byte 0xA9
        .byte 0x65
        .long 0x12F9A                   # Save button unlocked at the start (DGROUP:032A = 1)
        .word 1
        .byte 0x00
        .byte 0x01
buffer:                                 # free memory after the program
