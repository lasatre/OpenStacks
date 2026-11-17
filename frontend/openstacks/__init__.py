"""OpenStacks - a QEMU-based, BlueStacks-5-style Android emulator front-end.

Package layout:
    openstacks.config       instance store (instances.conf) + global settings
    openstacks.qemu_engine  QEMU argv builder + process lifecycle (QemuInstance)
    openstacks.adb          thin adb.exe wrapper (connect / install / input)
    openstacks.vnc          pure-python RFB (VNC) client + Qt thread wrapper
    openstacks.display      VncDisplay widget + InputBridge / InputWorker
    openstacks.player       player window shell (tabs + shared sidebar toolbar)
    openstacks.manager      instance-manager QMainWindow

Deliberately NO Qt imports here, so CLI tools (screenshots, adb, engine
helper scripts) can import openstacks.* pieces without a display.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
