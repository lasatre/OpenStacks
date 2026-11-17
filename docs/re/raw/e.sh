cd /mnt/f/Projects/BluestacksReversing/dev/re_scratch
K=/mnt/f/Projects/BluestacksReversing/qemu_test/initramfs_x/boot/bstmods/bstaudio.ko
readelf -rW $K | awk '/rela.data/,0' | head -60 | grep -v "^$" > reladata.txt
readelf -rW $K | grep -E "fini_cmd|init_cmd|config_cmd|AUD_CMD|data_available" > callrefs.txt
objdump -s -j .data $K | head -30 > data.txt
