# KSU 面具一键定制（GitHub Actions）

把 **KernelSU 官方最新源码** 编成你自己的「面具」APK：换包名、换应用名、换版本，
每次构建现场生成一把**全新的 RSA 4096 私钥**并签名，同时按本次私钥重新编译
各 KMI 的 `kernelsu.ko`，保证内核只认你这一个面具。

> 已实测通过：[Actions 运行记录](https://github.com/wutong7891/ksu-mask-builder/actions/runs/43207987172)
> —— 1 个面具 APK + 5 个 KMI 的 `kernelsu.ko` 全部产出，签名哈希与 APK 一致。

## 一键使用

1. 打开仓库 **Actions** → 左侧 **一键改面具** → 右侧 **Run workflow**。
2. 只填三个东西：

   | 输入 | 说明 | 例子 |
   | --- | --- | --- |
   | `package_name` | 新包名（applicationId） | `com.mymask.app` |
   | `app_name` | 桌面显示的应用名 | `面具` |
   | `version_name` | 版本名 | `1.0.0` |

   其余全部有默认值（含 KMI 列表），直接点绿色 **Run workflow**。
3. 跑完（约 8–15 分钟）在该次运行页面底部下载产物。

## 产物说明

| 文件 | 用途 |
| --- | --- |
| `mask-apk/KSU-Mask_<包名>_<版本>.apk` | 改好名字/包名/版本、已用本次新私钥签名的面具 |
| `mask-apk/SIGNATURE.txt` | 本次证书指纹、证书大小、内核编译参数 |
| `mask-apk/report.json` | 机器可读构建信息（包名/名称/版本/签名/哈希） |
| `lkm-<KMI>/<KMI>_kernelsu.ko` | 已把本次证书哈希写死的内核模块，只认你这一个面具 |

## 它是怎么做到的

KernelSU 的内核并不认识「包名」，它校验的是**面具 APK 的签名证书**：
`kernel/manager/apk_sign.c` 用编译期常量 `EXPECTED_SIZE` / `EXPECTED_HASH`
比对 APK v2 签名里的证书 DER。只换包名不改内核，面具会被内核当成陌生人。

本工作流按下面的闭环执行：

1. `keytool` 现场生成随机 alias + 随机密码的 RSA 4096 keystore（每次不同、不复用）；
2. 用 `KSU_PACKAGE_NAME` / `KSU_NAME` 让上游 gradle 直接产出新包名、新名称的 APK，
   并改写版本函数拿到你填的版本号；
3. 把官方 release 的 `ksud` 放进 `jniLibs` 再编译（manager 缺 `libksud.so` 会构建失败）；
4. 用这把新私钥签名，取证书 DER 的 SHA-256 与长度；
5. 以 `KSU_EXPECTED_SIZE=0x….`、`KSU_EXPECTED_HASH=<sha256>`、`KSU_PACKAGE_NAME=<新包名>`
   在官方 DDK 容器里并行重编各 KMI 的 `kernelsu.ko`；
6. 用 `aapt2` + `apksigner` 回头校验 APK 的包名/应用名/版本/签名，不一致直接失败。

每次运行第 1 步私钥都不同，于是 APK 签名、`kernelsu.ko` 里的哈希每次都不同。

## 安装前必读

- **换包名后与原版共存**：包名不同即可同时安装，但只有内核签名匹配的那个能拿 root
  （内核只认一个签名）。
- **设备是内置 KSU 内核**：必须用本次 `SIGNATURE.txt` 里的参数重编内核再刷，
  参数见 `docs/KERNEL.md`，否则新面具不被识别。
- **设备是 LKM 模式**：刷入本次产物里的 `lkm-<KMI>/<KMI>_kernelsu.ko`
  （KMI 必须与设备内核一致，`uname -r` 判断）。
- 覆盖安装旧版会因签名不同失败，需要先卸载旧面具（或换包名共存）。

## 关于私钥

- 私钥只存在于该次运行的内核里，用于签名 APK；**公开仓库默认不上传 `keys/`**，
  避免任何人拿到后签出被你内核信任的面具。
- 想留存私钥：把仓库转私有，并在运行参数里打开 `upload_keys`。
- 反过来，如果你把仓库设为公开又打开了 `upload_keys`，私钥就会公开——别这么干。

> 说明：私有仓库的 Actions 需要账户有可用额度；本账号当前私有仓库运行被
> Billing 拦截，所以仓库目前是公开的，因此默认不产出私钥。要保留私钥请先处理
> Billing，再把仓库改回私有。

## 固定私钥（可选）

默认每次构建都换新私钥。若想让多次构建签出**同一个签名**（这样升级面具不用卸载、
`kernelsu.ko` 也能一直复用），把私钥放进仓库 Secrets：

1. 本地生成一把自己的 keystore（一次性）：

   ```bash
   keytool -genkeypair -keystore mykey.jks -storetype PKCS12 \
     -alias mymask -keyalg RSA -keysize 4096 -validity 36500 \
     -dname "CN=com.mymask.app, O=My Mask, C=CN"
   base64 -w0 mykey.jks > mykey.b64
   ```

2. 仓库 **Settings → Secrets and variables → Actions** 新建三个 Secret：

   | Secret | 内容 |
   | --- | --- |
   | `KSU_KEYSTORE_B64` | `mykey.b64` 的完整内容 |
   | `KSU_KEYSTORE_PASS` | keystore 密码 |
   | `KSU_KEY_ALIAS` | 例如 `mymask` |

3. 运行时把 `use_fixed_key` 打开，之后每次都是同一签名；
   `SIGNATURE.txt` 里的哈希也固定，内核只需编一次。

关掉 `use_fixed_key` 就回到「每次全新私钥」模式。

## 安全

- 二次分发请遵守 KernelSU 的 GPL-3.0 许可。
- 本工具只改包名/名称/版本与签名，不修改 KernelSU 的提权与隐藏逻辑。

## 本地调试

```bash
python3 scripts/mask.py validate --pkg com.a.b --name 面具 --version 1.0.0
python3 scripts/mask.py resolve-ref --input ""      # 打印 KSU_REF
python3 scripts/mask.py fetch --ref v3.3.1 --dest ksu
python3 scripts/mask.py prepare --src ksu --keys keys --pkg com.a.b --name 面具 --version 1.0.0 --tag v3.3.1
python3 scripts/mask.py build --src ksu --pkg com.a.b --name 面具
```
