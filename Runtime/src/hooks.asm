; CDHD Runtime - register-transparent vtable slot hooks (x64, MASM).
;
; The game's pack reader (pa::ResourceLoadWorker_Package) is patched so that
; requests for files listed in our manifest are handed to the game's own loose
; file reader (pa::BaseResourceLoadWorker) running on a fake worker object whose
; resource map resolves the path to a file on disk.
;
; These stubs never build a C++ frame for the forwarded call: they only decide
; which function to jump to, so every register and stack argument reaches the
; game's function untouched, whatever its real signature is.

EXTERN g_fakeWorker:QWORD          ; address of our fake BaseResourceLoadWorker
EXTERN g_origPkg:QWORD             ; QWORD[16] original ResourceLoadWorker_Package slots
EXTERN g_origBase:QWORD            ; QWORD[16] original BaseResourceLoadWorker slots
EXTERN CdhdShouldServe:PROC        ; int CdhdShouldServe(void* path, int slot)

.code

; ---------------------------------------------------------------------------
; Path based slots (2 = load, 13 = async load). Path object is in r8.
; ---------------------------------------------------------------------------
PATH_HOOK MACRO name, slot
name PROC
    push rcx
    push rdx
    push r8
    push r9
    sub rsp, 68h                    ; 20h shadow + 40h xmm save + 8 align
    movdqu xmmword ptr [rsp+20h], xmm0
    movdqu xmmword ptr [rsp+30h], xmm1
    movdqu xmmword ptr [rsp+40h], xmm2
    movdqu xmmword ptr [rsp+50h], xmm3
    mov rcx, r8
    mov edx, slot
    call CdhdShouldServe
    movdqu xmm0, xmmword ptr [rsp+20h]
    movdqu xmm1, xmmword ptr [rsp+30h]
    movdqu xmm2, xmmword ptr [rsp+40h]
    movdqu xmm3, xmmword ptr [rsp+50h]
    add rsp, 68h
    pop r9
    pop r8
    pop rdx
    pop rcx
    test eax, eax
    jz @F
    mov rcx, g_fakeWorker
    jmp qword ptr [g_origBase + slot*8]
@@:
    jmp qword ptr [g_origPkg + slot*8]
name ENDP
ENDM

; ---------------------------------------------------------------------------
; Request based slots: the request object (rdx) remembers the worker that
; created it at +20h. Requests created by our fake worker go back to it.
; ---------------------------------------------------------------------------
REQ_HOOK MACRO name, slot
name PROC
    test rdx, rdx
    jz @F
    mov rax, qword ptr [rdx+20h]
    cmp rax, g_fakeWorker
    jne @F
    mov rcx, g_fakeWorker
    jmp qword ptr [g_origBase + slot*8]
@@:
    jmp qword ptr [g_origPkg + slot*8]
name ENDP
ENDM

; Slot 12 receives a pointer to the request in r8.
REQ_HOOK_INDIRECT MACRO name, slot
name PROC
    test r8, r8
    jz @F
    mov rax, qword ptr [r8]
    test rax, rax
    jz @F
    mov rax, qword ptr [rax+20h]
    cmp rax, g_fakeWorker
    jne @F
    mov rcx, g_fakeWorker
    jmp qword ptr [g_origBase + slot*8]
@@:
    jmp qword ptr [g_origPkg + slot*8]
name ENDP
ENDM

PATH_HOOK CdhdHookSlot2, 2
PATH_HOOK CdhdHookSlot13, 13
REQ_HOOK CdhdHookSlot3, 3
REQ_HOOK CdhdHookSlot4, 4
REQ_HOOK CdhdHookSlot5, 5
REQ_HOOK CdhdHookSlot6, 6
REQ_HOOK_INDIRECT CdhdHookSlot12, 12

END
