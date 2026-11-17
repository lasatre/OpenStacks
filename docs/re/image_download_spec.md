# BlueStacks 5 (nxt) Android engine image download - implementation spec

Client RE'd: BlueStacks 5.21.550.1031 (oem `nxt`), `HD-MultiInstanceManager.exe`
(sha256 3e714732f0e83f90603b4529218bd5d6b4f553736372ae55c35f53f6667672b6, image base 0x140000000).
All URLs below were verified live on 2026-11-17 with HEAD / Range requests only (no full download).

## TL;DR

There is NO per-image metadata API. The client builds a deterministic CDN URL and downloads one
self-extracting 7z file per image. The only API call is an optional CDN-host lookup that needs no auth.

```
URL = {cdn_base}/windows/nxt/5.21.550.1031/5a99b4c3b70826e26cecb85b296b66c5/Android/{Image}/{Image}.exe
cdn_base = https://cdn3.bluestacks.com/downloads                 (default / fallback, hardcoded)
         | https://ak-build.bluestacks.com/public/app-player     (returned by get_cdn_host for full-parameter calls)
```

Both bases serve identical bytes (same md5). cdn3 answers HEAD directly but answers a ranged GET with
`301 -> ak-build...`, so follow redirects (`curl -L`).
No auth, no cookies, no special User-Agent (plain curl UA works), no GUID needed for the artifact.

## 1. API

### 1.1 `POST https://cloud.bluestacks.com/app_player/get_cdn_host` (optional)
- Evidence: string `/app_player/get_cdn_host` @0x14035b568, used by FUN_1400885d0 (0x1400885d0);
  response callback FUN_140022a30 (0x140022a30) parses `cdn_base_path`; if never set it falls back to the literal
  `https://cdn3.bluestacks.com/downloads` (@0x140156d48, xrefs 0x140022b9e / 0x140022d95).
- GET -> 405. POST with `application/x-www-form-urlencoded` body (Content-Length required; `curl -d` does it).
  The client sends its common field set (builder FUN_1400848b0 @0x1400848b0, shared by all cloud calls):
  `bluestacks_account_id, campaign_hash, country, guid, hypervisor, install_id, locale, launcher_guid,
  launcher_version, machine_id, oem=nxt, player_version=5.21.550.1031, version_machine_id`.
  Request building: FUN_1400864a0 @0x1400864a0 (path + `?k=v&...` text, QNetworkRequest).
- Live tests with a random uuid4 and generic values were accepted:
  - body `oem=nxt&player_version=5.21.550.1031&guid=<random uuid4>&country=US&locale=en-US&hypervisor=hyperv&launcher_version=5.21.550.1031`
    -> `200 {"cdn_base_path": "https://ak-build.bluestacks.com/public/app-player"}`
  - body `oem=nxt` or empty -> `200 {"cdn_base_path": "https://cdn3.bluestacks.com/downloads"}`
  - The server picks the CDN; the result is irrelevant for correctness.
- Recommendation for OpenStacks: skip the call, use `https://cdn3.bluestacks.com/downloads`.
- Unrelated: `GET https://cloud-api-cdn.bluestacks.com/get_cdn_cached_apis` returns an endpoint routing table
  (`{"data":{"url1":"https://cloud-api-cdn.bluestacks.com","endpoints":{"/app/icon":"url1",...}},"status":"success"}`);
  nothing image related.

### 1.2 URL builder `getImageDownloadUrl(std::string const&)` = FUN_140034c10 (0x140034c10)
Recovered from immediates in the decompilation:
```
prefix = cdn_base + "/windows/" + "nxt" + "/" + "5.21.550.1031" + "/" + "5a99b4c3b70826e26cecb85b296b66c5"
url    = prefix + "/Android/" + imageName + "/" + imageName + ".exe"
```
- `cdn_base` global lives at DAT_1403b28f0+0x370 (default literal above).
- `5a99b4c3b70826e26cecb85b296b66c5` is a per-build constant baked into the exe. It is NOT md5 of
  version/oem strings (common combos tested). It is required: a wrong key gives 403 from CloudFront.
  It cannot be derived for other client versions without that version's exe. Pin to this build; the 2024-09-03
  artifacts are still served.
