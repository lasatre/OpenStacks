# BlueStacks 5 "big slot" + HPP transport - implementation spec

Static RE only. Evidence addresses:
- G64 = guest `libOpenglSystemCommon.so` (vendor/lib64), file offsets = vaddr (guestlibs/_android_system_vendor_lib64_libOpenglSystemCommon.so). The 32-bit build has the same symbol set (hpp*, NegotiatePipeType); not diffed.
- KO = guest `bstpgaipc.ko` (pristine, /root/p49/boot/bstmods/bstpgaipc.ko), `.text` offsets.
- HP = HD-Player.exe (image base 0x140000000). DLL = libOpenglRender.dll.
- OURS = dev/src/hw/misc/bst_pgaipc.c, gr_render.c.

Raw disassembly used: scratchpad `hpp/osc64.dis`, `hpp/ko.dis`.

## 0. TL;DR (why the Unity game dies)

For HPP the guest does NOT negotiate: after sending the handshake it flips itself into HPP mode (`Hst+0x194=1`) and from then on talks only through
shared memory in the (4 MiB) big window. It requires the host to have initialised the window header:
**byte W[0] == 0x55** (lock free). HD-Player does `memset(W,0,0x2000); W[0]=0x55` inside NegotiatePipeType (HP 0x1402ace00, see 2.4).
Our host never wrote it (BAR2 is zero-filled). The first guest lock acquire does `xchg W[0],0xaa`, gets 0x00 (neither 0x55 nor 0xaa) and runs
`PgaLogPrint(6,"%s(%d): Unexpected condition. lock old value 0x%x.") ; exit(1)`
(G64 hppPipeLock-style spin: 0x19c7c `xchg %al,(%rbp)`, 0x19c98 `cmp $0xaa; jne 0x19e7b`, 0x19ea6 `exit(1)`; same copy in hppRecv at 0x1a1f3/0x1a208/0x1abda, hppSync 0x1adfc/0x1ae19/0x1af71).
exit(1) while other threads run static dtors => "pthread_mutex_lock called on a destroyed mutex"; the closed fd produces our observed `cmd=2 disconnect`.
Nothing else is needed to be "negotiated": there is no accept/reject reply to the hpp flag, only the shared-memory protocol below.

Second ordering trap: the WRITE-ack (IRQ) of the handshake must be sent AFTER the window header is initialised (the guest proceeds the moment the
WRITE ioctl returns). Our pure lane acks inside `pvt_recvcopy` (pure_take acks when the chunk is fully consumed), i.e. before any init. See 5.2.

## 1. Big slots

### 1.1 Who picks big vs small
Client (G64 `hstInitClientPgaIpc` 0x18530):
1. `hstNeedHighMem(procname)` (0x183f0; procname is the cmdline string already stored at Hst+0x74): returns TRUE iff `getuid() >= 10000` (a normal app uid) AND the name does not start with one of
   `com.bluestacks`, `com.uncube`, `com.pop.store`, `com.android`, `com.google`, `com.location.provider`, `com.antutu.ABenchMark` (strncmp prefixes).
   So it is not about arm vs x86: EVERY third-party app asks for a big slot (system/uid<10000 processes, surfaceflinger, and the listed prefixes stay small).
2. ioctl(fd, 0x7001, &arg) with `arg = 1` if high-mem else `0` (0x1855f-0x18596).
3. Result written back by the .ko into `arg`: **2 = small slot (window 0x40000), 3 = big slot (window 0x400000)** (0x185d5-0x18617). Any other value keeps Hst+0x180 as is.
4. `mmap(NULL, windowSize, PROT_READ|WRITE, MAP_SHARED, fd, 0)` (0x1864c). window -> Hst+0x178, size -> Hst+0x180.

Kernel (KO `bstpgaipc_connect` 0x7e0):
- arg==0: scan cid 0..0x7e for first `used[cid]==0` (`used` = array at gdev+0x70), mark it used, write arg=2.
- arg!=0: scan cid 0x7f..0x86 (loop 0x8f5, `cmp $0x87`); first free -> mark used, arg=3. If all 8 big slots busy: if `arg==3` fail (-ENOMEM, printk) else fall back to the small scan and arg=2.
  So up to 8 concurrent big clients: cid 0x7f..0x86. cid 127 = first big slot.
