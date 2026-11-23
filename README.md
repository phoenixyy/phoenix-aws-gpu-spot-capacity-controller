# GPU Spot Capacity Controller

**告诉控制器「在审核过的区域、机型和价格范围里维持 N 台 Spot 机器」，它用一笔 EC2 maintain Fleet 持续申请，并在机器终止后尝试补位。**

它不是训练平台，也不是 Karpenter：不保证库存、不自动恢复任务/checkpoint、不安装 GPU 驱动、不按 Pod 扩缩容。没有库存时，目标台数可能长期不足。

## 第一次拿到仓库？从这里开始

**先读 [从零开始：安装、准备输入和选择场景](docs/guides/start-here.md)。** 不需要先阅读源码或自己拼运维命令。

然后只选与你目标对应的场景，每篇都有编号步骤、需要编辑的文件、命令、检查点和停止方式：

| 你的目标 | Step-by-step guide | 起始配置 | 当前边界 |
|---|---|---|---|
| Hyderabad 申请 B300 Spot | [B300 独立部署](docs/guides/b300.md) | [B300 example](config/hyd-b300.example.yaml) | 固定 ap-south-2 / p6-b300.48xlarge；B300 客户实机部署/获配尚未验证 |
| 单区域维持其他 GPU Spot | [通用 GPU standalone](docs/guides/generic-gpu.md) | [通用 example](config/target.example.yaml) | 先人工准备启动输入，默认关闭，启用后才申请机器 |
| 给已有 EKS 提供 GPU EC2 | [已有 EKS](docs/guides/existing-eks.md) + [NTH 中断处理](docs/guides/eks-node-termination-handler.md) | 通用 example，修改 integration_mode | 独立 IMDS NTH cordon/drain；不创建集群或恢复任务，需审核安装；HYD B300 不支持 |
| 比较区域、首次选区或申请迁移 | [区域选择与迁移](docs/guides/regions.md) | 通用 example，准备所有候选区域 | 迁移需批准，先停源再建目的；B300/CPU 不允许跨区 |
| 没有 GPU quota，先验证 CPU | [CPU 验证](docs/guides/cpu-test.md) | [CPU example](config/cpu-validation.example.yaml) | 本地检查可用；任意账户云端部署尚需适配，不能直接套标准 CDK |
| 用完停止花钱 | [暂停、取消与清理](docs/guides/stop-and-cleanup.md) | 你自己的配置 | enabled=false 不取消已有 Fleet，删 stack 也不取消 Fleet |

### 一条完整使用路线

```text
安装 → 选择场景 → 复制 example 到 privateinputs/
     → 管理员准备 AMI/LT/网络/实例角色 → 填自己的配置
     → 本地校验 + 账户只读检查 → 审核部署控制平面
     → 写入 disabled target → 单独批准费用后 enable
     → 查看 Fleet/实例/日志 → 禁用 + 取消 Fleet + 终止实例 + 核查残留
```

部署控制平面只创建 Lambda、DynamoDB、EventBridge、SNS 和监控；**不会帮你创建客户 VPC、子网、GPU AMI、启动模板或业务任务**。这些是使用前提，输入来源见公共安装指南。

## 先在本地试一遍，不创建 AWS 资源

需要 Python 3.11+。CDK 合成还需要受支持的 Node.js；部署需要 CDK CLI 和经过审核的账户准备。

```sh
git clone https://github.com/phoenixyy/phoenix-aws-gpu-spot-capacity-controller.git
cd phoenix-aws-gpu-spot-capacity-controller
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python -m h100_spot_controller.cli --help
.venv/bin/python -m h100_spot_controller.cli validate-target config/hyd-b300.example.yaml
.venv/bin/python scripts/release-check.py
```

依赖安装需要网络；validate-target 只检查本地结构，示例占位符不是可用资源。release-check 阻断 AWS HTTP 做离线回归，不证明库存或真实运行 IAM。不要直接把 example 改 enabled=true 就运行。

## 常用命令分别做什么

所有路径以仓库根目录运行 `python -m h100_spot_controller.cli`；安装后也可用历史入口 `h100-spot-controller`。

| 命令 | 用途 | 是否写 AWS |
|---|---|---|
| validate-target | 本地结构与场景约束校验 | 否，不访问 AWS |
| discover | 查 AZ、offering、机型元数据 | 只读 |
| dry-run | 预览配置、启动合同、价格/选择等证据 | 只读；不是启动实例 |
| preflight | B300/CPU 专项预检，通用 GPU 用 dry-run | 只读 |
| target-review | 查看状态表中的配置和版本 | 只读 |
| target-put --execute | 条件写入配置；enabled 时另需 --enable-capacity | 是，DynamoDB；启用后定时任务可申请容量 |
| capacity-review | 枚举所有候选区域的 owned Fleet/实例；加 --table 读权威区域 | 只读 |
| cleanup | 预览清理；--execute 取消 Fleet，另加 --terminate-instances 终止机器 | 不带 execute 只读；带 execute 写 EC2 |
| failover-request/review/approve/reject | 通用 GPU 的整体迁移预览、计划和审批 | 依命令/execute 而定；批准可触发后续终止与重建 |

详细参数与可复制命令在对应场景指南，不要猜测版本号、plan_id、资源 ID。

## 文件地图：哪些要改，哪些不用改

