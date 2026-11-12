# KSU 面具一键定制（GitHub Actions）

把 **KernelSU 官方最新源码** 编成你自己的「面具」APK：换包名、换应用名、换版本，
每次构建现场生成一把**全新的 RSA 4096 私钥**并签名，同时按本次私钥重新编译
各 KMI 的 `kernelsu.ko`，保证内核只认你这一个面具。

## 一键使用

1. 打开仓库的 **Actions** 页 → 左侧 **一键改面具** → 右侧 **Run workflow**。
2. 只填三个东西：

   | 输入 | 说明 | 例子 |
   | --- | --- | --- |
   | `package_name` | 新包名（applicationId） | `com.mymask.app` |
   | `app_name` | 桌面显示的应用名 | `面具` |
   | `version_name` | 版本名 | `1.0.0` |

   其余全部有默认值，直接点绿色 **Run workflow**。
3. 跑完（约 15–30 分钟）在该次运行页面底部下载 **mask-apk** 和
   **lkm-\*** 产物。

## 产物说明

| 文件 | 用途 |
| --- | --- |
| `KSU-Mask_<包名>_<版本>.apk` | 改好名字/包名/版本、已用本次新私钥签名的面具 |
| `SIGNATURE.txt` | 本次的证书指纹、证书大小、内核编译参数 |
| `report.json` | 机器可读的构建信息（包名/名称/版本/签名/哈希） |
| `keys/release.jks`、`keys/keystore.env` | **本次独家私钥**（保存好，后续升级同一面具要用它） |
| `lkm-<KMI>/<KMI>_kernelsu.ko` | 已把本次证书哈希写死的内核模块，只认你这一个面具 |

## 它是怎么做到的

KernelSU 的内核并不认识「包名」，它校验的是**面具 APK 的签名证书**：
`kernel/manager/apk_sign.c` 用编译期常量 `EXPECTED_SIZE` / `EXPECTED_HASH`
比对 APK v2 签名里的证书 DER。所以只换包名不改内核，面具会被内核当成陌生人。

本工作流因此按下面的闭环执行：

1. `keytool` 现场生成随机 alias + 随机密码的 RSA 4096 keystore（不落库、不复用）；
2. 用 `KSU_PACKAGE_NAME` / `KSU_NAME` 让上游 gradle 直接产出新包名、新名称的 APK，
   并把版本号函数改成你填的版本；
3. 把官方 release 的 `ksud` 放进 `jniLibs` 再编译（manager 缺 `libksud.so` 会构建失败）；
4. 用这把新私钥签名，取证书 DER 的 SHA-256 与长度；
5. 以 `KSU_EXPECTED_SIZE=0x….`、`KSU_EXPECTED_HASH=<sha256>`、`KSU_PACKAGE_NAME=<新包名>`
   重新编译对应 KMI 的 `kernelsu.ko`（DDK 官方容器，矩阵并行）。

每次运行第 1 步的私钥都不同，于是 APK 签名、`kernelsu.ko` 里的哈希每次都不同。

## 安装前必读

- **换包名后与原版共存**：包名不同即可同时安装，但只有内核签名匹配的那一个能用
  root（内核只认一个签名）。
- **设备是内置 KSU 内核**：必须用本次的 `KSU_EXPECTED_*` 重新编译内核再刷入，
  否则新面具不会被识别。编译参数见 `SIGNATURE.txt` 或 `docs/KERNEL.md`。
- **设备是 LKM 模式**：刷入本次产物里的 `lkm-<KMI>/<KMI>_kernelsu.ko`（KMI 必须与
  设备内核一致，用 `uname -r` 判断）。
- 覆盖安装旧版会因签名不同而失败，需要先卸载旧面具（或在另一个包名下共存）。

## 安全

- 产物里的 `keys/` 就是你的私钥，**别提交进仓库、别公开分享**；泄露后任何人都能
  签出被你的内核信任的面具。
- 本仓库包含私钥产物，建议保持 **私有仓库**。
- 二次分发请遵守 KernelSU 的 GPL-3.0 许可。

## 本地调试

```bash
python3 scripts/mask.py validate --pkg com.a.b --name 面具 --version 1.0.0
python3 scripts/mask.py resolve-ref --input ""      # 打印 KSU_REF
python3 scripts/mask.py fetch --ref v3.3.1 --dest ksu
python3 scripts/mask.py prepare --src ksu --keys keys --pkg com.a.b --name 面具 --version 1.0.0 --tag v3.3.1
python3 scripts/mask.py build --src ksu --pkg com.a.b --name 面具
```
