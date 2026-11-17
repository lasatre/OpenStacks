cd /mnt/f/Projects/BluestacksReversing/dev/re_scratch
B=/mnt/f/Projects/BluestacksReversing/qemu_test/initramfs_x/boot/bstmods
readelf -rW $B/bstinput.ko | grep -A12 "rela.rodata'"
objdump -s -j .rodata $B/bstinput.ko | head
objdump -s -j .rodata.str1.1 $B/bstinput.ko
objdump -s -j .rodata.str1.8 $B/bstinput.ko