- Client log lines: `Generated url is %s`, `outFilePath %s` (MimBackend.cpp, `downloadImage`).

## 2. Artifact

One file per image: a Linux ELF x86-64 7-Zip SFX stub (392,848 bytes) followed by a standard 7z archive
(7z signature `37 7A BC AF 27 1C 00 03` at offset 392,848; file starts `7f 45 4c 46`).
`7zr.exe` in `C:\Program Files\BlueStacks_nxt\` extracts it directly (it finds the signature itself).
Method LZMA2:24 + BCJ, SOLID, 1 block, 287-byte (LZMA-encoded) headers at the tail.
Range requests work (`Accept-Ranges: bytes`, 206 verified), so resume / parallel chunks are possible.
Integrity: header `x-amz-meta-md5` (cdn3) == ETag md5 (ak-build) == md5 of the whole .exe.

| Image | Android | Content-Length | md5 (whole file) | Root.vhd size | Root.vhd CRC32 |
|---|---|---|---|---|---|
| Nougat32 | 7 | 514,831,954 | 316e7ad283cf6045bf0fd5caef7895b4 | 1,420,136,960 | 3BCE834D |
| Nougat64 | 7 | 714,106,476 | 8b48c5f81bcf8e9055fb1e58e75afd9f | not listed | |
| Pie64 | 9 | 594,114,143 | f482bb4cf40abcbec8422f5d4168c846 | 1,820,790,784 | 0545735F |
| Rvc64 | 11 | 693,493,911 | ffaea01e90e5e0ef2e7b1288c5df3306 | 2,187,881,984 | F1FD5ED5 |

Last-Modified 2024-09-03 for all. These four are the only images in this client's built-in trait table
(strings Nougat32 / Nougat64 / Pie64 / Rvc64 near `UtlAndroidImageTraits::get`). Tiramisu64 (Android 13) is NOT
offered to this version: `.../Android/Tiramisu64/Tiramisu64.exe` gives 403 (cdn3) / 404 (ak-build), although
`C:\ProgramData\BlueStacks_nxt\Engine\AppImagePreference.json` mentions it (newer clients: different version
path + key). Also probed and absent: Tiramisu32, Pie32, Rvc32, Oreo32, Hirsute64, Sv2_64 (403).

### Archive contents
Listed by building a sparse file (ELF stub stripped, 32-byte start header + last 200 KB fetched by ranged GET)
and running `7zr l -slt` on it. Pie64:
```
Root.vhd          1,820,790,784   system disk (VHD) - the bulk of the download
oem_Pie64.cfg             462
Android.bstk.in         6,366     VirtualBox-style machine template
fastboot.vdi       11,534,336     kernel + initramfs source for kernel.bin/initramfs.bin
Data.vhdx          83,886,080     empty data disk template, CRC32 FEE290BD (identical in all images)
```
Rvc64: same names (`oem_Rvc64.cfg` 462, `Android.bstk.in` 6366, fastboot.vdi 11,534,336 CRC 89261EB9).
Nougat32: `oem_Nougat32.cfg` 432, `Android.bstk.in` 6203, fastboot.vdi 9,437,184, Data.vhdx 83,886,080.
Flat archive (no top-level dir), one solid block: must stream the whole thing; Root.vhd is first.

## 3. Install flow in HD-MultiInstanceManager

Entry `MimBackend::downloadImage(QString x10, int, bool)` = FUN_140031dc0 (0x140031dc0, log tag `downloadImage`):
1. `Another image creation is already in progess, Exiting`; hypervisor compat check
   (`Incompatible hypervisor image download triggered, Ignoring`); SSE4 check (`bst.status.sse4_available`).
2. Free disk space check (FUN_140112c90): needs > 0x17FFFFFFF (~6 GiB) else `fShowInsufficientSpacePopup`.
3. Builds URL (FUN_140034c10) and output file `<DataDir>\Engine\<file>.exe` (literal "Engine" @0x14015552c);
   metrics events `mim_image_download_started/completed/failed`. Threaded Qt HTTP download with progress callback
   (FUN_140023740, param_2 == 0x73 = progress; UI states "Downloading" / "Creating"). Resume behaviour not proven
   (server supports Range).
4. Completion handler FUN_140023740 (0x140023740) runs via QProcess (timeout 1,800,000 ms):
   `"<InstallDir>\7zr.exe" x "<DataDir>\Engine\<Image>.exe" -o"<DataDir>\Engine\<Image>" -aoa`
   (literals `-o` / `x` / `-aoa` @0x140156e70). Logs `Zip file %s Successfully extracted to %s` or
   `Failed to Extract image zip file : %d`.
5. Deletes the downloaded .exe (`Unable to delete zip file at path %s`), emits `mim_image_install_completed`,
   adds the image to `bst.installed_images` (bluestacks.conf; `mimUpdateConfForInstanceForNewImage`), sets
   `bst.fresh_cpu_core` / `bst.fresh_cpu_ram` defaults (4 cores/4096 MB if RAM >= 7782 MB and >= 8 hw threads, else 2 cores), then creates the
   instance (FUN_14003edb0 -> `createFreshInstanceWrapper`, `Cloning from %s and image name is %s`).
6. Resulting layout `C:\ProgramData\BlueStacks_nxt\Engine\<Image>\` = the 5 archive files. On this machine the
   archive Data.vhdx CRC32 FEE290BD == installed `Engine\Pie64\Data_orig.vhdx` and `Engine\Rvc64\Data_orig.vhdx`
   (both 83,886,080 B): the pristine template is kept as `Data_orig.vhdx`; new instances copy
   `Data_orig.vhdx` -> `Data.vhdx` (clone code references `Data_orig.vhdx|vhd|vdi`, `Data.vhdx|vhd|vdi`), and `.bstk`
   is generated from `Android.bstk.in`. Installed `Android.bstk.in` is a newer revision (6362 B vs 6366 B in the archive) and
   installed `fastboot.vdi` differs from the archive (rewritten later by the client). I did not locate the
   exact instruction performing the Data.vhdx -> Data_orig.vhdx step; for OpenStacks treat the archive's
   `Data.vhdx` as `Data_template.vhdx`.

## 4. Implementation recipe for OpenStacks

1. `url = f"https://cdn3.bluestacks.com/downloads/windows/nxt/5.21.550.1031/5a99b4c3b70826e26cecb85b296b66c5/Android/{img}/{img}.exe"`,
   img in {Nougat32, Nougat64, Pie64, Rvc64}. HEAD first for `Content-Length` + `x-amz-meta-md5`.
2. GET with Range, following redirects, into `<img>.exe.part`; verify whole-file md5 against the table / header.
3. Strip the 392,848-byte ELF stub (find `37 7A BC AF 27 1C 00 03`) and extract with 7z (py7zr or `7z x`; 7-Zip
   also handles the SFX prefix directly, as `7zr.exe` does in the client).
4. Map to assets: `Root.vhd`; `fastboot.vdi` -> kernel.bin + initramfs.bin (existing prepare_assets.py);
   `Data.vhdx` -> `Data_template.vhdx`; keep `Android.bstk.in`, `oem_<Img>.cfg` for reference.
5. 7z verifies member CRC32 on extraction.

## 5. Privacy / auth
No account token or credential is needed for the CDN-host lookup or for the artifacts. A random uuid4 `guid` was
accepted by get_cdn_host; artifact download sends no identifiers. The real client would also send
bst.guid / machine_id / install_id / account id; none are needed.

## 6. Open items
- Per-build key for other BlueStacks versions (needed for Tiramisu64): only obtainable from that version's
  `HD-MultiInstanceManager.exe` (immediates in `getImageDownloadUrl`); no metadata API found.
  Cloud `POST /bs4/check_upgrade` returned 500 for generic params (not pursued).
- Exact resume semantics of the client downloader; exact Data.vhdx -> Data_orig.vhdx step.
- Work files (decompiled FUN_140023740 / FUN_140031dc0, sparse 7z listings):
  <local scratch folder, not kept>
