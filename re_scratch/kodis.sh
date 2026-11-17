K=${KO:-/mnt/f/Projects/BluestacksReversing/qemu_test/initramfs_x/boot/bstmods/bstpgaipc.ko}
for f in "$@"; do objdump -dr -M intel --no-show-raw-insn "$K" | awk -v f="<$f>:" '$0 ~ f {p=1} p && /^$/ {exit} p'; done
