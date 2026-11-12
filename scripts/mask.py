#!/usr/bin/env python3
"""KSU 面具一键定制工具。

子命令：
  resolve-ref   解析 KernelSU 版本（空=最新 release）
  fetch         浅克隆 KernelSU 源码（只取 manager / kernel）
  prepare       生成唯一签名私钥 + 改包名/应用名/版本 + 放入 ksud
  build         调用 gradlew 编译 release APK
  finalize      校验 APK（包名/名称/版本/签名）并输出产物
  kmi-list      解析 KMI 列表
  kernel-vars   输出内核编译需要的 KSU_* 变量

所有子命令的机器可读结果都以 KEY=VALUE 打印到 stdout，日志走 stderr，
方便直接 >> $GITHUB_OUTPUT / $GITHUB_ENV。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

KSU_REPO = "https://github.com/tiann/KernelSU"
KSU_API = "https://api.github.com/repos/tiann/KernelSU"

# manager 打包的 ABI -> KernelSU release 里的 ksud 资产名
ABI_ASSETS = {
    "arm64-v8a": "ksud-aarch64-linux-android",
    "x86_64": "ksud-x86_64-linux-android",
    "riscv64": "ksud-riscv64-linux-android",
}

KMI_CHOICES = [
    "android12-5.10",
    "android13-5.10",
    "android13-5.15",
    "android14-5.15",
    "android14-6.1",
    "android15-6.6",
    "android16-6.12",
    "android17-6.18",
]

PKG_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*(\.[a-zA-Z][a-zA-Z0-9_]*)+$")
VER_RE = re.compile(r"^[0-9]+(\.[0-9]+){0,3}([-+][0-9A-Za-z.\-]+)?$")


class ToolError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def out(**kw) -> None:
    for k, v in kw.items():
        print(f"{k}={v}")


SECRET_FLAGS = {"-storepass", "-keypass", "-ks-pass", "-key-pass"}


def redact(cmd) -> str:
    parts = [str(c) for c in cmd]
    for i, part in enumerate(parts):
        if part in SECRET_FLAGS and i + 1 < len(parts):
            parts[i + 1] = "******"
    return " ".join(parts)


def run(cmd, cwd=None, env=None, check=True, capture=False):
    log("$ " + redact(cmd))
    proc = subprocess.run(
        [str(c) for c in cmd],
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )
    if proc.stdout:
        log(proc.stdout.rstrip())
    if check and proc.returncode != 0:
        raise ToolError(f"命令失败（{proc.returncode}）: {redact(cmd)}")
    return proc


# --------------------------------------------------------------------------- #
# 通用
# --------------------------------------------------------------------------- #
def http_get(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "ksu-mask-builder/1.0", "Accept": "*/*"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def http_json(url: str, timeout: int = 60) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ksu-mask-builder/1.0",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def latest_release_tag() -> str:
    try:
        return http_json(f"{KSU_API}/releases/latest")["tag_name"]
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"获取 KernelSU 最新 release 失败: {exc}") from exc


def sdk_build_tools() -> Path:
    """定位最新的 build-tools 目录。"""
    roots = [
        os.environ.get("ANDROID_SDK_ROOT"),
        os.environ.get("ANDROID_HOME"),
        str(Path.home() / "Android" / "Sdk"),
    ]
    best: tuple[tuple[int, ...], Path] | None = None
    for root in filter(None, roots):
        bt = Path(root) / "build-tools"
        if not bt.is_dir():
            continue
        for d in bt.iterdir():
            if not d.is_dir():
                continue
            key = tuple(int(n) for n in re.findall(r"\d+", d.name)) or (0,)
            if best is None or key > best[0]:
                best = (key, d)
    if best is None:
        raise ToolError("找不到 Android SDK build-tools（ANDROID_SDK_ROOT 未设置？）")
    return best[1]


def find_tool(name: str) -> Path:
    found = shutil.which(name)
    if found:
        return Path(found)
    bt = sdk_build_tools()
    for candidate in (bt / name, bt / f"{name}.bat", bt / f"{name}.exe"):
        if candidate.exists():
            return candidate
    raise ToolError(f"找不到工具: {name}")


# --------------------------------------------------------------------------- #
# resolve-ref
# --------------------------------------------------------------------------- #
def cmd_resolve_ref(args) -> None:
    ref = (args.input or "").strip()
    if not ref:
        ref = latest_release_tag()
        log(f"[i] 未指定版本，使用最新 release: {ref}")
    elif not re.match(r"^[A-Za-z0-9._\-/]+$", ref):
        raise ToolError(f"版本号不合法: {ref!r}")
    out(KSU_REF=ref)


# --------------------------------------------------------------------------- #
# fetch
# --------------------------------------------------------------------------- #
def cmd_fetch(args) -> None:
    dest = Path(args.dest).resolve()
    ref = args.ref.strip()
    if dest.exists():
        shutil.rmtree(dest)

    base = [
        "git", "clone", "--depth", "1", "--branch", ref,
        "--filter=blob:none", "--sparse", KSU_REPO, str(dest),
    ]
    try:
        run(base, capture=True)
    except ToolError:
        log("[!] 稀疏克隆失败，退回普通浅克隆")
        if dest.exists():
            shutil.rmtree(dest)
        run(["git", "clone", "--depth", "1", "--branch", ref, KSU_REPO, str(dest)], capture=True)

    run(["git", "sparse-checkout", "set", "manager", "kernel", "uapi"], cwd=dest, capture=True)
    # 版本号由 gradle 用 git 计算，这里补一个轻量 tag 保证 describe 一定能用
    run(["git", "fetch", "--tags", "--depth", "1", "origin"], cwd=dest, check=False, capture=True)
    desc = run(["git", "describe", "--tags", "--always"], cwd=dest, capture=True, check=False)
    out(KSU_DESCRIBE=(desc.stdout or "").strip())
    out(KSU_DIR=str(dest))


# --------------------------------------------------------------------------- #
# 签名私钥
# --------------------------------------------------------------------------- #
def _write_key_outputs(keys_dir: Path, pkg: str, alias: str, password: str, der_bytes: bytes) -> dict:
    (keys_dir / "keystore.env").write_text(
        "KEYSTORE_FILE=release.jks\n"
        f"KEYSTORE_PASSWORD={password}\n"
        f"KEY_ALIAS={alias}\n"
        f"KEY_PASSWORD={password}\n"
        f"CERT_SHA256={hashlib.sha256(der_bytes).hexdigest()}\n"
        f"CERT_SIZE_HEX=0x{len(der_bytes):04x}\n",
        encoding="utf-8",
    )
    os.chmod(keys_dir / "keystore.env", 0o600)
    keylib = {
        "package": pkg,
        "alias": alias,
        "cert_sha256": hashlib.sha256(der_bytes).hexdigest(),
        "cert_size": len(der_bytes),
        "cert_size_hex": f"0x{len(der_bytes):04x}",
    }
    (keys_dir / "signature.json").write_text(
        json.dumps(keylib, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return {
        "alias": alias,
        "password": password,
        "cert_sha256": keylib["cert_sha256"],
        "cert_size": len(der_bytes),
        "keystore": str(keys_dir / "release.jks"),
    }


def export_der(ks: Path, alias: str, password: str, env: dict) -> bytes:
    """从 keystore 导出证书 DER，自动适配 JKS / PKCS12。"""
    der = ks.parent / "release.der"
    last = ""
    for extra in ([], ["-storetype", "PKCS12"], ["-storetype", "JKS"]):
        if der.exists():
            der.unlink()
        proc = subprocess.run(
            ["keytool", "-exportcert", "-keystore", str(ks), "-alias", alias,
             "-storepass", password, "-file", str(der)] + extra,
            text=True, capture_output=True, env=env,
        )
        if proc.returncode == 0 and der.exists():
            return der.read_bytes()
        last = (proc.stdout + proc.stderr).strip()
    raise ToolError(f"无法从 keystore 导出证书（alias 或密码不对？）: {last[:300]}")


def load_fixed_keystore(keys_dir: Path, pkg: str) -> dict:
    """使用仓库 Secrets 里的固定私钥，保证多次构建签名一致。"""
    b64 = (os.environ.get("KSU_KEYSTORE_B64") or "").strip()
    password = os.environ.get("KSU_KEYSTORE_PASS") or ""
    alias = (os.environ.get("KSU_KEY_ALIAS") or "").strip()
    missing = [n for n, v in (("KSU_KEYSTORE_B64", b64), ("KSU_KEYSTORE_PASS", password),
                              ("KSU_KEY_ALIAS", alias)) if not v]
    if missing:
        raise ToolError("use_fixed_key 已开启，但缺少 Secret: " + ", ".join(missing))

    keys_dir.mkdir(parents=True, exist_ok=True)
    ks = keys_dir / "release.jks"
    try:
        ks.write_bytes(base64.b64decode(b64, validate=True))
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"KSU_KEYSTORE_B64 不是合法 base64: {exc}") from exc

    env = dict(os.environ, LANG="C", LC_ALL="C")
    der_bytes = export_der(ks, alias, password, env)
    subprocess.run(
        ["keytool", "-exportcert", "-rfc", "-keystore", str(ks),
         "-alias", alias, "-storepass", password],
        capture_output=True, text=True, env=env,
    )
    info = _write_key_outputs(keys_dir, pkg, alias, password, der_bytes)
    log(f"[i] 使用固定私钥，证书指纹 {info['cert_sha256']}")
    return info


def generate_keystore(keys_dir: Path, pkg: str) -> dict:
    keys_dir.mkdir(parents=True, exist_ok=True)
    alias = "ksu-" + os.urandom(4).hex()
    password = os.urandom(24).hex()
    ks = keys_dir / "release.jks"
    if ks.exists():
        ks.unlink()

    env = dict(os.environ, LANG="C", LC_ALL="C")
    run([
        "keytool", "-genkeypair",
        "-keystore", str(ks),
        "-storetype", "PKCS12",
        "-alias", alias,
        "-keyalg", "RSA",
        "-keysize", "4096",
        "-sigalg", "SHA256withRSA",
        "-validity", "36500",
        "-storepass", password,
        "-keypass", password,
        "-dname", f"CN={pkg}, OU=KSU Mask Builder, O=KSU Mask Builder, C=CN",
    ], env=env, capture=True)

    der_bytes = export_der(ks, alias, password, env)
    subprocess.run(
        ["keytool", "-exportcert", "-rfc", "-keystore", str(ks),
         "-alias", alias, "-storepass", password],
        capture_output=True, text=True, env=env,
    )
    info = _write_key_outputs(keys_dir, pkg, alias, password, der_bytes)
    log(f"[i] 已生成新私钥，证书指纹 {info['cert_sha256']}")
    return info


# --------------------------------------------------------------------------- #
# prepare
# --------------------------------------------------------------------------- #
def patch_manager_gradle(manager: Path, version_name: str, version_code: int) -> None:
    gradle = manager / "build.gradle.kts"
    if not gradle.exists():
        raise ToolError(f"缺少 {gradle}，KernelSU 目录结构变了？")
    text = gradle.read_text(encoding="utf-8")

    # 直接替换两个版本函数体，避免依赖函数内部的写法（getGitDescribe / commitCount 等）
    code_pat = re.compile(r"fun\s+getVersionCode\s*\(\)\s*:\s*Int\s*\{.*?\n\}", re.S)
    name_pat = re.compile(r"fun\s+getVersionName\s*\(\)\s*:\s*String\s*\{.*?\n\}", re.S)

    new_text, n_code = code_pat.subn(
        "fun getVersionCode(): Int {\n    return %d\n}" % version_code, text
    )
    new_text, n_name = name_pat.subn(
        'fun getVersionName(): String {\n    return "%s"\n}' % version_name, new_text
    )
    if n_code != 1 or n_name != 1:
        raise ToolError(
            "未能改写版本号（manager/build.gradle.kts 结构可能已变化）："
            f"getVersionCode 命中 {n_code} 次，getVersionName 命中 {n_name} 次"
        )
    gradle.write_text(new_text, encoding="utf-8")
    log(f"[i] 版本改写为 {version_name} ({version_code})")


def write_signing_props(manager: Path, keys_dir: Path, info: dict) -> None:
    shutil.copy2(info["keystore"], manager / "key.jks")
    props = manager / "gradle.properties"
    text = props.read_text(encoding="utf-8") if props.exists() else ""
    wanted = {
        "KEYSTORE_FILE": "key.jks",
        "KEYSTORE_PASSWORD": info["password"],
        "KEY_ALIAS": info["alias"],
        "KEY_PASSWORD": info["password"],
    }
    for key, value in wanted.items():
        if re.search(rf"^{re.escape(key)}=.*$", text, flags=re.M):
            text = re.sub(rf"^{re.escape(key)}=.*$", f"{key}={value}", text, flags=re.M)
        else:
            text += ("" if text.endswith("\n") or not text else "\n") + f"{key}={value}\n"
    props.write_text(text, encoding="utf-8")
    log("[i] 已写入 manager/gradle.properties 签名配置")


def install_ksud(manager: Path, tag: str, base_apk: Path | None) -> list[str]:
    """把 release 里的 ksud 二进制放进 jniLibs，作为 libksud.so 打包。"""
    jni = manager / "app" / "src" / "main" / "jniLibs"
    installed: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for abi, asset in ABI_ASSETS.items():
            target = jni / abi / "libksud.so"
            target.parent.mkdir(parents=True, exist_ok=True)
            data = None
            try:
                url = f"https://github.com/tiann/KernelSU/releases/download/{tag}/{asset}"
                data = http_get(url)
                if not data.startswith(b"\x7fELF"):
                    raise ToolError("下载到的不是 ELF")
            except Exception as exc:  # noqa: BLE001
                log(f"[!] 下载 {asset} 失败: {exc}")
                data = None

            if data is None and base_apk and base_apk.exists():
                with zipfile.ZipFile(base_apk) as zf:
                    name = f"lib/{abi}/libksud.so"
                    if name in zf.namelist():
                        data = zf.read(name)
                        log(f"[i] 从 release APK 提取 {name}")

            if data is None:
                raise ToolError(
                    f"{abi} 的 ksud 获取失败。请确认 {tag} 是官方 release tag"
                    "（main 分支没有预编译 ksud）。"
                )
            target.write_bytes(data)
            os.chmod(target, 0o755)
            installed.append(abi)
            log(f"[i] jniLibs/{abi}/libksud.so ({len(data)} bytes)")
    return installed


def cmd_prepare(args) -> None:
    src = Path(args.src).resolve()
    manager = src / "manager"
    keys_dir = Path(args.keys).resolve()

    if not (manager / "app" / "build.gradle.kts").exists():
        raise ToolError(f"{manager} 不是 KernelSU manager 目录")

    version_code = int(args.code) if args.code else derive_version_code(args.pkg, args.version)
    use_fixed = (os.environ.get("USE_FIXED_KEY") or "").strip().lower() in ("1", "true", "yes")
    info = load_fixed_keystore(keys_dir, args.pkg) if use_fixed else generate_keystore(keys_dir, args.pkg)
    patch_manager_gradle(manager, args.version, version_code)
    write_signing_props(manager, keys_dir, info)
    abis = install_ksud(manager, args.tag, Path(args.base_apk) if args.base_apk else None)

    out(
        CERT_SHA256=info["cert_sha256"],
        CERT_SIZE_HEX=f"0x{info['cert_size']:04x}",
        CERT_SIZE=info["cert_size"],
        VERSION_CODE=version_code,
        ABI_LIST=",".join(abis),
        KEY_MODE="fixed" if use_fixed else "generated",
    )


def derive_version_code(pkg: str, version: str) -> int:
    digest = hashlib.sha1(f"{pkg}:{version}".encode()).hexdigest()
    return 30000 + int(digest[:6], 16) % 70000


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #
def cmd_build(args) -> None:
    manager = Path(args.src).resolve() / "manager"
    gradlew = manager / "gradlew"
    if not gradlew.exists():
        raise ToolError(f"缺少 {gradlew}")
    os.chmod(gradlew, 0o755)

    env = dict(os.environ)
    env.setdefault("GRADLE_OPTS", "-Xmx4g -Dorg.gradle.jvmargs=-Xmx4g")
    cmd = [
        "./gradlew", "assembleRelease",
        "--no-daemon", "--console=plain", "--stacktrace",
        f"-PKSU_PACKAGE_NAME={args.pkg}",
        f"-PKSU_NAME={args.name}",
    ]
    proc = subprocess.run(cmd, cwd=str(manager), env=env, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    # gradle 日志一律走 stderr，避免污染 $GITHUB_OUTPUT
    print(proc.stdout, file=sys.stderr, flush=True)
    if proc.returncode != 0:
        raise ToolError(f"gradle 构建失败（{proc.returncode}），详见上方日志")

    apks = sorted(
        (manager / "app" / "build" / "outputs" / "apk" / "release").glob("*.apk"),
        key=lambda p: p.stat().st_mtime,
    )
    if not apks:
        raise ToolError("构建成功但没找到 APK 产物")
    out(BUILT_APK=str(apks[-1]))
    log(f"[i] 产物: {apks[-1]} ({apks[-1].stat().st_size} bytes)")


# --------------------------------------------------------------------------- #
# finalize / verify
# --------------------------------------------------------------------------- #
def aapt_badging(apk: Path) -> str:
    aapt2 = find_tool("aapt2")
    proc = subprocess.run([str(aapt2), "dump", "badging", str(apk)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        aapt = find_tool("aapt")
        proc = subprocess.run([str(aapt), "dump", "badging", str(apk)],
                              capture_output=True, text=True)
    if proc.returncode != 0:
        raise ToolError(f"aapt 解析 APK 失败: {proc.stdout}{proc.stderr}")
    return proc.stdout


def apk_signer_sha256(apk: Path) -> str:
    apksigner = find_tool("apksigner")
    proc = subprocess.run([str(apksigner), "verify", "--print-certs", str(apk)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise ToolError(f"签名校验失败: {proc.stdout}{proc.stderr}")
    m = re.search(r"certificate SHA-256 digest:\s*([0-9a-fA-F]+)", proc.stdout)
    if not m:
        raise ToolError("未能从 apksigner 输出中解析证书指纹")
    return m.group(1).lower()


def cmd_finalize(args) -> None:
    apk = Path(args.apk).resolve()
    outdir = Path(args.out).resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    badging = aapt_badging(apk)
    pkg_m = re.search(r"package: name='([^']+)' versionCode='([^']+)' versionName='([^']*)'", badging)
    if not pkg_m:
        raise ToolError("aapt 输出里没有 package 信息")
    pkg, version_code, version_name = pkg_m.groups()
    label_m = re.search(r"application-label:'([^']*)'", badging)
    label = label_m.group(1) if label_m else ""

    signer = apk_signer_sha256(apk)

    problems = []
    if pkg != args.pkg:
        problems.append(f"包名未生效: 期望 {args.pkg}，实际 {pkg}")
    if version_name != args.version:
        problems.append(f"版本名未生效: 期望 {args.version}，实际 {version_name}")
    if str(args.code) and version_code != str(args.code):
        problems.append(f"版本号未生效: 期望 {args.code}，实际 {version_code}")
    if args.name and label and args.name not in label:
        problems.append(f"应用名可能未生效: 期望 {args.name}，实际 {label}")
    if args.cert_sha256 and signer != args.cert_sha256.lower():
        problems.append(f"签名不是本次生成的私钥: APK={signer} 期望={args.cert_sha256}")
    if problems:
        raise ToolError("APK 校验失败:\n  - " + "\n  - ".join(problems))

    final_name = f"KSU-Mask_{args.pkg}_{version_name}.apk"
    final_path = outdir / final_name
    shutil.copy2(apk, final_path)
    digest = hashlib.sha256(final_path.read_bytes()).hexdigest()

    report = {
        "package": pkg,
        "label": label,
        "version_name": version_name,
        "version_code": version_code,
        "cert_sha256": signer,
        "apk": final_name,
        "apk_sha256": digest,
        "apk_size": final_path.stat().st_size,
        "ksu_ref": args.ksu_ref,
    }
    (outdir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (outdir / "SIGNATURE.txt").write_text(
        "\n".join([
            f"包名        : {pkg}",
            f"应用名      : {label}",
            f"版本        : {version_name} ({version_code})",
            f"证书 SHA256 : {signer}",
            f"证书大小    : {args.cert_size_hex}",
            f"APK SHA256  : {digest}",
            f"APK 大小    : {final_path.stat().st_size} bytes",
            f"KernelSU    : {args.ksu_ref}",
            "",
            "内核侧编译参数（内置 KSU 或 LKM 都必须一致）：",
            f"  KSU_PACKAGE_NAME={pkg}",
            f"  KSU_EXPECTED_SIZE={args.cert_size_hex}",
            f"  KSU_EXPECTED_HASH={signer}",
        ]).rstrip() + "\n",
        encoding="utf-8",
    )
    log(f"[i] 校验通过: {final_path}")
    out(APK_PATH=str(final_path), APK_SHA256=digest, APK_LABEL=label,
        APK_VERSION_CODE=version_code)


# --------------------------------------------------------------------------- #
# 其它
# --------------------------------------------------------------------------- #
def cmd_kmi_list(args) -> None:
    items = [x.strip() for x in (args.input or "").split(",") if x.strip()]
    if not items:
        items = ["android14-6.1"]
    bad = [x for x in items if x not in KMI_CHOICES]
    if bad:
        raise ToolError(f"未知 KMI: {', '.join(bad)}；可选: {', '.join(KMI_CHOICES)}")
    seen, uniq = set(), []
    for x in items:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    out(KMI_MATRIX=json.dumps(uniq))


def cmd_kernel_vars(args) -> None:
    keys = Path(args.keys).resolve()
    sig = json.loads((keys / "signature.json").read_text(encoding="utf-8"))
    out(
        KSU_PACKAGE_NAME=sig["package"],
        KSU_EXPECTED_SIZE=sig["cert_size_hex"],
        KSU_EXPECTED_HASH=sig["cert_sha256"],
    )


def cmd_validate(args) -> None:
    problems = []
    if not PKG_RE.match(args.pkg or ""):
        problems.append(f"包名不合法: {args.pkg!r}（需形如 com.example.mask，至少两段）")
    if not (args.name or "").strip():
        problems.append("应用名不能为空")
    if not VER_RE.match(args.version or ""):
        problems.append(f"版本名不合法: {args.version!r}（例 1.0.0）")
    if args.code:
        if not args.code.isdigit() or not (1 <= int(args.code) <= 2_100_000_000):
            problems.append("版本号必须是 1..2100000000 的整数")
    if problems:
        raise ToolError("\n".join(problems))
    out(OK="1")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="KSU 面具一键定制")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("resolve-ref")
    s.add_argument("--input", default="")
    s.set_defaults(func=cmd_resolve_ref)

    s = sub.add_parser("fetch")
    s.add_argument("--ref", required=True)
    s.add_argument("--dest", required=True)
    s.set_defaults(func=cmd_fetch)

    s = sub.add_parser("prepare")
    s.add_argument("--src", required=True)
    s.add_argument("--keys", required=True)
    s.add_argument("--pkg", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--version", required=True)
    s.add_argument("--code", default="")
    s.add_argument("--tag", required=True, help="KernelSU release tag，用于下载 ksud")
    s.add_argument("--base-apk", default="", help="ksud 下载失败时从该 APK 提取")
    s.set_defaults(func=cmd_prepare)

    s = sub.add_parser("build")
    s.add_argument("--src", required=True)
    s.add_argument("--pkg", required=True)
    s.add_argument("--name", required=True)
    s.set_defaults(func=cmd_build)

    s = sub.add_parser("finalize")
    s.add_argument("--apk", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--pkg", required=True)
    s.add_argument("--name", default="")
    s.add_argument("--version", required=True)
    s.add_argument("--code", default="")
    s.add_argument("--cert-sha256", default="")
    s.add_argument("--cert-size-hex", default="")
    s.add_argument("--ksu-ref", default="")
    s.set_defaults(func=cmd_finalize)

    s = sub.add_parser("kmi-list")
    s.add_argument("--input", default="")
    s.set_defaults(func=cmd_kmi_list)

    s = sub.add_parser("kernel-vars")
    s.add_argument("--keys", required=True)
    s.set_defaults(func=cmd_kernel_vars)

    s = sub.add_parser("validate")
    s.add_argument("--pkg", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--version", required=True)
    s.add_argument("--code", default="")
    s.set_defaults(func=cmd_validate)

    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except ToolError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