| 文件/目录 | 做什么 | 使用者要改吗 |
|---|---|---|
| [docs/guides/](docs/guides/) | 按场景的步骤指南 | 先读，不需要改 |
| [config/hyd-b300.example.yaml](config/hyd-b300.example.yaml) | B300 关闭配置起点 | 复制到 privateinputs/ 后改副本 |
| [config/target.example.yaml](config/target.example.yaml) | 通用 GPU、EKS、区域策略配置起点 | 改副本，删未准备的候选区域 |
| [config/cpu-validation.example.yaml](config/cpu-validation.example.yaml) | 单台 CPU 验证配置起点 | 改副本；不能直接套生产 deploy |
| [privateinputs/](#文件地图哪些要改哪些不用改)（自己创建） | 填真实 AWS 输入的个人 YAML | 需要；Git 忽略，不提交 |
| [app.py](app.py) | CDK 控制平面入口，读取 customer_id/target_id/region context | 通常传参数，不修改代码 |
| [runtime_handler.py](runtime_handler.py) | Lambda reconcile/collect/spot_event 入口 | 不需要改 |
| [h100_spot_controller/cli.py](h100_spot_controller/cli.py) | 操作命令实现 | 不需要改 |
| [config.py](h100_spot_controller/config.py) | 场景约束与配置解析 | 不需要改 |
| [reconciliation.py](h100_spot_controller/reconciliation.py) | 单写者、持久创建 intent 与恢复逻辑 | 不需要改 |
| [fleet.py](h100_spot_controller/fleet.py) | 构造/观察/核对 maintain Fleet | 不需要改 |
| [preflight.py](h100_spot_controller/preflight.py) | B300/CPU 的元数据、配额和启动合同检查 | 不需要改 |
| [infrastructure.py](h100_spot_controller/infrastructure.py) | Lambda/状态表/定时器/监控/IAM 的 CDK 定义 | 部署前审核；不靠随意加权限解决拒绝 |
| [cpu_test_deployment.py](h100_spot_controller/cpu_test_deployment.py) 与 [CPU 打包脚本](scripts/build-cpu-test-package.py) | 原隔离 CPU 实验的模板/离线打包工具 | 离线账号占位符，不是任意账户自动部署工具 |
| [requirements.lock](requirements.lock)、[pyproject.toml](pyproject.toml) | 依赖和项目安装入口 | 按安装步骤使用 |
| [scripts/nth-values.py](scripts/nth-values.py)、[nth.py](h100_spot_controller/nth.py) | 从 existing-eks target 生成固定版本 NTH Helm values | 默认 dry-run；真实 drain 须明确确认；不自动连接集群 |
| [scripts/release-check.py](scripts/release-check.py)、[tests/](tests/) | 离线回归检查 | 修改后运行，不拿它代替实机验收 |
| [openspec/](openspec/) | 行为规范和设计变更 | 开发者阅读；不是部署步骤入口 |
| [deployment/](deployment/) | 历史验证模板/参数输入 | 不默认当客户生产模板；含验证 fixture 的输入必须替换 |

历史 [validation-target.example.yaml](config/validation-target.example.yaml) / [validation-target.auto-initial.example.yaml](config/validation-target.auto-initial.example.yaml) 是 G6e functional-validation 的 Tokyo/Seoul fixture，不是通用生产的起点。不要照搬里面的历史账户和子网输入。

## 工作原理与重要边界

EventBridge 定时触发 Lambda → 从 DynamoDB 取 target/state → 校验输入与 ownership → 保存创建 intent/token → 创建一笔 Spot-only maintain Fleet → AWS 持续尝试满足数量 → Lambda 观察并报告。

- 每台 weight=1，台数不是 GPU 数；B300 的所有批准 AZ 共享总 target，不是每 AZ 各一份。
- 通用 sequential 路径按批准顺序扩 AZ/Local Zone；B300 一开始包含 all-approved 标准 AZ。
- Spot 小时价上限为客户 cap 和有效 Linux 按需价的较低值；不是账单总额硬限制，也不提高竞价优先级。
- 重复 Fleet、残余机器、不可见的 saved FleetId 或不明创建结果会阻止新容量写；不能删状态换 token 盲建。
- CPU 测试有独立最长 30 分钟到期合同；GPU/B300 不因此获得自动停机窗口。
- 实例补位 ≠ 任务恢复；healthy ≠ GPU/训练/EKS 就绪。
- 操作员授权由实际维护流程控制，CLI 的 --execute 是确认开关，不是完整审批平台。

## 验证状态与深入阅读

首尔 c6i.large 的隔离测试验证过创建、状态持久化、主动终止、同 Fleet 补位和清理，并修复无 paginator 的观察错误。它不证明原 CPU IAM 模板可重用，也不替代真实 Spot 回收、B300 获配或任务恢复验收。

B300 代码离线实现已存在；客户账户部署、实际 B300 获配和补位尚未验证。遇到权限、客户安全或预算问题先让负责人审核，不从测试成功推导生产权限已经足够。

- [B300 详细参数/两客户手册](docs/hyd-b300-quickstart.md)
- [通用操作参考](docs/operator-runbook.md)
- [写操作前检查清单](docs/deployment-preflight-checklist.md)
- [CPU 实现合同](docs/cpu-validation.md)
- [验证记录模板](docs/validation-evidence-template.md)

`h100` 出现在模块、CLI、tag 和指标名称中是历史兼容标识。通用 GPU 不绑定 H100；HYD profile 才刻意锁定 B300。不要为改名而替换已部署资源。
