# 从这里开始：选择场景并准备环境

这份仓库是 **EC2 Spot 容量控制器**，不是训练平台、GPU 安装器或 Kubernetes 自动扩缩容器。你给它审核过的机型、子网和启动模板，它创建一笔 maintain Fleet 来持续请求 N 台机器。没有库存时可以一直短缺。

## 第 1 步：选一个场景

| 我要做什么 | 下一份指南 | 能得到什么 |
|---|---|---|
| 在 Hyderabad 持续申请 B300 | [B300](b300.md) | 固定区域/机型、同一 Fleet 持续申请与补位 |
| 在一个区域维持其他 GPU Spot | [通用 GPU](generic-gpu.md) | 按目标台数维持经过元数据校验的 GPU 机器 |
| 给已有 EKS 提供 EC2 GPU 机器 | [已有 EKS](existing-eks.md) | Fleet 容量；节点注册与工作负载由你负责 |
| 比较区域或申请整体迁移 | [区域选择与迁移](regions.md) | 推荐/初次选择，或人工批准的先停后建迁移 |
| 没有 GPU quota，先用 CPU 验证 | [CPU 验证](cpu-test.md) | 本地检查可运行；云端部署仍需适配，不能直接套原账户模板 |

第一次使用建议先跑本地检查，再选 **通用 GPU 的单区域 standalone**。明确要 HYD B300 时直接走 B300 指南，不需要先部署通用场景。

## 第 2 步：获取代码和安装

以下在本地终端执行。下载依赖需要网络，但不会创建 AWS 资源。需要 Python 3.11+；部署运行时为 Python 3.12。CDK 合成另需 CDK 支持的 Node.js 和 CDK CLI。

```sh
git clone https://github.com/phoenixyy/phoenix-aws-gpu-spot-capacity-controller.git
cd phoenix-aws-gpu-spot-capacity-controller
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python -m h100_spot_controller.cli --help
.venv/bin/python scripts/release-check.py
```

**检查点：** CLI 打印帮助；release-check 打印离线测试结果，failures/errors 为 0。这个测试不验证 AWS 库存和真实 IAM。没有 Node 或依赖时先修复本地环境，不要为让测试过而跳过失败。

后文均假定终端工作目录是仓库根目录。CLI 保留历史 `h100` 名称，不代表只能申请 H100。

## 第 3 步：理解你需要自己准备的东西

控制器 stack 不创建这些启动前提。请让对应账户的管理员准备并审核：

| 输入 | 从哪里取得 | 为什么需要 |
|---|---|---|
| AWS profile | 已授权的本机 AWS 配置 | 区分只读检查和部署/操作身份，不把密钥写进仓库 |
| Region、GPU Spot quota | EC2/Service Quotas 控制台 | 区域可用且配额足够；总 quota 不等于空闲额度 |
| 子网与 Zone ID | VPC、EC2 控制台 | 每个 subnet 必须对应配置中的 AZ ID，不能把 AZ 名当 ID |
| AMI、启动模板 ID/固定版本 | EC2 控制台 | GPU 兼容镜像、驱动/bootstrap、网络和磁盘合同 |
| 实例 profile ARN 与 role ARN | IAM/启动模板 | YAML 填 profile ARN；CDK PassRole 参数填里面的 role ARN |
| 安全组、UserData SHA256 | 审核过的启动模板 | 用于核对启动合约，不能拿占位符直接启用 |
| 机型、台数、Spot cap | 负载和预算审核 | 每台 weight=1；cap 是小时单价，不是账单总额硬限额 |

IAM、权限、客户网络、合规或费用边界有疑问时，停在启用前让负责人审核。不要通过挂 Admin 或换身份绕过运行角色拒绝。

## 第 4 步：保存自己的配置

```sh
mkdir -p privateinputs
# 根据场景复制对应 example；后续指南给出具体命令。
```

`privateinputs/` 被 Git 忽略。每个 target 用一份 YAML；填写真实资源后不要提交。`enabled: false` 是起点，不是取消已有 Fleet 的办法。

## 如何知道自己走到哪了

1. `validate-target`：只证明本地配置结构合格。
2. `dry-run` / 专项 `preflight`：账户只读证据；不保证库存。
3. CDK deploy：控制平面部署，尚未创建 GPU 容量。
4. `target-put` disabled：配置写入状态表，仍不申请容量。
5. `target-put` enabled：允许定时任务产生 Spot 需求，开始可能的费用。
6. `capacity-review` + 日志：确认 Fleet、实例和满足台数；实例 running 不代表训练或 EKS 已就绪。
7. [停机与清理](stop-and-cleanup.md)：取消 demand、终止实例、检查剩余资源和费用。

不要跳过第 7 步。删控制器 stack 不等于停止 Fleet。