- then `file->private = cid`, init waitqueue, `bstpgaipc_hypercall(cid, cmd=1)` (waits for the host IRQ; see 1.3).

### 1.2 BAR / offsets
KO `bstpgaipc_fops_mmap` 0x390:
- cid <= 0x7e: phys = bar1 + ((cid+1)<<18), len 0x40000  (this is the "+1": slot 0 = control page; our PGA_SLOTSHIFT=1).
- cid >= 0x7f: phys = **bar2 + ((cid-0x7f)<<22), len 0x400000** (no +1 shift). `gdev+0x40` = bar2 phys, `gdev+0x28` = bar1 phys (probe 0x129-0x275 reads pci resources 0,1,2 as BAR0/BAR1/BAR2; BAR0 is ioremap'd -> gdev+0x18, BAR1 first 0x1000 ioremap'd -> gdev+0x30 = mailbox/control page).
- Big window size is exactly 0x400000, 8 windows = 32 MiB = our BAR2 size. OK.
- Control page (BAR1 first 4 KiB) is shared by all cids: mailbox of cid at `cid*0x10` (cid up to 0x86 -> 0x860), served-cid register at +0x870. **No per-big-slot control fields exist**; the big slot differs only by window location/size.
- IRQ path (KO 0x310): reads `BAR1[0x870]`, must be <= 0x86, writes 0xffffffff back, mfence, writes the cid to `BAR0+8` (ack), sets `event[cid]=1` (gdev+0x28c+4*cid), wake_up(waitq[cid]). So events are sticky per-cid flags.

### 1.3 ioctls / commands (all slot types; KO `bstpgaipc_fops_ioctl` 0xb40)
| ioctl | KO action | doorbell cmd | waits for IRQ? |
|---|---|---|---|
| 0x7001 | connect (above) | 1 | yes (event[cid]) |
| 0x40087004 | write: mbox[+4]=len from user, hypercall 4, copy mbox[+4] back; ret<=0 => -EINVAL | 4 | yes |
| 0x40087003 | read: same shape | 3 | yes |
| 0x7006 | **kick**: hypercall(cid,6) | 6 | **no** (hypercall returns right after doorbell if cmd==6) |
| 0x7005 | **wait_host**: sleep until event[cid]!=0, then clear it (0x6b0). No device access. | - | local |
| 0x7007 | **reset**: event[cid]=0 (0x7a0). No device access | - | local |
| 0x7002 | -EINVAL | - | - |
| release | hypercall(cid,2) (does not wait); clears used[cid] | 2 | no |

`bstpgaipc_hypercall` (0x4b0): requires used[cid]; for cmd != 6 clears event[cid]; writes `mbox[cid].cmd`, mfence, writes cid to BAR0+0; then reads `mbox[cid].status (+8)`: != 0 => printk + -EINVAL; then waits for event[cid] only if cmd not in {2,6}.
(cmd 7 is never issued by this .ko.)

