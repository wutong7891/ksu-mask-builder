# 内核侧适配

面具换了包名/签名后，内核必须用同一组参数重编，才能重新「认出」这个面具。
所有参数都在构建产物的 `SIGNATURE.txt` / `report.json` 里。

## 参数

```
KSU_PACKAGE_NAME=<新包名>
KSU_EXPECTED_SIZE=<证书 DER 长度，形如 0x033b>
KSU_EXPECTED_HASH=<证书 DER 的 SHA-256，小写 hex>
```

来源：`kernel/Kbuild` 把这三个变量写成编译期宏
`KSU_PACKAGE_NAME` / `EXPECTED_SIZE` / `EXPECTED_HASH`，
由 `kernel/manager/apk_sign.c` 在运行时校验面具 APK 的 v2 签名。

## 方式 A：LKM（内核本身没有内置 KSU）

本仓库的工作流已经按 KMI 编好，直接下载 `lkm-<KMI>/<KMI>_kernelsu.ko`。

KMI 必须与设备内核一致：

| 设备内核 | KMI |
| --- | --- |
| 5.10 (Android 12/13) | `android12-5.10` / `android13-5.10` |
| 5.15 (Android 13/14) | `android13-5.15` / `android14-5.15` |
| 6.1 (Android 14/15) | `android14-6.1` |
| 6.6 (Android 15/16) | `android15-6.6` |
| 6.12 (Android 16/17) | `android16-6.12` |
| 6.18 | `android17-6.18` |

用 `uname -r` 看设备真实内核版本，选最接近的 KMI。

## 方式 B：内置 KSU 内核

拿到 KernelSU 源码后：

```bash
git clone --depth 1 --branch v3.3.1 --filter=blob:none --sparse https://github.com/tiann/KernelSU.git ksu
cd ksu && git sparse-checkout set kernel
# 把 ksu/kernel 拷进你的内核树（或用 kernel/setup.sh 集成），然后：
make CONFIG_KSU=y \
  KSU_PACKAGE_NAME=com.your.pkg \
  KSU_EXPECTED_SIZE=0x033b \
  KSU_EXPECTED_HASH=<64 位小写 sha256>
```

`EXPECTED_HASH` 必须是**证书 DER** 的 SHA-256，不是 APK 的 SHA-256，
也不是 `apksigner` 打印的 `Signer #1 certificate SHA-256 digest` 之外别的东西
（其实两者相同，可直接用 `SIGNATURE.txt` 里的值）。

## 验证

刷入后打开新面具，能正常获取 root、模块列表正常，说明内核与面具签名已对上；
若面具提示「未安装/不受支持」，一般是：

1. 内核 `EXPECTED_HASH` 与 APK 实际签名不一致（换了私钥但没重编内核）；
2. KMI 不匹配（LKM 方式最常见）；
3. 设备上还残留旧版面具的包名/数据。
