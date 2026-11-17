cd /mnt/f/Projects/BluestacksReversing/dev/re_scratch
K=/mnt/f/Projects/BluestacksReversing/qemu_test/initramfs_x/boot/bstmods/bstaudio.ko
objdump -dr -M intel --no-show-raw-insn $K > dis.txt
readelf -sW $K > syms.txt
readelf -SW $K > secs.txt
objdump -s -j .rodata -j .rodata.str1.1 -j .rodata.str1.8 $K > ro.txt
wc -l dis.txt