### 1.4 Gaps in OURS vs this
- Window/BAR geometry: `pga_window()` already maps cid>=0x7f -> BAR2+(cid-0x7f)*4MiB and size 4 MiB. Correct. (Small slots need PGA_SLOTSHIFT=1; big ones intentionally don't.)
- **cmd 6 (kick) is wrong**: `pga_doorbell` -> `gr_render_op_request` -> queued behind all other ops on the single `gr_worker` (which Sleep(2)s per op) -> worker does `mbox_set(0,0)` + `GR_DONE` (spurious IRQ!). Must be handled inline, no IRQ (see 5.1). Spurious IRQs are actively harmful in HPP (guest checks W[2] right after a 0x7005 wakeup, see 3.6).
- No HPP ring implementation at all (pure lane only knows WRITE/READ chunks).
- Handshake ack ordering (above).

## 2. Client init sequence with HPP

Which library: the x86_64 `libOpenglSystemCommon.so` (G64), also for translated arm64 apps. The arm64 dirs (/android/system/lib64/arm64: libEGL.so, libGLESv2.so, libhwui ...) contain no HST code (0 hits for hst/pga/`tid %d`); GL is reached through the native bridge into the x86 stack. The "%d %d %d" string is built in two steps, there is no single format with 3 ints in the guest:
- `hstInitClient` (0x186f0): `snprintf(Hst+0x74+len, " (pid %d, tid %d) %d %d", getpid(), tid, isImageDetectionEnabled(name), isAngleDisabled(name))` -> "name (pid P, tid T) a b".
- `NegotiatePipeType` (0x19530) appends `" %d"` = hpp flag (format string at 0x1d98e) -> "... a b 1".
- Whether the flag is 1: `(Hst+0x180 == 0x400000) && _hppNeedEnabled(name)` (0x19555-0x1955f). `_hppNeedEnabled` (0x193d0) cuts the name at the first space and calls `android::BstFilterAppsManager::isHppEnabled(String16)` which is in `libbinder.so` (0x5b620): a binder call to a BlueStacks system service (vtable +0x220 of the service proxy); **if the service is unreachable it returns true**. The per-app list is host/Java-side (not examined). Therefore HPP only exists in big slots; we cannot refuse it from the host - the guest switches itself on.

Step by step (hstInitClient 0x186f0 -> hstInitClientPgaIpc 0x18530):
1. `open("/dev/bstpgaipc", O_RDWR)` (0x18881; string 0x1d663).
2. ioctl 0x7001 arg=1 (high mem) -> kernel returns arg=3 (big) [cid 0x7f..0x86].  Host sees doorbell cid=N cmd=1 (connect), completes with IRQ.
3. mmap 0x400000 of the slot (BAR2 + (cid-0x7f)*4MiB). Hst+0x178=W, +0x180=0x400000.
4. NegotiatePipeType (0x19530): hpp=1 -> handshake string built in Hst+0x74 (256 B zero padded; `hstWriteAllDirect` 0x18aa0 memcpy to W[0..0x100) then `hstFlush` -> normal ioctl 0x40087004 (cmd 4) len=0x100) [hppOn still 0 here so this is the classic path].
5. After that WRITE returns (host ack IRQ): `ioctl(fd,0x7007)` (clear event), then
   `Hst+0x188 = W+0x2000` (hppBase), `Hst+0x190 = (0x400000-0x2000)>>12 = 0x3FE = 1022` slots, `Hst+0x194 = 1` (hppOn) (0x195be-0x195f4).
   `hstInitClient` skips its own handshake send when hppOn (0x188bf).
6. From now on `hstAlloc/hstFlush/hstRecv/hstSync/hstEnableSpin` dispatch to `hppAlloc/hppFlush/hppRecv/hppSync/hppEnableSpin` (checks of `cmpb $0,0x194` at 0x192da, 0x18fcf, 0x18b9a, 0x193a0, 0x193c0). No more cmd 3/4 doorbells on this cid; the only doorbell is cmd 6 (ioctl 0x7006) plus cmd 2 at close. All synchronisation is by the W header + IRQ (wake via ioctl 0x7005).
7. What the guest first checks: the first W lock (`xchg W[0],0xaa`) must return 0x55 (free) or 0xaa (held -> spin up to 100000 pause iterations then sched_yield, as long as `Hst+0x30` fd>0). Any other old value => log + `exit(1)`. => host must have W[0]=0x55 before the handshake WRITE is acked.

### 2.4 Host counterpart (HP)
`createFinish` vt+0x50 = HP 0x1402aa6e0: `hstPeekTimed(sock,8)`; `hstRecvCopy(sock, sock+0x30, 0x100)`; `NegotiatePipeType` (HP 0x1402ace00); `if (sock[0x14c]) raiseInterrupt(sock)` (0x1402aa750).
NegotiatePipeType: `sscanf(sock+0x30, "%s (pid %d, tid %d) %d %d %d") == 6`, with the 6th int != 0:
- `sock+0x0c = 0x80000` (rx buffer cap), `sock+0x18 = malloc(0x80000)` (rx buffer),
- `sock+0x140 = sock+0x130 + 0x2000` (hppBase), `sock+0x148 = (winSize-0x2000)>>12` (slot count; winSize = `sock+0x138`),
- `sock+0x14c = 1` (hpp), `memset(window, 0, 0x2000)`, **`window[0] = 0x55`**,
- sock+0x14d (spin hint) stays 0; set to 1 later by vt+0x68 (HP 0x1402aa5e0 = `if(sock[0x14c]) sock[0x14d]=1`), a performance hint only.
The trailing `raiseInterrupt` = the ACK of the handshake WRITE (exe acks a WRITE by raising the cid IRQ when it needs more data / is done; here done after init). Note ordering: init first, ack last.

## 3. HPP data protocol (shared-memory ring in the 4 MiB window)

### 3.1 Layout (W = window base)
```
W+0x0000  u8  lock        0x55 = free, 0xaa = held. Acquire: old=xchg(W[0],0xaa) must give 0x55 (spin on 0xaa; anything else = fatal). Release: xchg(W[0],0x55).
W+0x0001  u8  guestState  (written by guest; host reads/clears under lock)
                           0 idle / done      1 active (spinning for reply)
                           2 asleep waiting for ring space / hppSync completion (waits via ioctl 0x7005)
                           3 asleep waiting for a reply or the next reply chunk (waits via 0x7005)
W+0x0002  u8  hostState   (written by host, guest clears 2->0 and kicks)
                           0 host awake/polling   1 host is writing a reply
                           2 host idle/asleep: it will not look at the ring until kicked (0x7006)
                           3 reply chunk posted, MORE chunks follow (guest must kick after consuming)
W+0x0003  u8  replyReady  host sets 1 together with final hostState; guest clears 0 after it has consumed the whole reply
W+0x0004  u32 head        consumer index of the guest->host request ring (host advances)
W+0x0008  u32 tail        guest->host producer index (guest advances); also reused as slot count of a host->guest reply (3.4)
W+0x000c  u32 slotLen[N]  per-slot payload length, indexed by (index mod N); N = 1022; array ends at +0x1004
W+0x2000  slot i data at W+0x2000 + i*0x1000, i in [0,N)   (hppBase = W+0x2000, 4 KiB each)
```
Indices are free-running u32, slot = index % N. Ring full = `(tail+1) % N == head % N` (guest keeps one slot free, hppAlloc 0x198f6).
The first 0x2000 bytes of W are the header; the handshake lived in W[0..0x100) and is destroyed by the host memset (host copies it out first).

### 3.2 Guest -> host (request stream), guest code hppAlloc 0x196e0 / hppFlush 0x19ef0
- `hppAlloc(len)`: bump allocates inside the current slot (Hst+0x60 = bytes used in current slot, Hst+0x64 = oversize alloc length). If cur+len > 0x1000 the slot is closed early: `slotLen[idx%N] = cur` (could be < 0x1000), `tail = idx+1`, next slot. A slot filled to 0x1000 gets `slotLen=0x1000`, `tail++` (0x19766-0x19771). len > 0x1000 allocs are staged in a heap buffer (Hst+0x28) and copied slot by slot in hppFlush (0x19b10).
- Order of stores: payload bytes -> `slotLen[]` -> `tail`. (TSO; host must read tail first, then slotLen and data.)
- `hppFlush`: closes the partial slot (`slotLen = Hst+0x60`, `tail++`, 0x1a025-0x1a068). Then, if `head != tail`: `if (W[2]==2) { W[2]=0; ioctl(fd,0x7006) }` (kick) (0x1a076-0x1a121). If W[2] is 0/1/3 no kick.
- While producing, every ~8 slots / 0x200 allocs the guest also does the same `W[2]==2 -> W[2]=0 + kick` check (hppAlloc 0x19836-0x198e1) to keep the host draining.
- Ring full: `_hppWaitForHostRead` (0x19c60): lock; read p=W[2]; `W[1]=2`; unlock; if p==2 {W[2]=0; kick}; p==3 => fatal; then `ioctl(0x7005)` (sleep until host IRQ); lock; `W[1]=0`; unlock.
- The byte stream the host receives = concatenation of `slotLen[i]` bytes of slot i, for i=head..tail-1.

### 3.3 Guest waiting for a reply: hppRecv 0x1a180 (hstRecv calls hstFlush first)
```
if cached reply bytes >= want: serve from guest-side cache (Hst+0x4 valid, +0x8 consumed, +0x18 buf)  -- no ring access
lock; W[1]=1; unlock
if W[3]==0:
   spin up to 60000 iterations (0x1a333): { if W[2]==2 {W[2]=0; kick 0x7006}   // host asleep with request pending
                                            pause; if W[3]!=0 break }
   if still W[3]==0:   // sleep path (0x1a410)
        lock; p=W[2]; if p!=3 {W[1]=3}; unlock
        if p==2 {W[2]=0; kick}
        if p!=3 { ioctl(fd,0x7005) }          // wait for host IRQ
        lock; W[1]=1; unlock; re-check W[3]
reply processing (0x1a625-):
   loop: s=W[2]
     s==0 or s==2 : FINAL chunk (0x1a923)
     s==3         : CONTINUATION chunk (0x1a66f)
     other (incl. 1) : fatal "Invalid case! host status"  (exit(1))
```
Reply data location: **contiguous from W+0x2000 (slot 0), not ring-indexed**. n = tail%N (0 -> N); chunk length = `(n-1)*0x1000 + slotLen[n-1]` using `tail = W[8]` and `slotLen[n-1]`. (slotLen[n-1] must be <= 0x1000.)
- Final chunk: if guest cache empty and length >= want, return pointer W+0x2000 directly (zero copy) else memcpy to its heap buffer. Then `W[4]=0; W[8]=0; W[3]=0` (0x1aa33-0x1aa4b, 0x1a295), `lock; W[1]=0; unlock`.
- Continuation chunk: memcpy to heap buffer; `lock; W[1]=3; unlock; kick (0x7006); ioctl(0x7005)`; then lock; W[1]=1; unlock; loop again (reads W[2] again - must not be 1 by then).
- `hppSync` (0x1ad40, called via hstSync on swap/finish-type sync): hppFlush; then loop { hs=W[2]; if hs==0: lock; if W[2]==0 {W[1]=2}; unlock; ioctl(0x7005); lock; W[1]=0; unlock; continue;  if hs==2 and head!=tail: W[2]=0; kick; continue;  hs==2 and head==tail: done;  else done }. I.e. it waits until the host is idle (W[2]==2) with the ring empty. The host must therefore raise an IRQ when it goes idle if W[1]==2 (3.5).

### 3.4 Host -> guest (reply stream) - HD-Player `FUN_1402ad0b0` (hppFlush, called from hstSend 0x1402aabc0 and at the start of every peek)
Reply bytes accumulate in the sock send buffer (+0x28 buf, +0x20 len; hstAlloc appends). Flush:
```
if len==0 return
W[4]=0; W[8]=0                                  // reset request ring indices (guest has nothing pending)
cap = N*0x1000
while remaining>0:
    lock; W[2]=1; unlock
    if remaining > cap: copy cap bytes to W+0x2000; slotLen[N-1]=0x1000 (all earlier slots are implicitly full); count=N; final=3
    else: copy remaining to W+0x2000; count=ceil(rem/4096); slotLen[count-1] = rem&0xfff ? rem&0xfff : 0x1000; final=0
    W[8]=count
    lock; W[2]=final; c=W[1]; W[3]=1; unlock
    if c==3: W[1]=1; raised=0; raiseInterrupt(cid)        // wake sleeping guest
    elif c==2: fatal (guest waits for ring space while host posts a reply)
    if final==3: wait for kick (guest kicks after consuming chunk)   // acc90(-1)
(rest of the data continues from W+0x2000 again)
then wait until the guest consumed everything:
   while W[3]!=0 && !disconnected:
       [spin hint 0x14d: poll up to ~19us checking W[1]==3 -> W[1]=1, raise]
       else: lock; c=W[1]; if c in{1,3} W[2]=2; unlock; if c==3 {W[1]=1; raise}; if c in{1,3} wait for kick; lock; W[2]=0; unlock
sock.len = 0
```
(The `c==2` fatal case and the "guest must not see W[2]==1 after a wake" rule imply: never raise spuriously.)

### 3.5 Host receive (request) side - HD-Player `hstPeekTimed` 0x1402abde0 -> HPP variant `FUN_1402ad5a0`
State: rx buffer `sock+0x18` (cap +0xc), `+4` = rxValid, `+8` = rxConsumed (advanced by hstRecv/RecvCopy), `+0x204` = disconnect flag, `+0x200` = "interrupt already raised" latch, `+0x14d` spin hint.
```
peek(need):
   hppFlush()  (3.4)                                           // pending reply first
   if rxValid>0 && rxValid==rxConsumed: rxValid=rxConsumed=0
   while rxValid-rxConsumed < need && !disc:
       head=W[4]; tail=W[8]
       if head==tail:                                           // ring empty
            (spin hint path: for ~30us poll; if W[1]==2 {W[1]=0; raised=0; raise} )
            lock                                                // xchg W[0],0xaa
            c=W[1]; if c!=3: W[2]=2                             // announce "asleep, kick me"
            unlock
            if c==2: W[1]=0; raised=0; raise IRQ                // guest waited for ring space / sync: wake it
            if c!=3: wait for kick event (cond var; kick = doorbell cmd 6 OR cmd 3/4)  ; clears the S_RUN flag; raised=0
            (c==3 => guest waits for our reply, do NOT sleep: loop/poll)
            FUN_1402ad930 (lock); W[2]=0; unlock
       else:
            repeat: slot=head%N; len=slotLen[slot]; append W+0x2000+slot*0x1000 [len bytes] to rx (grow rx buffer in 512 KiB steps, realloc);
                    head++; W[4]=head;
                    until (rxValid>0x20000 && rxValid-rxConsumed>=need) || head==W[8]
   *out = rx + rxConsumed ; sock[0x14d]=0
```
Exact fact: the host does **not** raise an IRQ per consumed slot; it raises (if W[1]==2) only when it finds the ring EMPTY. Guest producers blocked on ring-full therefore wake when the host has drained everything.
Known race in the original (we can fix): head==tail is sampled before taking the lock; a request published between that and `W[2]=2` with the guest seeing W[2]==0 produces no kick -> lost wakeup. Recommended fix: after setting W[2]=2 under lock, re-read tail; if head!=tail, set W[2]=0 and continue without sleeping.

### 3.6 Kick, IRQ and the rules that must hold
- **Kick** (guest ioctl 0x7006 -> doorbell cmd 6 on the cid): host side HP 0x1402ab270: for cmd in {3,4,6} `sock.len = mbox[1]; sock.data = &mbox[1]; lock; sock[0x1e8]=1 (S_RUN); cond_signal(sock+0x1a0); unlock; mbox[2]=0` and nothing else - no IRQ, no mbox result. The same event wakes `FUN_1402acc90` (wait; clears S_RUN, `raised=0`).
- **IRQ to guest** = `raiseInterrupt` HP 0x1402aa750: if `!sock[0x200]`: take the global IRQ mutex, wait until the previous served-cid IRQ has been acked (guest BAR0+8 write), write cid to `ctrl[0x870]`, assert INTx, set `sock[0x200]=1`. Our pga_complete/cq queue already models this.
- Guest handling: the .ko sets sticky `event[cid]=1`; `ioctl 0x7005` returns at once if set and clears it. After a wake the guest re-reads W; the only fatal re-read is hostState==1 during reply-continuation (hppRecv 0x1a649). So: **raise an IRQ only when `W[1]` (under lock) is 2 or 3 as in 3.4/3.5; never an extra "completion" IRQ for cmd 6.**
- Lock discipline: every field handshake (W[1], W[2], W[3] updates) is under the W[0] byte lock (host: xchg 0xaa spin, up to 100000 spins then Sleep(0); old value other than 0x55/0xaa => fatal in exe). Guest data (slot payload/slotLen/head/tail) is lock-free.

### 3.7 Disconnect
cmd 2 (guest close): HP sets `sock+0x204=1`, signals S_RUN, `if(sock[0x14c]) FUN_1402ad540()` (HPP teardown: frees rx), clears registry entry. All host loops above check `+0x204` and return 0x68.

## 4. Renderer integration (DLL)
The DLL is oblivious to HPP. `RenderThread::main` (DLL 0x18012b2f0; 0x18012b64c is inside it) uses only the HstSocketHost vtable: `+0x50 createFinish` once, then per message `+0x40` (free/stats hook), `+0x08 peek(8,&out)` (header: u32 opcode, u32 len), `+0x00 recv(len,&out)`; replies via `+0x30 alloc`/`+0x18 alloccopy`, flushed by `+0x28 send` or implicitly by the next peek. Same slots as the pure lane already implements (OURS g_vt_pure[0..6,10]). HPP lives entirely inside the exe's peek/flush implementation (FUN_1402abde0 -> FUN_1402ad5a0 for peek, FUN_1402aabc0 -> FUN_1402ad0b0 for send; `sock[0x14c]` selects). No HPP-specific slot is required by the DLL; `+0x68` (HP 0x1402aa5e0) only sets the spin hint `sock[0x14d]=1` (skip or keep as no-op). The renderControl stream is identical byte-for-byte to the non-HPP stream (the DLL's record parser eats the concatenated slot payloads; nothing HPP-specific in the framing).

