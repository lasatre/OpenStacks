# HstStream raw notes (objdump of _android_system_vendor_lib64_libOpenglSystemCommon.so)

Vtable _ZTV9HstStream @0x2a348, vptr @0x2a358 (slot N = *(0x2a358+N))

+0x00 0x133e0 idealAllocSize       +0x08 0x13840 allocBuffer   +0x10 0x13860 commitBuffer(->0)
+0x18 0x138d0 readFully(->0)       +0x20 0x138e0 commitBufferAndReadFully(->0)
+0x28 0x138f0 read                 +0x30 0x13870 writeFully      +0x38 0x133f0 write(thunk)
+0x40 0x13630 dtor                 +0x48 0x136a0 dtor
+0x50 0x13850 alloc   -> hstAlloc  0x192d0
+0x58 0x138b0 flush   -> hstFlush  0x18fb0
+0x60 0x13910 readback-> hstRecvCopy 0x19270 -> hstRecv 0x18b70
+0x68 0x13930 lock    -> hstLock   0x18b50
+0x70 0x13940 unlock  -> hstUnlock 0x18b60
+0x78 0x13950 hstSync  +0x80 0x13960 hstEnableSpin

ioctl sites: WRITE 0x40087004 @0x19067, @0x19121 ; READ 0x40087003 @0x18d04, @0x18e05
init: open @0x182d0, ioctl 0x7001 @0x18596, mmap @0x1864c, handshake memcpy @0x188eb

Hst: +0x00 wcur, +0x04 rxValid, +0x08 rxConsumed, +0x10/14/18 rxCache, +0x20 bigLen, +0x24 bigCap,
     +0x28 bigBuf, +0x30 fd, +0x34 connected, +0x38 mutex, +0x70 dword, +0x74 handshake[0x100],
     +0x178 window, +0x180 windowSize, +0x188 hppBase(+0x2000), +0x190 hppSlots, +0x194 hppOn

alloc: ret = window + wcur; wcur += len.  wcur reset to 0 only by flush (after WRITE).
len > windowSize -> flush, ret = bigBuf (heap), bigLen = len; flushed in windowSize chunks from window+0.
flush: WRITE(len=wcur) with data at window+0; also chunks bigBuf. READ: arg[0]=want, reply at window+0.
