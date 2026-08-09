# RunPod RTX 5090：加入 btcpuzzle.info Puzzle #71

这条路径只适用于公开的 [Puzzle #38 测试池](https://btcpuzzle.info/puzzle/38)
和 [Puzzle #71](https://btcpuzzle.info/puzzle/71)。它调用官方 Pool 客户端，不接受自定义地址或私钥范围。

> `btc-puzzle-lab run 71 --auto` 是仓库原有的独立扫描流程，**不会加入 Pool**。
> 加入 Pool 必须使用本文的 `btc-puzzle-pool` 命令。

## 1. 创建 Pod

- GPU：RTX 5090
- 镜像：`runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404`
- 使用按需实例，不要选 Spot / 可抢占实例
- 给 `/workspace` 挂持久卷，建议至少 20 GiB
- Jupyter Terminal 可以直接使用；若启用 SSH，还要先在 RunPod 配置 SSH 公钥

脚本会核验 Ubuntu 24.04、`nvidia-smi` 和 CUDA 12.8。镜像实际环境不符时会停止，
不会掩盖错误并继续运行。

## 2. 在可信的本地电脑生成 RSA 密钥

不要在 RunPod 生成或保存生产私钥：

```bash
umask 077
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 \
  -out btcpuzzle-private.pem
openssl pkey -in btcpuzzle-private.pem -pubout \
  -out btcpuzzle-public.pem
```

- `btcpuzzle-private.pem`：只保存在本地电脑，并另做离线备份
- `btcpuzzle-public.pem`：可以作为 RunPod Secret 注入
- Pool Token：从 btcpuzzle.info 账户取得；泄露后立即撤销

在 RunPod 中用 Secret / 环境变量配置：

| 变量 | 内容 |
|---|---|
| `BTCPUZZLE_USER_TOKEN` | Pool Token |
| `BTCPUZZLE_RSA_PUBLIC_KEY` | `btcpuzzle-public.pem` 的完整内容 |
| `BTCPUZZLE_WORKER` | 可选，最多 15 个字母或数字，例如 `runpod5090` |

也可以把公钥放在 Pod 文件中，再设置
`BTCPUZZLE_RSA_PUBLIC_KEY_FILE=/path/to/btcpuzzle-public.pem`。不要上传私钥。

## 3. 安装固定版本的客户端

```bash
cd /workspace
git clone https://github.com/catamitez0-maker/btc-puzzle-lab.git
cd btc-puzzle-lab
bash scripts/runpod-pool-bootstrap.sh
source .venv/bin/activate
```

bootstrap 会：

1. 安装编译依赖；
2. 固定官方源码到提交 `025e2656fc5ff6f3e8ea51477b8374c8000ee366`；
3. 为 RTX 5090 编译原生 `sm_120` SASS，并同时保留 `compute_120` PTX；
4. 应用 fail-closed 安全补丁；
5. 执行不会领取范围的 `doctor --puzzle 38`。

它不会调用官方 `btcpuzzle.sh`，因此不会修改 `/etc/resolv.conf`，Token 和 RSA 公钥也
不会进入子进程命令行。运行时配置只写入权限为 `0600` 的临时 `pool.conf`。

## 4. 先通过 Puzzle #38

```bash
btc-puzzle-pool test --timeout 900
```

测试会持续领取 #38 测试范围，直到同一次运行完成：找到目标、提交该范围、保存 RSA
密文。测试池共有 32 个范围；RTX 5090 通常很快，但 Pool 数据每 30 分钟重置。

成功后会生成：

```text
state/pool/results/WINNER_*.txt
```

把该文件下载回可信的本地电脑，然后解密：

```bash
awk -F': ' '/^Private Key:/{print $2; exit}' WINNER_*.txt \
  | openssl base64 -d -A -out puzzle38.enc
openssl pkeyutl -decrypt \
  -inkey btcpuzzle-private.pem \
  -in puzzle38.enc \
  -pkeyopt rsa_padding_mode:oaep
```

确认结果是 64 位十六进制，并与公开的 #38 解答对应。只有完成这一步，才算验证了
“RunPod 公钥加密 → 下载密文 → 本地私钥解密”的完整链路。程序的 #71 硬门槛会绑定
已测试二进制的 SHA-256 和当前 GPU compute capability；更换二进制或 GPU 后必须重测。

## 5. 运行 Puzzle #71

```bash
btc-puzzle-pool doctor --puzzle 71
btc-puzzle-pool run --puzzle 71
```

用 `Ctrl-C` 正常停止。加密 winner 会在运行期间复制到持久卷，而不是只等进程退出。

## 必须知道的限制

- 官方协议没有公开 checkpoint / resume / release API；中断后不能从当前范围的偏移继续。
- #71 的范围重分配窗口是 12 小时，因此不适合 Spot / 可抢占 Pod。
- 当前 [#71 页面](https://btcpuzzle.info/puzzle/71) 的一个范围约
  `35,184,372,088,831` keys；实际 5090 worker 完成一个范围通常以小时计，不能按纯
  benchmark 的理想除法估算账单。
- 适配器默认启用官方 `save_key`：命中结果先用你的 RSA 公钥加密，再保存到 Pool 账户，
  同时在持久卷留密文副本。Pool 无法直接解密，但能看到任务和范围元数据，理论上可以
  推断命中区域；追求最高隐私时应使用自建 HTTPS `api_share`，不要依赖 Pool 存储。
- 官方客户端是 GPL-3.0。本仓库将它作为独立进程构建并记录源码提交；分发修改后的
  二进制或镜像时须保留许可证并提供对应源码。

## 本仓库额外修复的上游风险

固定提交构建时会精确匹配并修复以下行为；任何补丁匹配失败都会阻止安装：

- Pool API 返回的目标地址必须是官方 #38 或 #71 地址；
- 范围提交只有 HTTP 200 才算成功；
- RSA 不可用或加密失败时拒绝输出、发送明文；
- `save_key` 和 Telegram 必须检查 HTTP / 成功响应；
- Token、公钥和明文私钥会从包装器输出中脱敏。

官方参考：
[入池指南](https://btcpuzzle.info/how-to-join-pool)、
[API 文档](https://btcpuzzle.info/api-documentation)、
[官方客户端](https://github.com/ilkerccom/btcpuzzle)、
[GPU benchmark](https://btcpuzzle.info/benchmark)。