## 5. Implementation plan (OURS)

### 5.1 bst_pgaipc.c
- `pga_doorbell`: handle `cmd==PGAIPC_CMD_HYPERCALL` first, before the gr switch: `stl_le_p(mb+8,0); gr_render_kick(cid); return;` (no `pga_complete`, no queue, no ret write). Keep cmd 7 untouched (never issued).
- Everything else unchanged for big slots (window math already right). Optionally log connect `cid>=0x7f` once.
- Threading: the doorbell runs on the vCPU/iothread under BQL; `gr_render_kick` must only do `mutex_lock; flag=1; cond_signal; unlock` (non-blocking). HPP is *not* polled by the vCPU; the render thread (DLL RenderThread, one per connection) does the waiting, so a blocking wait is fine there. IRQs are raised through the existing `GR_DONE -> qemu_bh_schedule(gr_bh[cid]) -> pga_complete` path (BH coalescing per cid is acceptable, event flag is sticky).

### 5.2 gr_render.c
New per-sock state (GrSock): `bool hpp; uint8_t *hRx; uint32_t hRxCap,hRxValid,hRxCons; QemuMutex/QemuCond or SRWLOCK+CONDITION_VARIABLE kick; bool kickFlag; uint32_t nslots; uint8_t *hBase;`.
The exe sock already has `S_RUN(+0x1e8)`, `S_CNDP(+0x1a0)`, `S_MTXP(+0x150)`, `S_RAISED(+0x200)`, `S_DISC(+0x204)`; reuse them: kick = `S_LOCK; S_RUN(s)=1; WakeConditionVariable(S_CNDP); S_UNLOCK`.
1. `void gr_render_kick(uint32_t cid)`: exported; sets S_RUN + wake as above (also used by cmd 3/4 in the exe, but the pure lane keeps its own mechanism for those).
2. `pvt_createfinish`: do not use `pvt_recvcopy` for the name. Take 0x100 bytes with `pure_take(s,0x100,false /*no advance, no ack*/)`, copy to `raw+0x30`, parse with sscanf (6 fields). If `a6 != 0 && GR_WSZ(cid) == 0x400000`:
   - `hpp_init(s)`: `W=GR_WIN(cid)`; `memset(W,0,0x2000); W[0]=0x55;` `hBase=W+0x2000; nslots=(0x400000-0x2000)>>12 (=1022); hRx=malloc(0x80000); cap=0x80000; valid=cons=0; s->hpp=1`. (memset after copying the name out.) Use a release fence.
   - then do the deferred handshake ack: `S_RPOS=S_WCNT; pure_ack_write_locked(s)` (this does `mbox_set(len)` + `GR_DONE`). **The ack must come after hpp_init.** Do not send a second IRQ.
   Else (hpp==0) keep current behaviour (consume + ack).
