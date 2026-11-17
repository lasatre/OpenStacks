cd /mnt/f/Projects/BluestacksReversing/dev/re_scratch
objdump -dr -M intel --no-show-raw-insn /mnt/f/Projects/BluestacksReversing/qemu_test/initramfs_x/boot/bstmods/bstvmsg.ko > vmsg.dis
objdump -dr -M intel --no-show-raw-insn /mnt/f/Projects/BluestacksReversing/qemu_test/initramfs_x/boot/bstmods/bstinput.ko > inp.dis
readelf -sW /mnt/f/Projects/BluestacksReversing/qemu_test/initramfs_x/boot/bstmods/bstinput.ko > inp.sym
readelf -sW /mnt/f/Projects/BluestacksReversing/qemu_test/initramfs_x/boot/bstmods/bstvmsg.ko > vmsg.sym
readelf -SW /mnt/f/Projects/BluestacksReversing/qemu_test/initramfs_x/boot/bstmods/bstinput.ko > inp.sec
objdump -s -j .rodata -j .rodata.str1.1 -j .rodata.str1.8 -j .data /mnt/f/Projects/BluestacksReversing/qemu_test/initramfs_x/boot/bstmods/bstinput.ko > inp.rodata
