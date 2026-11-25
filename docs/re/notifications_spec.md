# Android notifications to the desktop

BlueStacks shows the guest's notifications as desktop notifications. The guest side is
built into the images, so OpenStacks only has to receive them.

## Guest side (Android 11 `services.jar`; the Pie image sends the same)

`NotificationManagerService$PostNotificationRunnable.run` calls
`NotificationManagerService.sendNotificationToHost(pkg, Notification, NotificationRecord)`
for every posted notification that has a small icon. No setting gates the call. The method
skips:

- group summaries (`Notification.isGroupSummary()`);
- progress notifications (`android.progress` or `android.progressMax` non-zero);
- notifications with a custom `contentView`;
- updates (`NotificationRecord.isUpdate`) of importance <= 2 (low);
- system apps (`ApplicationInfo.flags & (FLAG_SYSTEM | FLAG_UPDATED_SYSTEM_APP)`), except
  `com.bluestacks.quest`. This includes `com.android.shell`, so `cmd notification post` is
  never forwarded. Testing it takes a user-installed app.

It then builds the JSON `{"packageName", "appName" (application label), "contentTitle"
(android.title), "contentText" (android.text), "mSecsFromEpochUTC" (Notification.when)}`
and calls `BstHostCallManager.onAppNotificationReceived(json)`, then
`BstHostCallService.native_onAppNotificationReceived` (`libhostcall_jni.so`). A non-zero
result only logs "Failed to send notification data to host".

## Wire format (bstvmsg, service 'hcal')

Method 44, `hcallOnAppNotificationReceivedRpc`:

    in+0x00 u32 44   in+0x04 u32 msglen (= 12 + len)   in+0x08 u32 len   in+0x0c JSON (len bytes)

Reply: result 0 (`out+len+4`; `out+0` is also zeroed).

## OpenStacks

- `qemu/hw/misc/bst_vmsg.c` writes each notification to QEMU's log as one line,
  `[bstvmsg] notification <JSON>`, with control bytes blanked.
- `frontend/openstacks/notify.py`: `NotificationWatch` follows `logs/<name>.qemu.log` from
  its end at player start, so older notifications are not replayed. `Notifier` shows each
  one:
  - Linux: `notify-send`, or the tray when `notify-send` is missing.
  - Windows: a tray icon's message (a toast), which goes silently to the notification
    centre while Do Not Disturb is on.
- The same title and text within a minute are shown once.
- Each notification is also written to the instance's Activity log.
- The instance setting **Notifications** turns the desktop notifications off.