3. Make the pure vtable entries branch on `s->hpp`:
   - `pvt_peek4/peek3/recv/recvcopy` -> `hpp_peek(s, need, &p, advance)` implementing 3.5 with the rx buffer; `advance` bumps `hRxCons`; on `hRxValid==hRxCons` reset both to 0 (as exe). Return 0x68 on `S_DISC`.
   - `pvt_alloc/alloccopy`: unchanged (staging in pRbuf, `pWpos`).
   - `pvt_send` and the start of every peek: `hpp_flush(s)` implementing 3.4 using `pRbuf/pWpos` as the send buffer; at the end `pWpos=0`.
   - `pure_flush` currently acks/waits for a guest READ; it must not run for hpp socks.
4. Window accessors (all with the W lock = byte `W[0]`): `static inline bool wlock(s)`: `for(i=0;;i++){ old=__atomic_exchange_n(&W[0],0xaa,__ATOMIC_ACQ_REL); if(old==0x55) return true; if(old!=0xaa) fatal-log; if(S_DISC) return false; if(i>100000) Sleep(0); }`; `wunlock: __atomic_store_n(&W[0],0x55,__ATOMIC_RELEASE)` (guest releases with xchg; plain store with release is equivalent). Read head/tail/slotLen with `__atomic_load_n(...,ACQUIRE)`; publish `W[8]` / `W[3]` after the data with RELEASE.
5. `hpp_wait_kick(s)`: `S_LOCK; while(!S_RUN(s) && !S_DISC(s)) SleepConditionVariableSRW(S_CNDP,S_MTXP,timeout=2..5 ms (belt and braces against lost kicks); S_RUN=0; S_RAISED=0; S_UNLOCK`. A timeout wake that finds nothing simply loops (cheap), which also makes the lost-wakeup race in 3.5 harmless.
6. `hpp_raise(s)`: `if (!S_RAISED(s)) { S_RAISED=1; GR_DONE(s->cid); }` (clear S_RAISED in hpp_wait_kick and wherever 3.5/3.4 say `raised=0`). Raise ONLY in the cases of 3.4/3.5 (W[1]==3 after posting a reply; W[1]==2 when the ring is found empty).
7. `OP_DISCONNECT`: besides `S_DISC=1`, call `gr_render_kick(cid)` so a thread parked in hpp_wait_kick/posting loops exits with 0x68; free hRx on session end; clear `s->hpp`. `sock_prepare` (reconnect reuse) must reset `hpp=0`, hRx indices and `pWpos`.
8. Never touch `mbox[]` for an hpp sock after the handshake ack, except cmd 2.
9. The single `gr_worker` thread: connect (cmd 1) and the handshake WRITE (cmd 4, `pure_write`) are still serialised on it (they only set state/ack and return, fine). All HPP waiting happens on the DLL RenderThread, not on gr_worker. Make sure `Sleep(2)` after each op (gr_worker) cannot delay the *handshake ack*: ack is performed from the RenderThread in createFinish, OK.
10. Logging to add: `hpp_init cid`, first N peeks/flushes (len, head/tail), kick count, raise count; and a watchdog log if a render thread waits >3 s with `W[1],W[2],W[3],head,tail` dumped (invaluable for the first bring-up).

### 5.3 Expected first-boot behaviour / checks
- After the fix the guest log (`PgaLogPrint`, /data/local/tmp/pga_gl.log if tracing on) should show `New SOCKET connection:` followed by `hppAlloc ... allote len`/`kick host` instead of `Unexpected condition. lock old value 0x0`.
- Sanity values: `slotLen <= 0x1000`, reply chunk len = (n-1)*4096+slotLen[n-1]; never leave W[2]==1 while W[3]!=0.
- Not covered / unverified: behaviour of `FUN_1402ad540` (teardown), exact contents of vt+0x40 (HP 0x1402aa610), `hppEnableSpin` effect (guest `Hst+0x195` spin enable only; harmless), the 32-bit lib (same layout assumed), the Java-side HPP app list (isHppEnabled returns true if the binder service is absent).
